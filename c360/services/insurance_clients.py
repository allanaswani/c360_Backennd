"""Insurance clients — the service layer, and who is allowed to see them.

The same shape as ``property_clients``, and for the same reasons: this is a customer
list the bank does not own, most of the people on it have no bank record, and none of
them are allocated to anyone's book.

One difference worth stating. The property register is a register and nothing else.
This universe has two tiers: clients the insurance arm holds a record for, and
clients who appear only in its premium receipts because the register never got them.
The second group has a name and a count of payments and nothing more, and the list
says so rather than rendering a row of blanks that reads as broken.
"""
from __future__ import annotations

from typing import Any

from ..rbac.scoping import Scope, staff_hidden
from ..warehouse.gateway import WarehouseGateway

#: Keys that exist for filtering and must never reach the client.
_INTERNAL = ('sales_code', 'is_staff', 'staff', 'staff_evaluated', 'employer')


def visible_to(scope: Scope) -> bool:
    """Who may see the insurance-client universe.

    Whole-book callers only, exactly as for property clients: not because the data is
    more sensitive than a customer record, but because none of it is any one RM's.
    Showing a relationship manager eleven thousand names they have no claim on is a
    worklist they cannot action and a privacy surface with no purpose. When these
    clients are allocated to books, this is the one function that changes.
    """
    return scope.sales_codes is None


def _public(client: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in client.items() if k not in _INTERNAL}
    out.pop('bio', None)
    return out


def build_list(gateway: WarehouseGateway, scope: Scope, *, query: str = '',
               limit: int = 50, unbanked_only: bool = False) -> dict[str, Any]:
    """The insurance-client list for this caller, with the counts that explain it."""
    try:
        rows = gateway.search_insurance_clients(query, limit=limit,
                                                unbanked_only=unbanked_only)
    except Exception:
        rows = []
    # An insurance client who is also an HF employee is as confidential here as on
    # their bank record, so the same sieve applies to the ones we could evaluate.
    visible = [c for c in rows if not staff_hidden(scope, c)]
    unchecked = sum(1 for c in visible if not c.get('staff_evaluated'))
    try:
        coverage = gateway.insurance_client_coverage()
    except Exception:
        coverage = None
    return {
        'count': len(visible),
        'results': [_public(c) for c in visible],
        'coverage': coverage,
        # Never a silent omission: the staff rule needs an employer, segment or
        # employee number and the insurance register carries none of them, so it can
        # only run on clients that bridge to a bank record.
        'staff_unverified': unchecked,
        # How many of these rows exist only as premium receipts, with no client
        # record on the register at all.
        'receipts_only': sum(1 for c in visible if c['insurance'].get('receipts_only')),
        'basis': 'Insurance client register',
        # The register cannot be listed wholesale and searched the same way: a
        # receipt-only client has no client number, so there is no stable key to page
        # them by. They are reachable by search only, and the page must say so rather
        # than appearing to show everything.
        'receipts_only_searchable': not query,
    }


def resolve(gateway: WarehouseGateway, scope: Scope, key: str) -> tuple[dict | None, str | None]:
    """One insurance client. Returns ``(record, error_code)``.

    ``error_code`` is ``'not_found'`` or ``'not_allocated'`` — the latter being the
    honest version of a 403 for a record that is in nobody's book.
    """
    try:
        client = gateway.get_insurance_client(key)
    except Exception:
        client = None
    if client is None or staff_hidden(scope, client):
        return None, 'not_found'
    if not visible_to(scope):
        return None, 'not_allocated'
    return client, None
