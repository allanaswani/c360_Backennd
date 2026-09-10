"""Named tabular datasets — the single definition of every exportable table.

A dataset is ``{key, title, subtitle, columns, rows, generated_at, meta}`` where
``columns`` is a list of ``{key, label, type}`` and ``rows`` is a list of dicts.
The export endpoints render one to CSV / XLSX; the digest emails attach the same
structures. Defining them once is what stops the emailed report and the on-screen
export from drifting apart.

``type`` drives formatting only (``num``, ``ms``, ``pct``, ``datetime``, ``text``);
it never changes the underlying value, so a CSV opened in Excel keeps its numbers
as numbers.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

from django.db.models import Avg, Count, Max, Q, Sum
from django.utils import timezone

from .. import changes as change_audit
from ..models import AuditEvent, HealthSnapshot, MetricMinute


def _col(key: str, label: str, type_: str = 'text') -> dict:
    return {'key': key, 'label': label, 'type': type_}


def _dataset(key: str, title: str, columns: list[dict], rows: list[dict],
             subtitle: str = '', meta: dict | None = None) -> dict:
    return {
        'key': key,
        'title': title,
        'subtitle': subtitle,
        'columns': columns,
        'rows': rows,
        'row_count': len(rows),
        'generated_at': timezone.now().isoformat(),
        'meta': meta or {},
    }


# --- activity trail ---------------------------------------------------------

def activity(since=None, until=None, kind: str = '', status: str = '', q: str = '',
             user: str = '', limit: int = 50_000) -> dict:
    """The API/interaction audit trail — same filters as the admin screen.

    ``limit`` is a hard ceiling rather than a page size: an export is meant to hand
    over the whole filtered set, but an unbounded query against a table that grows
    with every request is how an export takes the server down.
    """
    qs = AuditEvent.objects.all()
    if since is not None:
        qs = qs.filter(ts__gte=since)
    if until is not None:
        qs = qs.filter(ts__lte=until)
    if kind:
        qs = qs.filter(kind=kind)
    if status:
        try:
            qs = qs.filter(status=int(status))
        except (TypeError, ValueError):
            pass
    if user:
        qs = qs.filter(username__icontains=user)
    if q:
        qs = qs.filter(Q(route__icontains=q) | Q(path__icontains=q)
                       | Q(target__icontains=q) | Q(username__icontains=q))

    rows = [{
        'ts': e.ts.isoformat(),
        'username': e.username or (f'#{e.user_id}' if e.user_id is not None else ''),
        'kind': e.kind,
        'method': e.method,
        'route': e.route,
        'path': e.path,
        'status': e.status,
        'duration_ms': e.duration_ms,
        'target': e.target,
        'ip': e.ip or '',
        'session': e.session,
    } for e in qs.order_by('-ts')[:limit]]

    return _dataset(
        'activity', 'Activity trail',
        [
            _col('ts', 'Time', 'datetime'), _col('username', 'User'),
            _col('kind', 'Kind'), _col('method', 'Method'), _col('route', 'Route'),
            _col('path', 'Path'), _col('status', 'Status', 'num'),
            _col('duration_ms', 'Duration (ms)', 'ms'), _col('target', 'Target'),
            _col('ip', 'IP'), _col('session', 'Session'),
        ],
        rows,
        subtitle=_window_text(since, until),
    )


# --- change audit -----------------------------------------------------------

def change_log(since=None, until=None, model: str = '', username: str = '',
               action: str = '', q: str = '', limit: int = 10_000) -> dict:
    """The who-changed-what feed, flattened one row per changed field.

    On screen a change is one row with a list of field diffs nested inside it. A
    spreadsheet has no nesting, so each field change becomes its own row — which
    is also the shape you want for filtering ("show me every sales_code change").
    """
    feed = change_audit.feed(since=since, until=until, model=model or None,
                             username=username or None, action=action or None,
                             search=q or None, limit=limit)
    rows: list[dict] = []
    for entry in feed:
        base = {
            'when': entry['when'],
            'username': entry['username'] or 'system',
            'action': entry['action'],
            'model_label': entry['model_label'],
            'object_id': entry['object_id'],
            'object_label': entry['object_label'],
            'reason': entry['reason'],
        }
        if entry['changes']:
            for change in entry['changes']:
                rows.append({**base, 'field': change['field'],
                             'old_value': change['old'], 'new_value': change['new']})
        else:
            rows.append({**base, 'field': '', 'old_value': '', 'new_value': ''})

    return _dataset(
        'changes', 'Change audit',
        [
            _col('when', 'Time', 'datetime'), _col('username', 'Changed by'),
            _col('action', 'Action'), _col('model_label', 'Record type'),
            _col('object_id', 'Record id'), _col('object_label', 'Record'),
            _col('field', 'Field'), _col('old_value', 'Before'),
            _col('new_value', 'After'), _col('reason', 'Reason'),
        ],
        rows,
        subtitle=_window_text(since, until),
    )


# --- monitoring -------------------------------------------------------------

def traffic_series(window_minutes: int = 1440) -> dict:
    """Per-minute traffic, latency and errors — the numbers behind the graphs."""
    cutoff = timezone.now() - timedelta(minutes=window_minutes)
    agg = (MetricMinute.objects.filter(minute__gte=cutoff)
           .values('minute')
           .annotate(count=Sum('count'), errors=Sum('errors'),
                     client_errors=Sum('client_errors'), sum_ms=Sum('sum_ms'),
                     p95=Max('p95_ms'), p99=Max('p99_ms'), max_ms=Max('max_ms'))
           .order_by('minute'))
    rows = [{
        'minute': r['minute'].isoformat(),
        'requests': r['count'] or 0,
        'errors_5xx': r['errors'] or 0,
        'errors_4xx': r['client_errors'] or 0,
        'rps': round((r['count'] or 0) / 60.0, 2),
        'avg_ms': round((r['sum_ms'] or 0) / r['count']) if r['count'] else 0,
        'p95_ms': r['p95'] or 0,
        'p99_ms': r['p99'] or 0,
        'max_ms': r['max_ms'] or 0,
    } for r in agg]

    return _dataset(
        'traffic', 'Traffic & latency',
        [
            _col('minute', 'Minute', 'datetime'), _col('requests', 'Requests', 'num'),
            _col('rps', 'Req/s', 'num'), _col('errors_5xx', '5xx', 'num'),
            _col('errors_4xx', '4xx', 'num'), _col('avg_ms', 'Avg', 'ms'),
            _col('p95_ms', 'p95', 'ms'), _col('p99_ms', 'p99', 'ms'),
            _col('max_ms', 'Max', 'ms'),
        ],
        rows,
        subtitle=f'last {_humanise_minutes(window_minutes)}',
    )


def routes(window_minutes: int = 1440, limit: int = 200) -> dict:
    """Per-route call volume, latency and error count for the window."""
    cutoff = timezone.now() - timedelta(minutes=window_minutes)
    agg = (AuditEvent.objects.filter(ts__gte=cutoff, kind='api')
           .values('route', 'method')
           .annotate(n=Count('id'), avg_ms=Avg('duration_ms'),
                     slowest=Max('duration_ms'),
                     errs=Count('id', filter=Q(status__gte=500)),
                     client_errs=Count('id', filter=Q(status__gte=400, status__lt=500)))
           .order_by('-n')[:limit])
    rows = [{
        'method': r['method'], 'route': r['route'], 'calls': r['n'],
        'avg_ms': round(r['avg_ms'] or 0), 'slowest_ms': r['slowest'] or 0,
        'errors_5xx': r['errs'], 'errors_4xx': r['client_errs'],
        'error_rate_pct': round(100.0 * r['errs'] / r['n'], 2) if r['n'] else 0.0,
    } for r in agg]

    return _dataset(
        'routes', 'Endpoints',
        [
            _col('method', 'Method'), _col('route', 'Route'),
            _col('calls', 'Calls', 'num'), _col('avg_ms', 'Avg', 'ms'),
            _col('slowest_ms', 'Slowest', 'ms'), _col('errors_5xx', '5xx', 'num'),
            _col('errors_4xx', '4xx', 'num'), _col('error_rate_pct', 'Error rate', 'pct'),
        ],
        rows,
        subtitle=f'last {_humanise_minutes(window_minutes)}',
    )


def error_log(window_minutes: int = 1440, limit: int = 20_000) -> dict:
    """Every 4xx/5xx served in the window — the log lines, as a table."""
    cutoff = timezone.now() - timedelta(minutes=window_minutes)
    qs = (AuditEvent.objects.filter(ts__gte=cutoff, status__gte=400)
          .order_by('-ts')[:limit])
    rows = [{
        'ts': e.ts.isoformat(),
        'status': e.status,
        'severity': 'server' if (e.status or 0) >= 500 else 'client',
        'method': e.method,
        'route': e.route,
        'path': e.path,
        'username': e.username or (f'#{e.user_id}' if e.user_id is not None else ''),
        'duration_ms': e.duration_ms,
        'ip': e.ip or '',
        'detail': _error_detail(e),
    } for e in qs]

    return _dataset(
        'errors', 'Error log',
        [
            _col('ts', 'Time', 'datetime'), _col('status', 'Status', 'num'),
            _col('severity', 'Severity'), _col('method', 'Method'),
            _col('route', 'Route'), _col('path', 'Path'), _col('username', 'User'),
            _col('duration_ms', 'Duration (ms)', 'ms'), _col('ip', 'IP'),
            _col('detail', 'Detail'),
        ],
        rows,
        subtitle=f'last {_humanise_minutes(window_minutes)}',
    )


def _error_detail(e: AuditEvent) -> str:
    """Whatever the middleware managed to attach — never invent a message."""
    meta = e.meta if isinstance(e.meta, dict) else {}
    for key in ('detail', 'error', 'exception', 'message'):
        if meta.get(key):
            return str(meta[key])[:300]
    return ''


# --- data health ------------------------------------------------------------

def data_health(report: dict) -> dict:
    """The warehouse health report as a table. Takes the already-built report so a
    caller never triggers a second (expensive) round of warehouse checks."""
    rows = [{
        'group': c.get('group', ''),
        'check': c.get('label', c.get('key', '')),
        'table': c.get('table', ''),
        'status': c.get('status', ''),
        'value': c.get('value'),
        'delta_pct': c.get('delta_pct'),
        'latency_ms': c.get('latency_ms'),
        'detail': c.get('detail', ''),
    } for c in (report.get('checks') or [])]

    freshness = report.get('freshness') or {}
    return _dataset(
        'data_health', 'Data health',
        [
            _col('group', 'Group'), _col('check', 'Check'), _col('table', 'Table'),
            _col('status', 'Status'), _col('value', 'Value', 'num'),
            _col('delta_pct', 'Change vs last', 'pct'),
            _col('latency_ms', 'Latency', 'ms'), _col('detail', 'Detail'),
        ],
        rows,
        subtitle=f"mode {report.get('data_mode', '?')}"
                 + (f" · as of {freshness.get('as_of')}" if freshness.get('as_of') else ''),
        meta={'freshness': freshness, 'data_mode': report.get('data_mode')},
    )


def health_history(limit: int = 200) -> dict:
    """Stored health snapshots — freshness over time, for the trend export."""
    snaps = list(HealthSnapshot.objects.all()[:limit])
    rows = []
    for sn in reversed(snaps):
        checks = sn.payload.get('checks') or []
        rows.append({
            'captured_at': sn.captured_at.isoformat(),
            'days_behind': sn.days_behind,
            'checks_ok': sum(1 for c in checks if c.get('status') == 'ok'),
            'checks_warn': sum(1 for c in checks if c.get('status') == 'warn'),
            'checks_error': sum(1 for c in checks if c.get('status') == 'error'),
            'checks_empty': sum(1 for c in checks if c.get('status') == 'empty'),
        })
    return _dataset(
        'health_history', 'Data-health history',
        [
            _col('captured_at', 'Captured', 'datetime'),
            _col('days_behind', 'Days behind', 'num'),
            _col('checks_ok', 'OK', 'num'), _col('checks_warn', 'Warn', 'num'),
            _col('checks_error', 'Error', 'num'), _col('checks_empty', 'Empty', 'num'),
        ],
        rows,
    )


# --- usage ------------------------------------------------------------------

def users_activity(window_minutes: int = 1440, limit: int = 500) -> dict:
    """Who used the app in the window, and how hard — for the digest's adoption line."""
    cutoff = timezone.now() - timedelta(minutes=window_minutes)
    agg = (AuditEvent.objects.filter(ts__gte=cutoff)
           .exclude(username='')
           .values('username')
           .annotate(actions=Count('id'),
                     api_calls=Count('id', filter=Q(kind='api')),
                     errors=Count('id', filter=Q(status__gte=500)),
                     last_seen=Max('ts'))
           .order_by('-actions')[:limit])
    rows = [{
        'username': r['username'], 'actions': r['actions'],
        'api_calls': r['api_calls'], 'errors': r['errors'],
        'last_seen': r['last_seen'].isoformat() if r['last_seen'] else '',
    } for r in agg]
    return _dataset(
        'users', 'User activity',
        [
            _col('username', 'User'), _col('actions', 'Actions', 'num'),
            _col('api_calls', 'API calls', 'num'), _col('errors', 'Errors', 'num'),
            _col('last_seen', 'Last seen', 'datetime'),
        ],
        rows,
        subtitle=f'last {_humanise_minutes(window_minutes)}',
    )


