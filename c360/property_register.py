"""the property register property clients — the part of the group's customer base that never banked.

Customer 360's customer universe has always been ``dim_customer``: the core-banking
master. the property register (the property development arm) keeps its own client register, and most
of its buyers are not core-banking customers at all. Measured against the live
warehouse on 2026-09-17. Two universes, both measured, because they answer slightly
different questions and the numbers must never appear to contradict each other:

* **The register** (``hfdi_client_data``, what the Property clients page counts):
  **4,843** clients, of whom **1,013 (21%)** have a matching bank record and
  **3,830** do not. **4,256** of them own at least one unit; **9,708** units in all.
* **Unit owners by national ID** (``rpt_c360_customer_property``): **3,442**
  distinct IDs, **766 (22%)** matched. The lower count is the same population seen
  through a column that some clients share and twelve leave blank.

Either way about four in five bought an HF property and hold no HF bank account, so
Customer 360 had no page for them and search could not find them.

That is not a join defect. Normalising both sides of the bridge — strip punctuation,
upper-case — moves the match from 753 to 755. Two clients. The unmatched IDs are
well-formed (2,061 of eight digits, 410 of seven); they simply have no bank record.

The bridge is weak because the field that would make it exact is empty:
``hfdi_client_data.client_hf_number`` is blank on all 4,843 rows. Until the property register
populates it, the national ID is the only link available, and it can only ever find
the clients who are *also* customers.

So these clients get their own namespace rather than being forced into the bank's.
A property-client id is ``PROP-<client_id>``. The prefix is deliberate: every bank-only
gateway method parses a customer id through ``TrinoWarehouse._cid`` (an ``int()``
cast), which returns ``None`` here — so deposits, loans, bureau and CRM lookups all
decline to answer for an the property register client instead of silently returning a bank
customer's figures. The namespace is the safety mechanism, not just a label.

``hfdi_client_data`` itself is clean, despite carrying event-sourcing columns:
4,843 rows for 4,843 clients, one name and one national ID each, ``event_type`` /
``load_type`` / ``date_partition`` all NULL. Every one of the 4,256 clients that
owns a unit is present in it, so ``client_id`` is a reliable join from the property
table to identity.
"""
from __future__ import annotations

import re

from . import brand
#: Prefix marking an id as the property register's rather than core banking's.
#:
#: Deliberately NOT a brand name. It says what the record is, not which company sold
#: the property, so a rebrand never reaches it. The previous spelling was the entity
#: name, which put a retired brand in every URL and every row of the list.
PREFIX = 'PROP-'

_ID_RE = re.compile(r'^PROP-(\d+)$', re.IGNORECASE)

#: What we call these people on screen. They are customers of the group, so the word
#: "client" alone would be a distinction without a difference to an RM — the segment
#: label has to carry the fact that there is no bank relationship.
SEGMENT_LABEL = brand.PROPERTY_CLIENT_SEGMENT


def format_id(client_id) -> str:
    """``415`` -> ``'PROP-415'``."""
    return f'{PREFIX}{int(float(client_id))}'


def parse_id(cust_id) -> int | None:
    """``'PROP-415'`` -> ``415``; anything else -> ``None``.

    Every caller uses this to decide whether an id belongs to the the property register universe, so
    a bank id and an the property register id can share one route without either being guessed at.
    """
    m = _ID_RE.match(str(cust_id or '').strip())
    return int(m.group(1)) if m else None


def is_hfdi_id(cust_id) -> bool:
    return parse_id(cust_id) is not None


def normalise_idno(value) -> str:
    """Strip punctuation and case from an identity document number.

    Used only to compare an the property register client's national ID against the bank's, never to
    display one. Measured gain over an exact trimmed match: two clients out of
    3,429 — which is the evidence that the 78% gap is real and not cosmetic.
    """
    return re.sub(r'[^A-Za-z0-9]', '', str(value or '')).upper()


def shape_client(row: dict, *, units: dict | None = None, bank: dict | None = None) -> dict:
    """One the property register client, in the same shape ``get_customer`` returns for a bank customer.

    Keeping the contract identical is what lets the existing customer page, scope
    checks and staff sieve run over these records unchanged. The ``hfdi`` key is the
    marker that says this is *not* a bank customer, and every consumer that needs to
    behave differently keys off it rather than sniffing the id.

    ``bank`` is the bridged ``dim_customer`` record when the national ID matched, and
    carries two things that matter: the bank customer id, so the UI can offer the
    real profile instead of this thinner one, and ``is_staff``, so an HF employee who
    bought a property stays behind the same sieve here as everywhere else. When the
    client does not bridge we cannot evaluate the staff rule at all — there is no
    employer, segment or employee id in the property register's register — and the record says so
    rather than asserting ``False``.
    """
    u = units or {}
    b = bank or {}
    name = (row.get('client_name') or '').strip() or None
    idno = (row.get('client_idno') or '').strip() or None
    phone = (row.get('client_phone') or '').strip() or None
    email = (row.get('client_email') or '').strip() or None
    pin = (row.get('client_pin') or '').strip() or None
    cid = int(float(row['client_id']))
    return {
        'cust_id': format_id(cid),
        'name': name,
        'segment': SEGMENT_LABEL,
        'branch': None,
        # No RM: these clients are not allocated to anyone's book. Saying so is the
        # point — an unallocated acquisition target is exactly what this list is for.
        'rm_name': None,
        'sales_code': None,
        'rm_source': None,
        'mobile': phone,
        'email': email,
        'id_no': idno,
        'active': True,
        'is_staff': bool(b.get('is_staff')) if bank else False,
        'staff_evaluated': bool(bank),
        'risk_class': None,
        'crb_status': None,
        'kyc_status': None,
        'relationship_since': None,
        'property_client': {
            'client_id': cid,
            'bank_cust_id': b.get('cust_id'),
            'units': int(u.get('units') or 0),
            'units_value': round(float(u.get('units_value') or 0)),
            'paid_pct': u.get('paid_pct'),
            'projects': u.get('projects') or [],
            'has_pin': bool(pin),
        },
        'bio': {
            'customer_type': brand.PROPERTY_CLIENT_SEGMENT,
            # A Kenyan national ID is all digits. Anything carrying a letter or
            # punctuation is an organisation's registration number ('C.102844',
            # 'CPR/2009/6011', 'BN/2016/447587') — calling those a National ID on
            # screen is simply wrong, and it is wrong on every company.
            'id_type': (None if not idno else
                        ('National ID' if idno.isdigit() else 'Registration number')),
            'id_no': idno,
            'issuing_authority': None,
            'kra_pin_status': f'Held by {brand.PROPERTY}' if pin else None,
            'date_of_birth': None,
            'gender': None,
            'city_of_birth': None,
            'employer': None,
            # hfdi_client_data has a client_address column and it is empty on every
            # one of the 4,843 rows, so there is no address to show.
            'address': None,
            'alt_phone': None,
            'branch': None,
            'account_open_date': None,
        },
    }


def coverage_note(total: int, banked: int) -> str:
    """One plain sentence for the top of the list, built from the real counts."""
    if not total:
        return f'No property clients found in the {brand.PROPERTY} register.'
    unbanked = total - banked
    pct = round(100 * banked / total)
    return (
        f'{total:,} property clients are on the {brand.PROPERTY} register. {banked:,} ({pct}%) also '
        f'hold a bank record and have a full Customer 360 profile; {unbanked:,} do not '
        f'bank with us at all.'
    )
