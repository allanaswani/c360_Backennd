"""API views — the thin HTTP surface over the services and engine.

Every view resolves the caller's scope first (RBAC at the query layer), then reads
through the gateway. No business logic here; views orchestrate and serialise.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ..rbac.scoping import customer_visible, resolve_scope, staff_hidden
from ..recommendations.engine import recommend_for_customer, worklist_across_customers
from ..services.customer import build_customer_header, build_linked_parties, build_value_summary, relationship_summary
from ..services.domains import DOMAIN_BUILDERS
from ..services.hfcb import build_hfcb_domain
from ..services.overview import build_customer_overview
from ..services.portfolio import build_portfolio_overview
from ..services import portfolio_cache
from ..warehouse.factory import data_mode, get_gateway
from ..warehouse.periods import PRESETS, resolve_period


def _period_from_request(request: Request, gateway):
    return resolve_period(
        request.query_params.get('period'),
        as_of=gateway.as_of_date(),
        start=request.query_params.get('start'),
        end=request.query_params.get('end'),
    )


def _not_found():
    return Response({'error': {'status': 404, 'detail': 'Customer not found.'}},
                    status=status.HTTP_404_NOT_FOUND)


def _resolve_or_hide(gateway, scope, cust_id):
    """Fetch a customer, treating out-of-reach records as not-found. Returns the raw
    record, or a 404 Response when the customer is unknown OR is an HF-staff account the
    caller may not see. Staff records return 404 (not 403) so their existence is never
    confirmed to a non-admin snooping by id."""
    raw = gateway.get_customer(cust_id)
    if raw is None or staff_hidden(scope, raw):
        return None, _not_found()
    return raw, None


class MetaView(APIView):
    """Bootstrap payload — data mode, as-of date, presets, resolved scope.
    Stays open (the sign-in screen reads it before a session exists)."""

    permission_classes = [AllowAny]

    def get(self, request: Request):
        gateway = get_gateway()
        scope = resolve_scope(request)
        return Response({
            'data_mode': data_mode(),
            'as_of': gateway.as_of_date().isoformat(),
            'period_presets': list(PRESETS),
            'scope': {
                'role': scope.role,
                'whole_book': scope.is_whole_book,
                'can_view_portfolio': scope.can_view_portfolio(),
            },
            'provenance_legend': {
                'live': 'Live data — from queries running today.',
                'preview': 'Preview data — simulated until the source series is built.',
                'to_source': 'Not yet sourced — pending a warehouse feed.',
            },
        })


class CustomerListView(APIView):
    """Searchable, scoped customer table — the bridge into Level 2."""

    def get(self, request: Request):
        gateway = get_gateway()
        scope = resolve_scope(request)
        rows = gateway.search_customers(
            request.query_params.get('q', ''),
            sales_codes=scope.sales_codes,
            limit=int(request.query_params.get('limit', 25)),
            include_staff=scope.is_superuser,   # staff customers are superuser-only
        )
        return Response({'count': len(rows), 'results': rows})


class CustomerDetailView(APIView):
    """Level 2 landing payload — header + cross-domain value summary."""

    def get(self, request: Request, cust_id: str):
        gateway = get_gateway()
        scope = resolve_scope(request)
        raw, hidden = _resolve_or_hide(gateway, scope, cust_id)
        if hidden:
            return hidden
        if not customer_visible(scope, raw):
            return Response({'error': {'status': 403, 'detail': 'Outside your book.'}},
                            status=status.HTTP_403_FORBIDDEN)
        header = build_customer_header(gateway, cust_id)
        if header is None:
            return _not_found()
        value_summary = build_value_summary(gateway, cust_id)
        # One plain-language line for the top of the page — composed from the facts
        # already assembled above, so it's testable and never invents a figure.
        header['summary'] = relationship_summary(header, value_summary)
        return Response({
            'header': header,
            'value_summary': value_summary,
        })


class LinkedPartiesView(APIView):
    """Same-person linked records (shared national ID), scoped to what the caller may
    see. Returns 404 for an unknown/staff-hidden primary; an empty body when nothing
    linked is visible."""

    def get(self, request: Request, cust_id: str):
        gateway = get_gateway()
        scope = resolve_scope(request)
        raw, hidden = _resolve_or_hide(gateway, scope, cust_id)
        if hidden:
            return hidden
        if not customer_visible(scope, raw):
            return Response({'error': {'status': 403, 'detail': 'Outside your book.'}},
                            status=status.HTTP_403_FORBIDDEN)
        linked = build_linked_parties(gateway, scope, cust_id)
        return Response(linked or {'count': 0, 'members': []})


class CustomerOverviewView(APIView):
    """Level 2 — cross-domain overview (value-by-domain, relationship trend)."""

    def get(self, request: Request, cust_id: str):
        gateway = get_gateway()
        scope = resolve_scope(request)
        raw, hidden = _resolve_or_hide(gateway, scope, cust_id)
        if hidden:
            return hidden
        if not customer_visible(scope, raw):
            return Response({'error': {'status': 403, 'detail': 'Outside your book.'}},
                            status=status.HTTP_403_FORBIDDEN)
        period = _period_from_request(request, gateway)
        return Response(build_customer_overview(gateway, cust_id, period))


class HFCBDomainView(APIView):
    """Level 3 — HFCB core-banking domain, period-filtered."""

    def get(self, request: Request, cust_id: str):
        gateway = get_gateway()
        scope = resolve_scope(request)
        raw, hidden = _resolve_or_hide(gateway, scope, cust_id)
        if hidden:
            return hidden
        if not customer_visible(scope, raw):
            return Response({'error': {'status': 403, 'detail': 'Outside your book.'}},
                            status=status.HTTP_403_FORBIDDEN)
        period = _period_from_request(request, gateway)
        return Response(build_hfcb_domain(gateway, cust_id, period))


class DomainView(APIView):
    """Level 3 — a non-core domain (whizz / properties / bancassurance), period-filtered."""

    def get(self, request: Request, cust_id: str, domain: str):
        builder = DOMAIN_BUILDERS.get(domain)
        if builder is None:
            return Response({'error': {'status': 404, 'detail': f'Unknown domain: {domain}'}},
                            status=status.HTTP_404_NOT_FOUND)
        gateway = get_gateway()
        scope = resolve_scope(request)
        raw, hidden = _resolve_or_hide(gateway, scope, cust_id)
        if hidden:
            return hidden
        if not customer_visible(scope, raw):
            return Response({'error': {'status': 403, 'detail': 'Outside your book.'}},
                            status=status.HTTP_403_FORBIDDEN)
        period = _period_from_request(request, gateway)
        payload = builder(gateway, cust_id, period)
        return Response(payload)


class RecommendationsView(APIView):
    """Next Best Product — Level 2 per-customer panel."""

    def get(self, request: Request, cust_id: str):
        gateway = get_gateway()
        scope = resolve_scope(request)
        raw, hidden = _resolve_or_hide(gateway, scope, cust_id)
        if hidden:
            return hidden
        if not customer_visible(scope, raw):
            return Response({'error': {'status': 403, 'detail': 'Outside your book.'}},
                            status=status.HTTP_403_FORBIDDEN)
        result = recommend_for_customer(gateway, cust_id, limit=int(request.query_params.get('limit', 3)))
        return Response(result.to_dict())


class PortfolioOverviewView(APIView):
    """Level 1 — portfolio overview. Served through the precompute cache so the
    whole-book aggregation never runs live on every request (architecture §6)."""

    def get(self, request: Request):
        gateway = get_gateway()
        scope = resolve_scope(request)
        if scope.role == 'rm' and not scope.sales_codes:
            return Response({'error': {'status': 403, 'detail': 'No book allocated to this user.'}},
                            status=status.HTTP_403_FORBIDDEN)
        period = _period_from_request(request, gateway)
        key = f'overview:{portfolio_cache.scope_key(scope.sales_codes)}:{period.token}:{period.start}:{period.end}:{data_mode()}'
        payload, cache_meta = portfolio_cache.get_or_build(
            key, lambda: build_portfolio_overview(gateway, scope.sales_codes, period)
        )
        return Response({**payload, 'scope': {**payload['scope'], 'role': scope.role}, 'cache': cache_meta})


class WorklistView(APIView):
    """Level 1 cross-sell worklist — recommendation output ranked across the book."""

    def get(self, request: Request):
        gateway = get_gateway()
        scope = resolve_scope(request)
        # Whole-book portfolio access is tighter than a single 360.
        if not scope.can_view_portfolio() and scope.sales_codes is None:
            return Response({'error': {'status': 403, 'detail': 'Portfolio view requires management access.'}},
                            status=status.HTTP_403_FORBIDDEN)
        # Live mode re-queries per customer, so cap the pass to the top of the book;
        # cache it like the portfolio overview so it is computed once, not per request.
        # The worklist batches holdings + risk (a few queries for the whole roster),
        # so it no longer needs a tight per-customer cap; 60 gives a rich call list
        # while keeping the batch query sizes and the cached payload sensible.
        max_customers = 60 if data_mode() == 'live' else None
        # Staff customers are superuser-only, so the cache key is per-lens (superuser vs
        # not) to avoid a superuser's staff-inclusive worklist being served to a manager.
        key = f'worklist:{portfolio_cache.scope_key(scope.sales_codes)}:{data_mode()}:{"s" if scope.is_superuser else "n"}'
        rows = portfolio_cache.get_or_build(
            key,
            lambda: worklist_across_customers(
                gateway, sales_codes=scope.sales_codes, max_customers=max_customers,
                include_staff=scope.is_superuser),
        )[0]
        return Response({'count': len(rows), 'results': rows})


def _acting_sales_code(request) -> str | None:
    """The signed-in user's own sales code (their book). Falls back to the dev header
    used for header-driven RBAC when there's no authenticated profile."""
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        code = getattr(getattr(user, 'profile', None), 'sales_code', None)
        if code:
            return str(code)
    raw = request.headers.get('X-C360-Sales-Codes', '') or ''
    first = raw.split(',')[0].strip()
    return first or None