# --- registry ---------------------------------------------------------------

#: Datasets the export endpoint can serve by name, and how to build one from
#: query parameters. Anything not listed here cannot be exported — the endpoint
#: never turns arbitrary caller input into a query.
EXPORTABLE: dict[str, Callable[..., dict]] = {
    'activity': activity,
    'changes': change_log,
    'traffic': traffic_series,
    'routes': routes,
    'errors': error_log,
    'health_history': health_history,
    'users': users_activity,
}


def _humanise_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f'{d} day{"s" if d != 1 else ""}'
    if minutes % 60 == 0:
        h = minutes // 60
        return f'{h} hour{"s" if h != 1 else ""}'
    return f'{minutes} minutes'


def _window_text(since, until) -> str:
    if since and until:
        return f'{since:%d %b %Y %H:%M} → {until:%d %b %Y %H:%M}'
    if since:
        return f'since {since:%d %b %Y %H:%M}'
    if until:
        return f'up to {until:%d %b %Y %H:%M}'
    return 'all time'


def summarise(window_minutes: int) -> dict[str, Any]:
    """Headline numbers for a window — shared by the digest email and the alert
    checks so the two can never disagree about what the error rate was."""
    now = timezone.now()
    cutoff = now - timedelta(minutes=window_minutes)
    agg = MetricMinute.objects.filter(minute__gte=cutoff).aggregate(
        requests=Sum('count'), errors=Sum('errors'), client_errors=Sum('client_errors'),
        sum_ms=Sum('sum_ms'), p95=Max('p95_ms'), p99=Max('p99_ms'), worst=Max('max_ms'))
    requests = agg['requests'] or 0
    errors = agg['errors'] or 0
    minutes_with_traffic = (MetricMinute.objects.filter(minute__gte=cutoff)
                            .values('minute').distinct().count())
    active_users = (AuditEvent.objects.filter(ts__gte=cutoff)
                    .exclude(username='').values('username').distinct().count())
    return {
        'window_minutes': window_minutes,
        'from': cutoff.isoformat(),
        'to': now.isoformat(),
        'requests': requests,
        'errors': errors,
        'client_errors': agg['client_errors'] or 0,
        'error_rate_pct': round(100.0 * errors / requests, 2) if requests else 0.0,
        'avg_ms': round((agg['sum_ms'] or 0) / requests) if requests else 0,
        'p95_ms': agg['p95'] or 0,
        'p99_ms': agg['p99'] or 0,
        'slowest_ms': agg['worst'] or 0,
        'active_users': active_users,
        'minutes_with_traffic': minutes_with_traffic,
    }
