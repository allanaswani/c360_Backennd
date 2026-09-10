"""The daily and weekly operations digests.

One email that answers, without anyone logging in: did the app stay up, was it
fast, did anything error, is the warehouse data still arriving, who used it, and
what did administrators change. Attachments carry the full tables; the body
carries the parts a person will actually read on a phone at 07:00.

Everything here is measured, never estimated. Where a number cannot be computed —
no traffic in the window, health checks that only run in live mode — the digest
says so rather than printing a zero that reads like a fact.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import datasets, mailer, render


def _tone_error_rate(pct: float) -> str:
    threshold = float(getattr(settings, 'C360_ALERT_ERROR_RATE_PCT', 5))
    if pct >= threshold:
        return 'bad'
    return 'warn' if pct >= threshold / 5 else 'good'


def _tone_latency(p95: int) -> str:
    threshold = int(getattr(settings, 'C360_ALERT_P95_MS', 2000))
    if p95 >= threshold:
        return 'bad'
    return 'warn' if p95 >= threshold / 2 else 'good'


def _health_report() -> dict:
    """The current warehouse health report, or a marker that it couldn't be read.

    A digest must never claim the warehouse is healthy because the check itself
    fell over, so a failure here is reported as a failure.
    """
    from ..warehouse.factory import data_mode, get_gateway
    try:
        report = get_gateway().health_report()
        report['data_mode'] = data_mode()
        return report
    except Exception as exc:                                 # noqa: BLE001
        return {'data_mode': 'unknown', 'freshness': None, 'checks': [],
                'unavailable': f'{type(exc).__name__}: {str(exc)[:200]}'}


def build(window_minutes: int, title: str) -> dict:
    """Assemble one digest. Returns ``{subject, html, text, attachments, summary}``."""
    now = timezone.localtime()
    summary = datasets.summarise(window_minutes)
    health = _health_report()
    since = timezone.now() - timedelta(minutes=window_minutes)

    ds_errors = datasets.error_log(window_minutes)
    ds_routes = datasets.routes(window_minutes)
    ds_users = datasets.users_activity(window_minutes)
    ds_changes = datasets.change_log(since=since)
    ds_health = datasets.data_health(health)
    ds_activity = datasets.activity(since=since)

    had_traffic = summary['requests'] > 0
    error_rate = summary['error_rate_pct']
    app_url = getattr(settings, 'C360_APP_URL', '')

    blocks: list[str] = []
    text_lines: list[str] = []

    # --- headline ----------------------------------------------------------
    if not had_traffic:
        blocks.append(render.callout(
            'No requests were served in this window. That is normal for a quiet '
            'period, but if you expected traffic it means the app was not reachable.',
            tone='warn'))
        text_lines.append('No requests served in this window.')
    else:
        blocks.append(render.tiles([
            {'label': 'Requests', 'value': f"{summary['requests']:,}",
             'sub': f"{summary['active_users']} active user(s)"},
            {'label': 'Error rate', 'value': f'{error_rate}%',
             'tone': _tone_error_rate(error_rate),
             'sub': f"{summary['errors']:,} server, {summary['client_errors']:,} client"},
            {'label': 'p95 latency', 'value': f"{summary['p95_ms']:,} ms",
             'tone': _tone_latency(summary['p95_ms']),
             'sub': f"p99 {summary['p99_ms']:,} ms · avg {summary['avg_ms']:,} ms"},
        ]))
        text_lines += [
            f"Requests:    {summary['requests']:,}",
            f"Error rate:  {error_rate}%  ({summary['errors']:,} server / {summary['client_errors']:,} client)",
            f"Latency:     p95 {summary['p95_ms']:,} ms · p99 {summary['p99_ms']:,} ms · avg {summary['avg_ms']:,} ms",
            f"Active users:{summary['active_users']:>4}",
        ]

    # --- data health -------------------------------------------------------
    blocks.append(render.heading('Data health', health.get('data_mode') or ''))
    if health.get('unavailable'):
        blocks.append(render.callout(
            f"The health check could not run: {health['unavailable']}", tone='bad'))
        text_lines.append(f"Data health: CHECK FAILED — {health['unavailable']}")
    elif not (health.get('checks') or []):
        blocks.append(render.empty(
            'No source checks ran. Source health is only measured against the live '
            'warehouse (C360_DATA_MODE=live).'))
        text_lines.append('Data health: not measured (not in live mode).')
    else:
        checks = health['checks']
        bad = [c for c in checks if c.get('status') in {'error', 'empty'}]
        warn = [c for c in checks if c.get('status') == 'warn']
        freshness = health.get('freshness') or {}
        days = freshness.get('days_behind')
        blocks.append(render.tiles([
            {'label': 'Sources OK', 'value': f"{sum(1 for c in checks if c.get('status') == 'ok')}/{len(checks)}",
             'tone': 'bad' if bad else 'warn' if warn else 'good'},
            {'label': 'Data freshness',
             'value': ('not known' if days is None else f'{days} day(s) behind'),
             'tone': 'bad' if days is None or days > int(getattr(settings, 'C360_ALERT_DATA_STALE_DAYS', 3))
                     else 'good',
             'sub': f"as of {freshness.get('as_of')}" if freshness.get('as_of') else ''},
            {'label': 'Needs attention', 'value': str(len(bad) + len(warn)),
             'tone': 'bad' if bad else 'warn' if warn else 'good'},
        ]))
        text_lines.append(
            f"Data health: {len(checks) - len(bad) - len(warn)}/{len(checks)} OK, "
            f"{len(warn)} warn, {len(bad)} failing; freshness "
            f"{'unknown' if days is None else str(days) + ' day(s) behind'}")
        if bad or warn:
            rows = [{**r, 'tone': 'bad' if r['status'] in {'error', 'empty'} else 'warn'}
                    for r in ds_health['rows'] if r['status'] in {'error', 'empty', 'warn'}]
            blocks.append(render.table(
                [c for c in ds_health['columns'] if c['key'] in {'check', 'status', 'value', 'detail'}],
                rows, limit=10, tone_key='tone'))

    # --- errors ------------------------------------------------------------
    blocks.append(render.heading('Errors', f"{ds_errors['row_count']:,} in the window"))
    if ds_errors['row_count'] == 0:
        blocks.append(render.empty('No 4xx or 5xx responses in this window.'))
        text_lines.append('Errors: none.')
    else:
        rows = [{**r, 'tone': 'bad' if r['severity'] == 'server' else 'warn'} for r in ds_errors['rows']]
        blocks.append(render.table(
            [c for c in ds_errors['columns'] if c['key'] in {'ts', 'status', 'route', 'username'}],
            rows, limit=8, tone_key='tone'))
        text_lines.append(f"Errors: {ds_errors['row_count']:,} (full list attached).")

    # --- busiest endpoints -------------------------------------------------
    if ds_routes['row_count']:
        blocks.append(render.heading('Busiest endpoints'))
        blocks.append(render.table(
            [c for c in ds_routes['columns'] if c['key'] in {'route', 'calls', 'avg_ms', 'errors_5xx'}],
            ds_routes['rows'], limit=8))

    # --- who used it -------------------------------------------------------
    blocks.append(render.heading('Usage', f"{ds_users['row_count']} user(s) active"))
    if ds_users['row_count'] == 0:
        blocks.append(render.empty('Nobody signed in during this window.'))
    else:
        blocks.append(render.table(
            [c for c in ds_users['columns'] if c['key'] in {'username', 'actions', 'errors', 'last_seen'}],
            ds_users['rows'], limit=8))

    # --- what changed ------------------------------------------------------
    blocks.append(render.heading('Administrative changes',
                                 f"{ds_changes['row_count']} field change(s)"))
    if ds_changes['row_count'] == 0:
        blocks.append(render.empty('No accounts, roles or RM allocations were changed.'))
        text_lines.append('Changes: none.')
    else:
        blocks.append(render.table(
            [c for c in ds_changes['columns']
             if c['key'] in {'when', 'username', 'action', 'object_label', 'field', 'new_value'}],
            ds_changes['rows'], limit=10))
        text_lines.append(f"Changes: {ds_changes['row_count']} field change(s) (attached).")

    subject = f'[C360] {title} — {now:%a %d %b %Y}'
    if not had_traffic:
        subject += ' · no traffic'
    elif error_rate >= float(getattr(settings, 'C360_ALERT_ERROR_RATE_PCT', 5)):
        subject += f' · {error_rate}% errors'

    html = render.shell(
        title=title,
        preheader=f"{now:%d %b %Y %H:%M} · {summary['requests']:,} requests · {error_rate}% errors",
        blocks=blocks,
        app_url=app_url,
        footer_note='Attachments hold the complete tables; the body shows the top rows only.',
    )

    attachments = [
        mailer.xlsx_attachment(ds_errors),
        mailer.xlsx_attachment(ds_changes),
        mailer.xlsx_attachment(ds_health),
        mailer.csv_attachment(ds_activity),
    ]

    return {
        'subject': subject,
        'html': html,
        'text': render.to_text(subject, text_lines),
        'attachments': attachments,
        'summary': summary,
    }


def send_daily() -> dict:
    report = build(24 * 60, 'Daily operations report')
    report['sent'] = mailer.send(report['subject'], report['html'], report['text'],
                                 report['attachments'])
    return report


def send_weekly() -> dict:
    report = build(7 * 24 * 60, 'Weekly operations rollup')
    report['sent'] = mailer.send(report['subject'], report['html'], report['text'],
                                 report['attachments'])
    return report
