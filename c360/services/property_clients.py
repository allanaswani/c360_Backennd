"""HFDI property clients — the service layer, and who is allowed to see them.

The register is a customer list the bank does not own. Most of the people on it
(3,830 of 4,843 at the last measurement) have no bank record at all, which means
three things follow that the bank-side services never have to think about:

* **Nobody's book.** A property client has no ``sales_code``, so the ordinary scope
  check fails for every RM — correctly, but with the wrong explanation. "Outside
  your book" implies someone else holds it. These clients are unallocated, and the
  message says so.
* **The staff sieve can only run on the bridged ones.** HFDI's register carries no
  employer, segment or employee id, so for a client with no bank record the staff
  rule has nothing to evaluate. Those rows carry ``staff_evaluated: False`` and the
  list says how many could not be checked, rather than implying all were.
* **It is a sales list, not a directory.** Ordered by holding value, with the
  unbanked filter front and centre, because the reason to open this page at all is
  the 3,830 people who bought an HF property and bank somewhere else.
"""
from __future__ import annotations

from typing import Any

from .. import brand
from ..rbac.scoping import Scope, staff_hidden
from ..warehouse.gateway import WarehouseGateway

#: Keys that exist for filtering and must never reach the client.
_INTERNAL = ('sales_code', 'is_staff', 'staff', 'staff_evaluated', 'employer')


def visible_to(scope: Scope) -> bool:
    """Who may see the property-client universe.

    Whole-book callers only — the admin tier, which already includes ``hfdi_admin``,
    the role that exists for exactly this data. A scoped RM is excluded not because
    the data is sensitive beyond the usual but because none of it is theirs: showing
    an RM 3,830 names they have no claim on is a worklist they cannot action and a
    privacy surface with no purpose. When these clients are allocated to books, this
    is the one function that has to change.
    """
    return scope.sales_codes is None


def _public(client: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in client.items() if k not in _INTERNAL}
    out.pop('bio', None)
    return out


def build_list(gateway: WarehouseGateway, scope: Scope, *, query: str = '',
               limit: int = 50, unbanked_only: bool = False) -> dict[str, Any]:
    """The property-client list for this caller, with the counts that explain it."""
    try:
        rows = gateway.search_property_clients(query, limit=limit, unbanked_only=unbanked_only)
    except Exception:
        rows = []
    # The staff sieve still applies to the ones we could evaluate: an HF employee who
    # bought a property is as confidential here as on their bank record.
    visible = [c for c in rows if not staff_hidden(scope, c)]
    unchecked = sum(1 for c in visible if not c.get('staff_evaluated'))
    try:
        coverage = gateway.property_client_coverage()
    except Exception:
        coverage = None
    return {
        'count': len(visible),
        'results': [_public(c) for c in visible],
        'coverage': coverage,
        # Never a silent omission: say how many rows the staff rule could not be run
        # against, because HFDI's register carries none of the fields it needs.
        'staff_unverified': unchecked,
        'basis': f'{brand.PROPERTY} client register',
    }


def resolve(gateway: WarehouseGateway, scope: Scope, client_id: int) -> tuple[dict | None, str | None]:
    """One property client. Returns ``(record, error_code)``.

    ``error_code`` is ``'not_found'`` or ``'not_allocated'`` — the latter being the
    honest version of a 403 for a record that is in nobody's book.
    """
    try:
        client = gateway.get_property_client(client_id)
    except Exception:
        client = None
    if client is None or staff_hidden(scope, client):
        return None, 'not_found'
    if not visible_to(scope):
        return None, 'not_allocated'
    return client, None
