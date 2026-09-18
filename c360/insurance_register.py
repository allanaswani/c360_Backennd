"""Insurance clients — the other part of the group's customer base that never banked.

The property register gave a home to HFDI's buyers. This does the same for the
insurance arm's clients, and for the same reason: the group has a relationship with
them, and Customer 360's universe was the bank's customer master, so they did not
exist here.

The insurance team named four customers as missing. Three were real and none of them
were reachable:

    Rajaa Stones Ltd         a bank customer whose policies would not resolve
    Susan Wanjiku Kariuki    a bank customer, five namesakes, no linking identifier
    VTN Ventures Limited     on the insurance register, not a bank customer
    Michael Njau Kimani      not on the insurance register either - 29 premium
                             receipts and nothing else

Measured on the live warehouse (2026-09-18):

* ``hfbi_customer_data`` holds **16,952** clients. Of those, 4,413 bridge to a bank
  customer by national ID and 4,490 by phone, so roughly two thirds have no bank
  relationship at all.
* **5,956** of them have no policy row, while ``hfbi_receipt_data`` holds 31,975
  premium receipts. A paid premium is a harder fact than a policy record here.
* **6,392** distinct names appear in the receipts and 5,938 (93%) match the register.
  The remaining ~454 are clients the register never got, and Michael Njau Kimani is
  one of them. Leaving them out would be the third time that name came back missing.

So the universe is the register PLUS the receipt-only names, and the id scheme has
to carry both:

    INS-RA386                a register client, keyed by client_no
    INS-MICHAELNJAUKIMANI    a receipt-only client, keyed by normalised name

``INS-`` is deliberately not a brand name, for the same reason ``PROP-`` is not: the
prefix sits in every URL and outlives whatever the arm is called this year.
"""
from __future__ import annotations

import re

#: Prefix marking an id as the insurance register's rather than core banking's.
PREFIX = 'INS-'

#: A client_no is short and alphanumeric ('RA386', 'VT001'); a name key is longer and
#: letters-only. Both are matched here and told apart by lookup, not by shape, because
#: guessing from shape is how 'VT001' would one day be read as a name.
_ID_RE = re.compile(r'^INS-([A-Za-z0-9]{2,64})$', re.IGNORECASE)

#: How an insurance client's segment reads on screen.
SEGMENT_LABEL = 'Insurance client'


def _person_name(value) -> str | None:
    """A readable name from whatever the register stores for a person.

    The risk-manager column holds an email address. 'Brian.Gumba@hfgroup.co.ke'
    becomes 'Brian Gumba'; anything that is already a name is returned as it is,
    because guessing twice is how a real name gets mangled.
    """
    raw = (value or '').strip()
    if not raw:
        return None
    if '@' not in raw:
        return raw
    local = raw.split('@', 1)[0]
    parts = [p for p in local.replace('_', '.').split('.') if p]
    if not parts:
        return raw
    return ' '.join(p[:1].upper() + p[1:] for p in parts)


def format_id(key) -> str:
    """``'RA386'`` -> ``'INS-RA386'``."""
    return f'{PREFIX}{str(key).strip()}'


def parse_id(cust_id) -> str | None:
    """``'INS-RA386'`` -> ``'RA386'``; anything else -> ``None``.

    Callers use this to decide whether an id belongs to the insurance universe, so a
    bank id and an insurance id can share one route without either being guessed at.
    The returned key is either a client_no or a normalised name; which one it is, is
    settled by looking it up rather than by inspecting it.
    """
    m = _ID_RE.match(str(cust_id or '').strip())
    return m.group(1).upper() if m else None


def is_insurance_id(cust_id) -> bool:
    return parse_id(cust_id) is not None


