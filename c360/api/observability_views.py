"""Observability + audit API — reads the persisted rollups/events (all workers, via the
DB) for the in-app ops dashboard, plus the client telemetry collector and the Prometheus
``/metrics`` exposition. Dashboard reads are admin-only; ``/metrics`` is unauthenticated
(scrape it on a trusted network) and exposes only aggregate numbers, never PII."""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg, Count, Max, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .. import changes, observability as obs
from ..models import AuditEvent, MetricMinute
from ..rbac.scoping import resolve_scope
from ..reports import datasets, tabular


def _admin_or_403(request):
    scope = resolve_scope(request)
    if not scope.is_admin:
        return Response({'error': {'status': 403, 'detail': 'Observability is admin-only.'}},
                        status=status.HTTP_403_FORBIDDEN)
    return None


class MetricsExporterView(APIView):
    """GET /metrics — Prometheus text exposition for THIS worker's live window, so a real
    Prometheus can scrape it whenever you stand one up (each worker is its own target)."""
    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request: Request):
        return HttpResponse(obs.prometheus_text(), content_type='text/plain; version=0.0.4; charset=utf-8')


class ObservabilityOverviewView(APIView):
    """GET /api/observability/overview/?window=<minutes> — the live ops board: traffic,
    latency percentiles, error rate, uptime, top routes, recent errors — aggregated across
    all workers from the persisted minute rollups + the audit trail."""

    def get(self, request: Request):
        denied = _admin_or_403(request)
        if denied:
            return denied
        try:
            window = max(5, min(1440, int(request.query_params.get('window', 60))))
        except (TypeError, ValueError):
            window = 60
        now = timezone.now()
        cutoff = now - timedelta(minutes=window)

        # Per-minute series aggregated across instances (workers).
        rows = list(
            MetricMinute.objects.filter(minute__gte=cutoff)
            .values('minute')
            .annotate(count=Sum('count'), errors=Sum('errors'), client_errors=Sum('client_errors'),
                      sum_ms=Sum('sum_ms'), p95=Max('p95_ms'), p99=Max('p99_ms'), max_ms=Max('max_ms'))
            .order_by('minute'))
        series = [{
            'minute': r['minute'].isoformat(),
            'count': r['count'] or 0,
            'errors': r['errors'] or 0,
            'client_errors': r['client_errors'] or 0,
            'rps': round((r['count'] or 0) / 60.0, 2),
            'avg_ms': round((r['sum_ms'] or 0) / r['count']) if r['count'] else 0,
            'p95_ms': r['p95'] or 0,
            'p99_ms': r['p99'] or 0,
            'max_ms': r['max_ms'] or 0,
        } for r in rows]

        total = sum(s['count'] for s in series)
        errors = sum(s['errors'] for s in series)
        client_errors = sum(s['client_errors'] for s in series)
        present = len(series)                       # minutes that actually recorded traffic
        # Downtime: minutes in the window with no data from any worker.
        uptime_pct = round(100.0 * present / window, 2) if window else 100.0
        last = series[-1] if series else None

        # Route breakdown + recent errors come from the audit trail (it carries the route
        # + duration + status per action). Windowed + indexed, so this stays cheap.
        audit_win = AuditEvent.objects.filter(ts__gte=cutoff, kind='api')
        top_routes = list(
            audit_win.values('route', 'method')
            .annotate(n=Count('id'), avg_ms=Avg('duration_ms'),
                      errs=Count('id', filter=Q(status__gte=500)))
            .order_by('-n')[:12])
        top_routes = [{
            'route': r['route'], 'method': r['method'], 'count': r['n'],
            'avg_ms': round(r['avg_ms'] or 0), 'errors': r['errs'],
        } for r in top_routes]
        recent_errors = [_audit_row(e) for e in
                         AuditEvent.objects.filter(ts__gte=cutoff, status__gte=400).order_by('-ts')[:15]]
        active_users = audit_win.exclude(user_id__isnull=True).values('user_id').distinct().count()
        instances = MetricMinute.objects.filter(minute__gte=now - timedelta(minutes=3)) \
            .values('instance').distinct().count()

        return Response({
            'generated_at': now.isoformat(),
            'window_minutes': window,
            'summary': {
                'requests': total,
                'errors': errors,
                'client_errors': client_errors,
                'error_rate_pct': round(100.0 * errors / total, 2) if total else 0.0,
                'rps_now': last['rps'] if last else 0.0,
                'p95_now_ms': last['p95_ms'] if last else 0,
                'p99_now_ms': last['p99_ms'] if last else 0,
                'uptime_pct': uptime_pct,
                'active_users': active_users,
                'instances': instances,
            },
            'series': series,
            'top_routes': top_routes,
            'recent_errors': recent_errors,
        })


