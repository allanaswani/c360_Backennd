"""Related-party vocabulary and shaping — shared by the live and mock gateways.

The curated Postgres carries a real related-party register (``public.relationship``,
42,263 rows): directional ``origin_customer → related_customer`` edges, each with a
``relationship_type``. It answers the question the lakehouse cannot — who are this
company's directors and signatories, which companies does this person sit on —
because ``dim_customer`` has only a free-text ``employer`` field and an occupation
picklist, neither of which names a role at a specific organisation.

Verified against the live table rather than assumed:

* 23 distinct types. ``REPRESENTS`` is 84% of rows (35,395), then ``DIRECTOR``
  (3,808), ``PARENT_CHILD`` (886), ``SIGNATORY`` (568), and a long tail.
* Types are **space-padded** (``'DIRECTOR    '``), so every read must trim.
* ``origin_customer`` / ``related_customer`` are varchar but 100% numeric,
  length 3–7, so they cast cleanly to ``dim_customer.customer_id``.
* ``expiry_date`` is varchar and blank on 42,096 of 42,263 rows. The 167 that carry
  one are closed and are excluded.

One thing is **not** verified: which column the organisation sits in. Whether
``(origin, related, DIRECTOR)`` reads "related is a director of origin" or the
reverse cannot be told from the shapes alone, and it is not inferable from this
laptop — the curated Postgres is on a LAN segment we cannot reach. So the edge is
carried with its direction intact and the UI states only what is certain: which
side of the pair the register filed the entry under. It never renders a sentence
that would be backwards if the convention turns out the other way round.

To settle it, compare fan-out per side on a role only an organisation can hold::

    SELECT 'origin' side, round(avg(n),2) avg_ties, max(n) max_ties FROM (
        SELECT origin_customer, count(*) n FROM public.relationship
         WHERE trim(relationship_type)='DIRECTOR' GROUP BY 1) a
    UNION ALL
    SELECT 'related', round(avg(n),2), max(n) FROM (
        SELECT related_customer, count(*) n FROM public.relationship
         WHERE trim(relationship_type)='DIRECTOR' GROUP BY 1) b;

The side with the high fan-out is the company (a company has many directors; a
person holds few directorships). Aggregates only — no customer data leaves the box.
"""
from __future__ import annotations

#: Roles describing a position in an ORGANISATION. These are what an RM is looking
#: for: who runs this company, what does this person sit on.
CORPORATE_ROLES = frozenset({
    'DIRECTOR', 'SIGNATORY', 'PARTNER', 'PROPRIETOR', 'TRUSTEE', 'MANAGER',
    'GUARANTOR', 'NOMINEE', 'AGENT', 'ASSOCIATE', 'REPRESENTS', 'ADMINISTERS',
    'POA', 'EMPLOYEE', 'GROUP', 'SCHEME_MEMB',
})

#: Family and personal ties. Present in the register, and deliberately NOT promoted
#: on a banking screen: they are personal data about third parties with no bearing
#: on a credit or sales decision. Carried through flagged so the UI can hold them
#: back, rather than silently dropped — the count would then not add up.
PERSONAL_ROLES = frozenset({
    'PARENT_CHILD', 'SON', 'HUSBAND_WIFE', 'SIBLINGS', 'GRANDPARENT',
    'GUARDIAN', 'NEXT_OF_KIN',
})

#: Plain-language labels. The register's own strings are terse and upper-case;
#: these are what an RM should read. Anything unmapped falls back to title-case.
ROLE_LABELS = {
    'DIRECTOR': 'Director',
    'SIGNATORY': 'Signatory',
    'PARTNER': 'Partner',
    'PROPRIETOR': 'Proprietor',
    'TRUSTEE': 'Trustee',
    'MANAGER': 'Manager',
    'GUARANTOR': 'Guarantor',
    'NOMINEE': 'Nominee',
    'AGENT': 'Agent',
    'ASSOCIATE': 'Associate',
    'REPRESENTS': 'Represents',
    'ADMINISTERS': 'Administers',
    'POA': 'Power of attorney',
    'EMPLOYEE': 'Employee',
    'GROUP': 'Group member',
    'SCHEME_MEMB': 'Scheme member',
    'PARENT_CHILD': 'Parent / child',
    'SON': 'Son',
    'HUSBAND_WIFE': 'Spouse',
    'SIBLINGS': 'Sibling',
    'GRANDPARENT': 'Grandparent',
    'GUARDIAN': 'Guardian',
    'NEXT_OF_KIN': 'Next of kin',
}


def normalise(role: str | None) -> str:
    """Trim the register's padding and upper-case. ``'DIRECTOR    '`` → ``'DIRECTOR'``."""
    return (role or '').strip().upper()


def label(role: str) -> str:
    """Plain-language label for a role code."""
    return ROLE_LABELS.get(role, role.replace('_', ' ').title())


def is_corporate(roles) -> bool:
    """True when any role describes a position in an organisation."""
    return any(r in CORPORATE_ROLES for r in roles)


def shape_member(entry: dict, detail: dict | None) -> dict:
    """One related party: the edge plus whatever identity we could resolve.

    ``detail`` is the customer record for the counterparty when it could be looked
    up. It can legitimately be missing — the register may reference a customer
    number that no longer exists in the master — and the row is kept either way, as
    a relationship to an id we cannot name is still a fact about this customer.

    ``sales_code`` and the staff markers are carried through deliberately: the
    caller-side filter in ``services.customer.build_related_parties`` runs the same
    scope and staff sieve as every other route to a customer record, and both read
    those keys. Without them an RM's own related parties would all fail the scope
    test and the panel would silently empty. They are dropped again before the
    payload leaves the service, so neither reaches the client.
    """
    d = detail or {}
    roles = [normalise(r) for r in entry.get('roles', []) if normalise(r)]
    return {
        'cust_id': entry['cust_id'],
        'direction': entry.get('direction'),
        'roles': roles,
        'role_labels': [label(r) for r in roles],
        'name': d.get('name'),
        'segment': d.get('segment'),
        'branch': d.get('branch'),
        'value': d.get('value'),
        'products_held': d.get('products_held'),
        'sales_code': d.get('sales_code'),
        # `is_staff` when the gateway pre-computed it (live), else the raw markers
        # the staff rule derives from (the mock seed carries `staff`).
        'is_staff': d.get('is_staff'),
        'staff': d.get('staff'),
        'employer': d.get('employer'),
        'personal': not is_corporate(roles),
    }


def shape(members: list[dict]) -> dict | None:
    """The payload, or None when there is nothing to show."""
    if not members:
        return None
    # Named counterparties first — an id we could not resolve is the least useful
    # row — then by relationship value.
    members.sort(key=lambda m: (m.get('name') is None, -(m.get('value') or 0)))
    return {
        'basis': 'Related-party register',
        'count': len(members),
        'corporate_count': sum(1 for m in members if not m['personal']),
        'members': members,
    }
