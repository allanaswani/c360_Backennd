"""API views — the thin HTTP surface over the services and engine.

Every view resolves the caller's scope first (RBAC at the query layer), then reads
through the gateway. No business logic here; views orchestrate and serialise.
"""
from __future__ import annotations

from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ..rbac.scoping import customer_visible, resolve_scope
from ..recommendations.engine import recommend_for_customer, worklist_across_customers
from ..services.customer import build_customer_header, build_value_summary
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
        )
        return Response({'count': len(rows), 'results': rows})


class CustomerDetailView(APIView):
    """Level 2 landing payload — header + cross-domain value summary."""

    def get(self, request: Request, cust_id: str):
        gateway = get_gateway()
        scope = resolve_scope(request)
        header = build_customer_header(gateway, cust_id)
        if header is None:
            return Response({'error': {'status': 404, 'detail': 'Customer not found.'}},
                            status=status.HTTP_404_NOT_FOUND)
        raw = gateway.get_customer(cust_id)
        if not customer_visible(scope, raw):
            return Response({'error': {'status': 403, 'detail': 'Outside your book.'}},
                            status=status.HTTP_403_FORBIDDEN)
        return Response({
            'header': header,
            'value_summary': build_value_summary(gateway, cust_id),
        })


class CustomerOverviewView(APIView):
    """Level 2 — cross-domain overview (value-by-domain, relationship trend)."""

    def get(self, request: Request, cust_id: str):
        gateway = get_gateway()
        scope = resolve_scope(request)
        raw = gateway.get_customer(cust_id)
        if not raw:
            return Response({'error': {'status': 404, 'detail': 'Customer not found.'}},
                            status=status.HTTP_404_NOT_FOUND)
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
        raw = gateway.get_customer(cust_id)
        if not raw:
            return Response({'error': {'status': 404, 'detail': 'Customer not found.'}},
                            status=status.HTTP_404_NOT_FOUND)
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
        raw = gateway.get_customer(cust_id)
        if not raw:
            return Response({'error': {'status': 404, 'detail': 'Customer not found.'}},
                            status=status.HTTP_404_NOT_FOUND)
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
        raw = gateway.get_customer(cust_id)
        if not raw:
            return Response({'error': {'status': 404, 'detail': 'Customer not found.'}},
                            status=status.HTTP_404_NOT_FOUND)
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
        key = f'worklist:{portfolio_cache.scope_key(scope.sales_codes)}:{data_mode()}'
        rows = portfolio_cache.get_or_build(
            key,
            lambda: worklist_across_customers(
                gateway, sales_codes=scope.sales_codes, max_customers=max_customers),
        )[0]
        return Response({'count': len(rows), 'results': rows})