class AuditListView(APIView):
    """GET /api/observability/audit/ — filterable, paginated audit trail. Filters:
    ?user=<id>&kind=<k>&status=<n>&q=<substring>&since=<iso>&until=<iso>&limit=&offset="""

    def get(self, request: Request):
        denied = _admin_or_403(request)
        if denied:
            return denied
        qp = request.query_params
        qs = AuditEvent.objects.all()
        if qp.get('user'):
            qs = qs.filter(Q(user_id=_int(qp['user'])) | Q(username__icontains=qp['user']))
        if qp.get('kind'):
            qs = qs.filter(kind=qp['kind'])
        if qp.get('status'):
            qs = qs.filter(status=_int(qp['status']))
        if qp.get('q'):
            term = qp['q']
            qs = qs.filter(Q(route__icontains=term) | Q(path__icontains=term)
                           | Q(target__icontains=term) | Q(username__icontains=term))
        since = _since_from(qp)
        if since is not None:
            qs = qs.filter(ts__gte=since)
        if qp.get('until'):
            qs = qs.filter(ts__lte=qp['until'])
        limit = max(1, min(200, _int(qp.get('limit')) or 50))
        offset = max(0, _int(qp.get('offset')) or 0)
        total = qs.count()
        rows = [_audit_row(e) for e in qs.order_by('-ts')[offset:offset + limit]]
        return Response({'count': total, 'limit': limit, 'offset': offset, 'results': rows})


class TelemetryCollectView(APIView):
    """POST /api/telemetry/collect/ — the client interaction beacon. Accepts a BATCH of
    events {events:[{kind, route, path, target, meta}]} sent via navigator.sendBeacon, so
    a click never blocks on the network. Buffered like server events (no inline DB write).
    Clicks are sampled at OBSERVABILITY_CLIENT_SAMPLE; navigations/page views are kept."""

    def post(self, request: Request):
        from django.conf import settings
        import random
        events = (request.data or {}).get('events') or []
        if not isinstance(events, list):
            return Response({'accepted': 0})
        uid = request.user.id if getattr(request.user, 'is_authenticated', False) else None
        uname = request.user.get_username() if uid else ''
        sample = float(getattr(settings, 'OBSERVABILITY_CLIENT_SAMPLE', 1.0))
        ip = request.META.get('HTTP_X_FORWARDED_FOR', request.META.get('REMOTE_ADDR', '')).split(',')[0].strip()
        session = request.headers.get('X-C360-Session', '')[:64]
        accepted = 0
        for ev in events[:100]:                 # cap a single beacon's payload
            if not isinstance(ev, dict):
                continue
            kind = str(ev.get('kind') or 'click')[:16]
            if kind == 'click' and sample < 1.0 and random.random() > sample:
                continue
            obs.audit_buffer.record(
                user_id=uid, username=uname or '', kind=kind,
                method='', route=str(ev.get('route') or '')[:200], path=str(ev.get('path') or '')[:300],
                status=None, duration_ms=_int(ev.get('duration_ms')),
                target=str(ev.get('target') or '')[:200], ip=ip or None, session=session,
                meta=ev.get('meta') if isinstance(ev.get('meta'), dict) else {})
            accepted += 1
        return Response({'accepted': accepted})


class ChangeAuditView(APIView):
    """GET /api/observability/changes/ — who changed what, with before → after.

    The companion to the activity trail: that one records what was *accessed*, this
    one records what was *altered*. Filters: ``?model=&user=&action=&q=&since=&
    until=&limit=``.
    """

    def get(self, request: Request):
        denied = _admin_or_403(request)
        if denied:
            return denied
        qp = request.query_params
        limit = max(1, min(500, _int(qp.get('limit')) or 100))
        since = _since_from(qp)
        rows = changes.feed(
            since=since, until=_parse_dt(qp.get('until')),
            model=qp.get('model') or None, username=qp.get('user') or None,
            action=qp.get('action') or None, search=qp.get('q') or None,
            limit=limit,
        )
        return Response({
            'count': len(rows),
            'limit': limit,
            'results': rows,
            'summary': changes.summary(since=since),
            'models': [{'model': m['model'], 'label': m['verbose']}
                       for m in changes.auditable_models()],
        })