class BookSummaryView(APIView):
    """The caller's own book (or the whole book, for management) — headline totals,
    segment mix and top customers by AUM. Sourced from the reporting Postgres; returns
    an honest ``available: false`` when that feed isn't wired (mock / Postgres down)."""

    def get(self, request: Request):
        scope = resolve_scope(request)
        sales_code = _acting_sales_code(request)
        whole = scope.can_view_portfolio() and not sales_code
        if not whole and not sales_code:
            return Response({'available': False,
                             'detail': 'No sales code on your profile yet — ask an admin to set it so your book can load.'})
        summary = get_gateway().get_book_summary(None if whole else sales_code)
        if not summary:
            return Response({'available': False,
                             'detail': 'Book analytics need the live reporting warehouse — not available in preview mode.'})
        return Response({'available': True, **summary})


_HEALTH_CAPTURE_MIN_GAP = timedelta(minutes=10)   # don't write a row on every refresh
_HEALTH_HISTORY_LIMIT = 60                        # points charted on the trend graphs


def _capture_health_snapshot(report: dict) -> None:
    """Store one health snapshot, throttled, so the admin page accrues a time series.
    Live mode only; never lets a persistence hiccup break the health view."""
    if report.get('data_mode') != 'live':
        return
    from ..models import HealthSnapshot
    try:
        last = HealthSnapshot.objects.first()   # newest (Meta ordering -captured_at)
        if last and (timezone.now() - last.captured_at) < _HEALTH_CAPTURE_MIN_GAP:
            return
        days = (report.get('freshness') or {}).get('days_behind')
        HealthSnapshot.objects.create(days_behind=days, payload=report)
    except Exception:
        pass


