"""Request instrumentation — the single point where traffic, latency, errors and the
server-side audit trail are captured. Deliberately thin and defensive: it times the
request, hands the numbers to the in-memory collector (O(1)), and buffers one audit
record. It NEVER does a DB write itself and NEVER lets a telemetry error affect the
response."""
from __future__ import annotations

import time

from django.conf import settings

from . import observability as obs

# Endpoints that are monitoring overhead, not application traffic — excluded from both
# metrics and audit so the dashboard doesn't measure itself.
_SKIP_PREFIXES = ('/api/observability', '/api/telemetry', '/metrics')


def _client_ip(request) -> str | None:
    xff = request.META.get('HTTP_X_FORWARDED_FOR')
    if xff:
        return xff.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR')


def user_from_request(request):
    """(user_id, username) with no DB hit on the hot path: prefer an already-authenticated
    Django user, else decode the JWT claims from the Authorization header (signature check
    only, no query). Returns (None, '') for anonymous/unresolvable."""
    u = getattr(request, 'user', None)
    if u is not None and getattr(u, 'is_authenticated', False):
        return u.id, u.get_username()
    auth = request.META.get('HTTP_AUTHORIZATION', '')
    if auth.startswith('Bearer '):
        try:
            from rest_framework_simplejwt.tokens import AccessToken
            tok = AccessToken(auth[7:])
            return tok.get('user_id'), (tok.get('username') or tok.get('name') or '')
        except Exception:
            return None, ''
    return None, ''


class ObservabilityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response
        self.enabled = getattr(settings, 'OBSERVABILITY_ENABLED', True)

    def __call__(self, request):
        if not self.enabled:
            return self.get_response(request)
        start = time.perf_counter()
        response = self.get_response(request)
        dur_ms = (time.perf_counter() - start) * 1000.0
        try:
            self._observe(request, response, dur_ms)
        except Exception:
            pass  # telemetry must never break a response
        return response

    def _observe(self, request, response, dur_ms: float) -> None:
        path = request.path
        # Only instrument the API surface; skip static, admin console and self-monitoring.
        if not path.startswith('/api/'):
            return
        if any(path.startswith(p) for p in _SKIP_PREFIXES):
            return

        status = getattr(response, 'status_code', 0)
        obs.collector.record(dur_ms, status)   # metrics: in-memory, O(1)

        # Server-side audit: one buffered event per API action ("who saw what, when").
        route = self._route(request)
        uid, uname = user_from_request(request)
        obs.audit_buffer.record(
            user_id=uid, username=uname or '', kind='api',
            method=request.method, route=route, path=path[:300],
            status=status, duration_ms=int(dur_ms),
            target=self._target(request), ip=_client_ip(request),
            session=request.headers.get('X-C360-Session', '')[:64], meta={},
        )

    @staticmethod
    def _route(request) -> str:
        """The normalised URL pattern (bounded cardinality), e.g.
        '/api/customers/:cust_id/' rather than the raw id. Falls back to the raw path."""
        rm = getattr(request, 'resolver_match', None)
        if rm is not None and rm.route:
            r = rm.route
            return ('/' + r) if not r.startswith('/') else r
        return request.path[:200]

    @staticmethod
    def _target(request) -> str:
        """A stable subject for the action when there is one (the customer being viewed),
        so the audit reads 'user X opened customer 12345'."""
        rm = getattr(request, 'resolver_match', None)
        if rm is not None:
            cid = rm.kwargs.get('cust_id') or rm.kwargs.get('pk')
            if cid is not None:
                return str(cid)[:200]
        return ''