class ReportExportView(APIView):
    """GET /api/observability/export/?dataset=<key>&fmt=csv|xlsx — a whole table.

    The screen paginates; an export must not. This builds the full filtered dataset
    server-side and streams one file, so a 40,000-row audit export does not depend
    on the browser having paged through it first.

    Only datasets named in ``EXPORTABLE`` can be requested — caller input selects
    from a fixed registry, it never becomes a query.

    The parameter is ``fmt`` and not ``format`` on purpose: DRF reserves ``format``
    for content negotiation and raises 404 for a value with no matching renderer,
    so ``?format=csv`` would never reach this method.
    """

    def get(self, request: Request):
        denied = _admin_or_403(request)
        if denied:
            return denied

        qp = request.query_params
        key = qp.get('dataset') or ''
        builder = datasets.EXPORTABLE.get(key)
        if builder is None:
            return Response(
                {'error': {'status': 400,
                           'detail': f'Unknown dataset. Available: {", ".join(sorted(datasets.EXPORTABLE))}.'}},
                status=status.HTTP_400_BAD_REQUEST)

        fmt = (qp.get('fmt') or 'csv').lower()
        if fmt not in {'csv', 'xlsx'}:
            return Response({'error': {'status': 400, 'detail': 'fmt must be csv or xlsx.'}},
                            status=status.HTTP_400_BAD_REQUEST)

        try:
            dataset = builder(**_export_kwargs(key, qp))
        except Exception as exc:                             # noqa: BLE001
            return Response({'error': {'status': 500,
                                       'detail': f'Could not build the export: {type(exc).__name__}: {exc}'}},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)

        if fmt == 'xlsx':
            payload = tabular.to_xlsx(dataset)
            content_type = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        else:
            payload = tabular.to_csv(dataset)
            content_type = 'text/csv; charset=utf-8'

        response = HttpResponse(payload, content_type=content_type)
        response['Content-Disposition'] = f'attachment; filename="{tabular.filename(dataset, fmt)}"'
        response['X-C360-Row-Count'] = str(dataset['row_count'])
        return response


def _export_kwargs(key: str, qp) -> dict:
    """Translate query parameters into the builder's arguments, per dataset.

    Explicit per dataset rather than passing the query string through, so an
    unexpected parameter can never reach a builder that would treat it as a filter.
    """
    window = max(5, min(43_200, _int(qp.get('window')) or 1440))   # 5 min … 30 days
    since, until = _since_from(qp), _parse_dt(qp.get('until'))
    if key == 'activity':
        return {'since': since, 'until': until, 'kind': qp.get('kind', ''),
                'status': qp.get('status', ''), 'q': qp.get('q', ''),
                'user': qp.get('user', '')}
    if key == 'changes':
        return {'since': since, 'until': until, 'model': qp.get('model', ''),
                'username': qp.get('user', ''), 'action': qp.get('action', ''),
                'q': qp.get('q', '')}
    if key in {'traffic', 'routes', 'errors', 'users'}:
        return {'window_minutes': window}
    return {}


def _since_from(qp):
    """The start of the requested window.

    A caller may pass an absolute ``since``, or a relative ``window`` in minutes.
    Relative is preferred by the UI: the rows are stamped with the SERVER's clock,
    so resolving "the last 24 hours" here avoids a browser whose clock is off
    quietly returning the wrong slice.
    """
    explicit = _parse_dt(qp.get('since'))
    if explicit is not None:
        return explicit
    minutes = _int(qp.get('window'))
    if minutes and minutes > 0:
        return timezone.now() - timedelta(minutes=min(minutes, 525_600))   # cap at a year
    return None


def _parse_dt(value):
    if not value:
        return None
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    # A naive timestamp from the client is interpreted in the server's timezone
    # rather than silently compared against aware values (which raises).
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _audit_row(e: AuditEvent) -> dict:
    return {
        'id': e.id, 'ts': e.ts.isoformat(), 'user_id': e.user_id, 'username': e.username,
        'kind': e.kind, 'method': e.method, 'route': e.route, 'path': e.path,
        'status': e.status, 'duration_ms': e.duration_ms, 'target': e.target,
        'ip': e.ip, 'session': e.session, 'meta': e.meta,
    }


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