def _health_history() -> list[dict]:
    """Recent snapshots in chronological order, compacted for charting: freshness and,
    per source, its value / status / latency at each capture."""
    from ..models import HealthSnapshot
    try:
        snaps = list(HealthSnapshot.objects.all()[:_HEALTH_HISTORY_LIMIT])
    except Exception:
        return []
    out = []
    for sn in reversed(snaps):   # oldest → newest for a left-to-right line
        checks = {c.get('key'): {'value': c.get('value'), 'status': c.get('status'),
                                 'latency_ms': c.get('latency_ms')}
                  for c in (sn.payload.get('checks') or [])}
        out.append({'at': sn.captured_at.isoformat(), 'days_behind': sn.days_behind, 'checks': checks})
    return out


class DataHealthView(APIView):
    """Admin-only live-warehouse health — freshness, table reachability and row counts.
    Surfaces the exact conditions that have silently broken the app before (a frozen
    as-of date, an emptied source table) so ops sees them before a user does."""

    def get(self, request: Request):
        scope = resolve_scope(request)
        if not scope.is_admin:
            return Response({'error': {'status': 403, 'detail': 'Data health is admin-only.'}},
                            status=status.HTTP_403_FORBIDDEN)
        report = get_gateway().health_report()
        report['data_mode'] = data_mode()
        report['generated_at'] = timezone.now().isoformat()
        # Persist a throttled snapshot so the page can chart trends, then attach history.
        _capture_health_snapshot(report)
        report['history'] = _health_history()
        return Response(report)