def shape_client(row: dict, *, policies: dict | None = None, receipts: dict | None = None,
                 bank: dict | None = None, name_key: str | None = None) -> dict:
    """One insurance client, in the shape ``get_customer`` returns for a bank customer.

    Keeping the contract identical is what lets the existing customer page, scope
    checks and staff sieve run over these records unchanged. The ``insurance`` key is
    the marker that says this is not a bank customer.

    A receipt-only client has no ``client_no`` and no identity beyond the name on
    their receipts, which is stated rather than padded out with blanks that look like
    missing data.
    """
    p = policies or {}
    r = receipts or {}
    b = bank or {}
    client_no = (row.get('client_no') or '').strip() or None
    name = (row.get('name') or '').strip() or None
    key = client_no or name_key or ''
    return {
        'cust_id': format_id(key),
        'name': name,
        'segment': SEGMENT_LABEL,
        'branch': (row.get('branch') or '').strip() or None,
        # Not allocated to anyone's book, same as a property client. The register
        # stores its risk manager as an email address, and an address is not a name:
        # the header rendered "RM Brian.Gumba@hfgroup.co.ke".
        'rm_name': _person_name(row.get('risk_manager')),
        'sales_code': None,
        'rm_source': None,
        'mobile': (row.get('phone') or '').strip() or None,
        'email': (row.get('email') or '').strip() or None,
        'id_no': (row.get('idno') or '').strip() or None,
        'active': bool(p.get('active')),
        'is_staff': bool(b.get('is_staff')) if bank else False,
        'staff_evaluated': bool(bank),
        'risk_class': None,
        'crb_status': None,
        'kyc_status': None,
        'relationship_since': None,
        'insurance': {
            'client_no': client_no,
            # Set when this client also banks with us, so the UI can hand over to the
            # richer profile instead of being the worse of the two.
            'bank_cust_id': b.get('cust_id'),
            'policies': int(p.get('total') or 0),
            'active_policies': int(p.get('active') or 0),
            'premium': round(float(p.get('premium') or 0)),
            'sum_insured': round(float(p.get('sum_insured') or 0)),
            # None when the receipts feed holds no row for this client, which is
            # 54% of the register. 0 would mean it holds rows summing to nothing,
            # and that never happens - so a bare 0 reads as 'never paid' about a
            # client the feed simply does not cover.
            'receipts': (None if r.get('receipts') is None
                         else int(r.get('receipts') or 0)),
            # True when the register never got this client and the only evidence they
            # exist is the premiums they have paid.
            'receipts_only': client_no is None,
            'sales_person': (row.get('sales_person') or '').strip() or None,
            'occupation': (row.get('occupation') or '').strip() or None,
        },
        'bio': {
            'customer_type': SEGMENT_LABEL,
            'id_type': (None if not (row.get('idno') or '').strip() else
                        ('National ID' if str(row.get('idno')).strip().isdigit()
                         else 'Registration number')),
            'id_no': (row.get('idno') or '').strip() or None,
            'issuing_authority': None,
            'kra_pin_status': 'Held by the insurance arm' if (row.get('pin') or '').strip() else None,
            'date_of_birth': None,
            'gender': (row.get('gender') or '').strip() or None,
            'city_of_birth': None,
            'employer': None,
            'address': (row.get('address') or '').strip() or None,
            'alt_phone': None,
            'branch': (row.get('branch') or '').strip() or None,
            'account_open_date': None,
        },
    }


def coverage_note(total: int, banked: int, receipts_only: int) -> str:
    """One plain sentence for the top of the list, built from the real counts."""
    if not total:
        return 'No clients found on the insurance register.'
    unbanked = total - banked
    pct = round(100 * banked / total)
    note = (f'{total:,} insurance clients. {banked:,} ({pct}%) also hold a bank record '
            f'and have a full Customer 360 profile; {unbanked:,} do not bank with us.')
    if receipts_only:
        verb = 'appears' if receipts_only == 1 else 'appear'
        note += (f' {receipts_only:,} more {verb} only in the premium receipts, with '
                 f'no client record on the register at all.')
    return note
