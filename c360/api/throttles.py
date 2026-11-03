"""Sign-in throttles keyed by ACCOUNT, with a loose ceiling per IP.

DRF's ScopedRateThrottle keys an anonymous caller by IP. Every user reaches Django
through the same nginx / Next.js proxy, so the whole bank shared one counter: ten
OTP checks an hour, thirty password checks an hour, for everybody. A multi-user test
on 2026-11-03 used that up and sign-ins started failing with 429.

Brute force is an attack on an account, so that is what the tight limit counts:
one counter per (scope, account). A second, much looser per-IP counter still caps a
single address spraying many accounts.
"""
from __future__ import annotations

from django.core import signing
from rest_framework.throttling import SimpleRateThrottle

MFA_SALT = 'c360.login.mfa'
MFA_TICKET_MAX_AGE = 10 * 60


def _account(request) -> str | None:
    data = getattr(request, 'data', None) or {}
    ident = str(data.get('username') or data.get('email') or '').strip().lower()
    if ident:
        return f'u:{ident}'
    ticket = data.get('ticket')
    if ticket:
        try:
            uid = signing.loads(ticket, salt=MFA_SALT, max_age=MFA_TICKET_MAX_AGE).get('uid')
        except signing.BadSignature:
            return None
        if uid is not None:
            return f'id:{uid}'
    return None


class _ViewScoped(SimpleRateThrottle):
    def __init__(self):
        pass        # rate is read per request from the view, as ScopedRateThrottle does


class AccountRateThrottle(_ViewScoped):
    """``throttle_scope`` rate, counted per account named in the request body."""

    def allow_request(self, request, view):
        self.scope = getattr(view, 'throttle_scope', None)
        if not self.scope:
            return True
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        account = _account(request)
        # No account named: fall back to the caller's address.
        ident = account or f'ip:{self.get_ident(request)}'
        return self.cache_format % {'scope': self.scope, 'ident': ident}


class IpCeilingThrottle(_ViewScoped):
    """``<throttle_scope>_ip`` rate, counted per caller address."""

    def allow_request(self, request, view):
        scope = getattr(view, 'throttle_scope', None)
        if not scope:
            return True
        self.scope = f'{scope}_ip'
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


SIGN_IN_THROTTLES = [AccountRateThrottle, IpCeilingThrottle]
