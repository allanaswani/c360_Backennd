"""Incident detection — "in case of anything", by email.

Runs a fixed set of checks against what the app has actually measured, decides
which are firing, and mails only the *transitions*: an incident that starts, one
that is still going after ``C360_ALERT_RENOTIFY_MINUTES``, and one that clears.
The dedupe state lives in :class:`~c360.models.AlertState`.

What is checked, and why each one is here:

``warehouse_conn``  The Trino connection or its credentials failed. This has taken
                    the app down before (an expired service-account password), and
                    it is invisible from the outside — pages just go empty.
``data_stale``      The warehouse is reachable but its as-of date has stopped
                    moving. Every figure in the app silently becomes yesterday's.
``source_empty``    A source table went to zero rows, or lost a large share of
                    them — a partial or failed pipeline load.
``error_rate``      Server errors crossed the threshold over the last hour.
``latency``         p95 crossed the threshold over the last hour.
``no_traffic``      The app served nothing for an hour during working hours. Only
                    checked 08:00–18:00 on weekdays, because a silent Sunday
                    night is not an incident and alerting on it trains people to
                    ignore the sender.

Checks are independent: one raising an exception must not stop the others, so
each is wrapped and a failing check reports itself as a problem rather than
vanishing.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import datasets, mailer, render
from ..models import AlertState

log = logging.getLogger('c360')

WORKING_HOURS = range(8, 18)          # local hours in which silence is suspicious
_DROP_FRACTION = 0.30                 # matches the data-health screen's drop alarm


def _finding(key: str, severity: str, title: str, detail: str, hint: str = '') -> dict:
    return {'key': key, 'severity': severity, 'title': title, 'detail': detail, 'hint': hint}


# --- the checks -------------------------------------------------------------

def _check_warehouse(health: dict) -> list[dict]:
    if health.get('unavailable'):
        return [_finding('warehouse_conn', 'critical', 'Warehouse health check failed',
                         health['unavailable'],
                         'The check itself could not run — treat the warehouse as unverified.')]
    conn = next((c for c in health.get('checks') or [] if c.get('key') == 'warehouse_conn'), None)
    if conn and conn.get('status') == 'error':
        return [_finding('warehouse_conn', 'critical', 'Warehouse unreachable',
                         conn.get('detail') or 'connection failed',
                         'Check TRINO_USER / TRINO_PASSWORD and network reachability.')]
    return []


def _check_freshness(health: dict) -> list[dict]:
    freshness = health.get('freshness') or {}
    if not freshness:
        return []
    limit = int(getattr(settings, 'C360_ALERT_DATA_STALE_DAYS', 3))
    days = freshness.get('days_behind')
    if freshness.get('status') == 'error':
        return [_finding('data_stale', 'critical', 'Data freshness unknown',
                         freshness.get('detail') or 'the as-of date could not be read')]
    if isinstance(days, int) and days > limit:
        return [_finding('data_stale', 'warning', f'Warehouse data is {days} days behind',
                         f"as-of {freshness.get('as_of')} — threshold is {limit} day(s)",
                         'Every figure in the app is showing this date, not today.')]
    return []


def _check_sources(health: dict) -> list[dict]:
    broken = []
    for check in health.get('checks') or []:
        if check.get('key') == 'warehouse_conn':
            continue                                   # already covered, don't double-alert
        status = check.get('status')
        if status in {'error', 'empty'}:
            broken.append(f"{check.get('label')}: {check.get('detail')}")
        elif isinstance(check.get('delta_pct'), (int, float)) and check['delta_pct'] <= -_DROP_FRACTION * 100:
            broken.append(f"{check.get('label')}: down {abs(check['delta_pct'])}% since the last check")
    if not broken:
        return []
    return [_finding('source_empty', 'critical' if len(broken) > 2 else 'warning',
                     f'{len(broken)} warehouse source(s) failing',
                     '; '.join(broken[:6]),
                     'A partial pipeline load shows up here before users notice.')]


def _check_error_rate(summary: dict) -> list[dict]:
    threshold = float(getattr(settings, 'C360_ALERT_ERROR_RATE_PCT', 5))
    # Below a floor of requests a single error is a large percentage; alerting on
    # that is noise, not signal.
    if summary['requests'] < 20:
        return []
    if summary['error_rate_pct'] >= threshold:
        return [_finding('error_rate', 'critical',
                         f"Error rate {summary['error_rate_pct']}%",
                         f"{summary['errors']:,} server errors out of {summary['requests']:,} "
                         f"requests in the last hour (threshold {threshold}%)")]
    return []


def _check_latency(summary: dict) -> list[dict]:
    threshold = int(getattr(settings, 'C360_ALERT_P95_MS', 2000))
    if summary['requests'] < 20:
        return []
    if summary['p95_ms'] >= threshold:
        return [_finding('latency', 'warning', f"p95 latency {summary['p95_ms']:,} ms",
                         f"threshold is {threshold:,} ms; p99 {summary['p99_ms']:,} ms, "
                         f"slowest {summary['slowest_ms']:,} ms")]
    return []


def _check_traffic(summary: dict) -> list[dict]:
    now = timezone.localtime()
    if now.weekday() >= 5 or now.hour not in WORKING_HOURS:
        return []
    if summary['requests'] == 0:
        return [_finding('no_traffic', 'critical', 'No requests served in the last hour',
                         f'during working hours ({now:%a %H:%M})',
                         'Either nobody can reach the app, or it is not running.')]
    return []


def evaluate() -> list[dict]:
    """Run every check. A check that raises reports itself rather than disappearing."""
    from .digest import _health_report

    findings: list[dict] = []
    try:
        health = _health_report()
    except Exception as exc:                                 # noqa: BLE001
        health = {'unavailable': f'{type(exc).__name__}: {str(exc)[:200]}', 'checks': []}
    try:
        summary = datasets.summarise(60)
    except Exception as exc:                                 # noqa: BLE001
        log.exception('alert: traffic summary failed')
        findings.append(_finding('metrics_read', 'critical', 'Could not read request metrics',
                                 f'{type(exc).__name__}: {str(exc)[:200]}'))
        summary = None

    checks: list = [(_check_warehouse, health), (_check_freshness, health), (_check_sources, health)]
    if summary is not None:
        checks += [(_check_error_rate, summary), (_check_latency, summary), (_check_traffic, summary)]

    for fn, arg in checks:
        # The name is read defensively: this handler exists so one broken check
        # cannot silence the rest, and it would defeat itself by raising while
        # reporting a failure.
        name = getattr(fn, '__name__', None) or repr(fn)
        try:
            findings.extend(fn(arg))
        except Exception as exc:                             # noqa: BLE001
            log.exception('alert check %s failed', name)
            findings.append(_finding(f'check_{name}', 'warning',
                                     f'Alert check {name} failed',
                                     f'{type(exc).__name__}: {str(exc)[:200]}'))
    return findings


# --- transitions ------------------------------------------------------------

def _renotify_due(state: AlertState, now) -> bool:
    minutes = int(getattr(settings, 'C360_ALERT_RENOTIFY_MINUTES', 360))
    if minutes <= 0 or state.last_notified_at is None:
        return False
    return (now - state.last_notified_at) >= timedelta(minutes=minutes)


def reconcile(findings: list[dict]) -> dict[str, list[dict]]:
    """Compare findings against stored state; return what needs an email.

    ``{'new': [...], 'ongoing': [...], 'resolved': [...]}``. Only ``new`` and
    ``ongoing`` (past the re-notify interval) plus ``resolved`` are mailed.
    """
    now = timezone.now()
    firing_keys = {f['key'] for f in findings}
    new, ongoing, resolved = [], [], []

    for finding in findings:
        state, created = AlertState.objects.get_or_create(key=finding['key'])
        if created or not state.firing:
            state.firing = True
            state.since = now
            state.notify_count = 1
            state.last_notified_at = now
            state.severity = finding['severity']
            state.detail = finding['detail']
            state.save()
            new.append(finding)
        elif _renotify_due(state, now):
            state.notify_count += 1
            state.last_notified_at = now
            state.severity = finding['severity']
            state.detail = finding['detail']
            state.save()
            ongoing.append({**finding, 'since': state.since, 'notify_count': state.notify_count})
        else:
            # Still firing, still inside the quiet interval — record, do not mail.
            state.severity = finding['severity']
            state.detail = finding['detail']
            state.save(update_fields=['severity', 'detail', 'updated_at'])

    for state in AlertState.objects.filter(firing=True).exclude(key__in=firing_keys):
        resolved.append({'key': state.key, 'severity': state.severity,
                         'title': _resolved_title(state.key), 'detail': state.detail,
                         'since': state.since,
                         'duration': _humanise(now - state.since) if state.since else ''})
        state.firing = False
        state.last_notified_at = now
        state.save()

    return {'new': new, 'ongoing': ongoing, 'resolved': resolved}


def _resolved_title(key: str) -> str:
    return {
        'warehouse_conn': 'Warehouse connection restored',
        'data_stale': 'Warehouse data is fresh again',
        'source_empty': 'Warehouse sources are healthy again',
        'error_rate': 'Error rate back to normal',
        'latency': 'Latency back to normal',
        'no_traffic': 'Traffic has resumed',
    }.get(key, f'{key} resolved')


def _humanise(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return f'{minutes} min'
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f'{hours}h {minutes}m'
    days, hours = divmod(hours, 24)
    return f'{days}d {hours}h'


# --- delivery ---------------------------------------------------------------

def _render_alert(new: list[dict], ongoing: list[dict], resolved: list[dict]) -> tuple[str, str, str]:
    worst = 'critical' if any(f['severity'] == 'critical' for f in new + ongoing) else 'warning'
    firing = new + ongoing
    if firing:
        headline = firing[0]['title'] if len(firing) == 1 else f'{len(firing)} issues detected'
        subject = f"[C360] {'ALERT' if worst == 'critical' else 'Warning'} — {headline}"
    else:
        subject = (f"[C360] Resolved — {resolved[0]['title']}" if len(resolved) == 1
                   else f'[C360] Resolved — {len(resolved)} issues cleared')

    blocks: list[str] = []
    lines: list[str] = []

    for group, label, tone in ((new, 'New', 'bad'), (ongoing, 'Still open', 'warn')):
        if not group:
            continue
        blocks.append(render.heading(label))
        for finding in group:
            tone_used = 'bad' if finding['severity'] == 'critical' else 'warn'
            body = f"{finding['title']} — {finding['detail']}"
            if finding.get('hint'):
                body += f" · {finding['hint']}"
            if finding.get('since'):
                body += f" · open since {timezone.localtime(finding['since']):%d %b %H:%M}"
            blocks.append(render.callout(body, tone=tone_used))
            lines.append(f"[{finding['severity'].upper()}] {finding['title']}: {finding['detail']}")

    if resolved:
        blocks.append(render.heading('Resolved'))
        for finding in resolved:
            body = finding['title']
            if finding.get('duration'):
                body += f" — was open for {finding['duration']}"
            blocks.append(render.callout(body, tone='good'))
            lines.append(f"[RESOLVED] {finding['title']}")

    title = 'Incident alert' if firing else 'Incident resolved'
    html = render.shell(
        title=title,
        preheader=subject.replace('[C360] ', ''),
        blocks=blocks,
        app_url=getattr(settings, 'C360_APP_URL', ''),
        footer_note='One email per incident — you will not be re-sent this while it stays open.',
    )
    return subject, html, render.to_text(subject, lines)


def run(dry_run: bool = False) -> dict:
    """Evaluate, reconcile and (unless dry-run) mail. Returns what it decided."""
    findings = evaluate()
    outcome = reconcile(findings) if not dry_run else {
        'new': findings, 'ongoing': [], 'resolved': [],
    }
    to_mail = outcome['new'] + outcome['ongoing'] + outcome['resolved']
    outcome['findings'] = findings
    outcome['sent'] = 0
    if not to_mail:
        return outcome
    subject, html, text = _render_alert(outcome['new'], outcome['ongoing'], outcome['resolved'])
    outcome['subject'] = subject
    outcome['html'] = html
    outcome['text'] = text
    if not dry_run:
        outcome['sent'] = mailer.send(subject, html, text)
    return outcome
