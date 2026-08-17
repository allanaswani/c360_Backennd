"""Access scoping — enforced at the query layer, not the UI.

The whole book is more sensitive than a single customer's own 360, so scope is
resolved once per request and passed *into* the gateway as a ``sales_codes`` filter
that the SQL applies (``retail_allocated_portfolio.sales_code``). The frontend
never decides what a user may see; it only renders what the scoped query returns.

Roles
-----
* ``rm``         — sees only their own book (their ``sales_code``s).
* ``management`` — sees the whole book (``sales_codes = None`` → no filter).

Production wiring: role + sales_codes come from the authenticated profile, the same
mechanism the portfolio platform uses. For standalone local dev this reads
explicit request headers so the scoping can be exercised end-to-end without the
shared auth service:

    X-C360-Role:        rm | management        (default: management, dev only)
    X-C360-Sales-Codes: SC-1042,SC-1077        (the RM's own codes)

A real deployment replaces ``resolve_scope`` with a profile lookup; the return
shape stays identical so nothing downstream changes.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..roles import ROLE_ADMIN, ROLE_MANAGER, tier_for_groups
from .staff import is_staff_customer


@dataclass(frozen=True)
class Scope:
    role: str
    sales_codes: list[str] | None   # None => whole book (management)
    is_admin: bool = False          # admin tier only — the sole tier that may see staff

    @property
    def is_whole_book(self) -> bool:
        return self.sales_codes is None

    def can_view_portfolio(self) -> bool:
        # Level 1 (whole-book) needs tighter access than a single 360.
        return self.role == 'management'


def resolve_scope(request) -> Scope:
    # An authenticated login is authoritative: role + book scope come from the
    # user's role Groups (tier) and Profile, never from client-supplied headers.
    # This is the production path.
    user = getattr(request, 'user', None)
    if user is not None and getattr(user, 'is_authenticated', False):
        tier = tier_for_groups(user.groups.values_list('name', flat=True))
        # Staff customers (HF employees) are visible to the admin tier ONLY — not to
        # managers, not to RMs. Superusers and the admin tier (ceo / exco / hfdi_admin)
        # qualify; every other role is blocked from those records at the query layer.
        is_admin = bool(user.is_superuser or tier == ROLE_ADMIN)
        # Management tiers (admin/manager) and superusers get the whole book AND the
        # Level 1 portfolio analytics.
        if user.is_superuser or tier in (ROLE_ADMIN, ROLE_MANAGER):
            return Scope('management', None, is_admin=is_admin)
        # RMs (officer tier): this is Customer 360 — an RM must be able to look up ANY
        # customer's 360, not only their own book. So they get whole-book *view*
        # access (sales_codes=None → no per-customer filter). The 'rm' role still gates
        # them out of the whole-book Level 1 dashboard/worklist (management's remit);
        # their own book (profile.sales_code) remains available as a 'my book' lens.
        return Scope('rm', None, is_admin=False)

    # Unauthenticated: the documented local-dev seam — header-driven scope so RBAC
    # can be exercised without the auth service. Ignored once a user is logged in.
    # X-C360-Admin: true grants the admin lens so the staff sieve is testable too.
    role = (request.headers.get('X-C360-Role') or 'management').strip().lower()
    is_admin = (request.headers.get('X-C360-Admin') or '').strip().lower() in ('1', 'true', 'yes')
    if role == 'rm':
        raw = request.headers.get('X-C360-Sales-Codes', '') or ''
        codes = [c.strip() for c in raw.split(',') if c.strip()]
        return Scope('rm', codes, is_admin=False)
    return Scope('management', None, is_admin=is_admin)


def customer_visible(scope: Scope, customer: dict) -> bool:
    """Is this specific customer within the caller's scope?"""
    if scope.sales_codes is None:
        return True
    return customer.get('sales_code') in set(scope.sales_codes)


def staff_hidden(scope: Scope, customer: dict) -> bool:
    """True when this record is an HF-staff customer the caller may NOT see. Only the
    admin tier is exempt; for everyone else a staff record is treated as non-existent
    (the view returns 404, never a 403, so the account's existence isn't confirmed)."""
    if scope.is_admin:
        return False
    return is_staff_customer(customer)
