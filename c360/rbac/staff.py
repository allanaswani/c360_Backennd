"""Staff-customer confidentiality — HF employees' own accounts are admin-only.

A customer who is themselves an HF employee is sensitive: a relationship manager — or
even whole-book management — must never be able to search for, or open the 360 of, a
colleague's account. Only the *admin* tier may (see ``rbac/scoping.py``).

Detection is deliberately **fail-safe**: ANY of several independent signals marks a
record as staff, because under-detecting here leaks exactly the data we are protecting.
The signals are drawn from ``dim_customer`` and every threshold is env-tunable, so the
rule can be tightened against the real warehouse without a code change:

    C360_STAFF_EMPLOYER_PATTERNS  employer text meaning "works for HF" (substring)
    C360_STAFF_SEGMENTS           customer_segment / scheme values that denote staff
    C360_STAFF_USE_EMPID          opt-in: treat a real fk_bankemployeeid as a staff link
    C360_STAFF_EMPID_PLACEHOLDERS  fk_bankemployeeid values that are NOT a real link

``fk_bankemployeeid`` is NOT used as a staff signal by default. In the HFCB warehouse
that column is not an "is an HF employee" link at all — it carries the *creating channel
or officer* code (``KOCE…`` for the Kocela/Whizz mobile channel, ``IAPP…`` for app
onboarding, plus per-officer initials like ``JWM``/``SMW``). Populated on ~91% of the
book, so treating any value as staff hid over a million ordinary customers from every RM
(they saw "Customer not found" on live accounts). The genuine, precise staff marker is
the employer text; the empid rule is therefore opt-in via ``C360_STAFF_USE_EMPID`` and
only makes sense once the field carries a real employee-number format.

The gateway stamps ``is_staff`` on every record it returns (search row, roster row and
full customer) using :func:`is_staff_from_fields`; the query layer then hides those
records from any non-admin caller. Keeping the rule in this one module means the SQL
gateway and the mock never drift.
"""
from __future__ import annotations

import os


def _csv_env(name: str, default: str) -> list[str]:
    raw = os.environ.get(name)
    return [p.strip().upper() for p in (raw if raw is not None else default).split(',') if p.strip()]


def _flag_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


# Employer text that means "works for HF" (case-insensitive substring match).
# NOTE: the match is ``pattern in employer``, so a pattern must be a SUBSTRING of the
# real value — not the other way round. dim_customer.employer stores the short form
# ``HFC`` (not ``HFC LTD``), so ``HFC`` must be listed on its own; it also subsumes
# ``HFCB`` / ``HFC LTD`` / ``HFC LIMITED`` / ``HFC BANK``. Under-listing here leaks a
# colleague's 360 to non-admins, so err toward the broader token.
STAFF_EMPLOYER_PATTERNS = _csv_env(
    'C360_STAFF_EMPLOYER_PATTERNS',
    'HOUSING FINANCE,HF GROUP,HFC,HF BANK,HFDI,HF FOUNDATION,HF CUSTODY,HF INSURANCE',
)
# customer_segment / scheme values that denote a staff scheme (exact match).
STAFF_SEGMENTS = _csv_env('C360_STAFF_SEGMENTS', 'STAFF,STAFF SCHEME,EMPLOYEE,EMPLOYEES')
# fk_bankemployeeid values that are migration/placeholder noise, NOT a real staff link.
STAFF_EMPID_PLACEHOLDERS = set(_csv_env(
    'C360_STAFF_EMPID_PLACEHOLDERS', 'MIG_CIS,MIGCIS,0,NULL,NONE,NA,N/A',
))


def _up(v) -> str:
    return str(v).strip().upper() if v is not None else ''


def is_real_employee_id(v) -> bool:
    """A ``fk_bankemployeeid`` that actually links the customer to a bank employee
    (i.e. not blank and not one of the known migration placeholders)."""
    s = _up(v)
    return bool(s) and s not in STAFF_EMPID_PLACEHOLDERS


def is_staff_from_fields(*, employer=None, segment=None, bank_employee_id=None, explicit=None) -> bool:
    """The single staff rule, fed raw ``dim_customer`` fields. ANY signal → staff.

    Note: ``bank_employee_id`` is only consulted when ``C360_STAFF_USE_EMPID`` is set,
    because in this warehouse the column holds a creating-channel/officer code, not an
    employee link (see module docstring). By default the marker is the employer text.
    """
    if explicit is True:
        return True
    emp = _up(employer)
    if emp and any(pat in emp for pat in STAFF_EMPLOYER_PATTERNS):
        return True
    seg = _up(segment)
    if seg and seg in STAFF_SEGMENTS:
        return True
    if _flag_env('C360_STAFF_USE_EMPID'):
        return is_real_employee_id(bank_employee_id)
    return False


def is_staff_customer(record: dict | None) -> bool:
    """Evaluate the staff rule against a gateway record. Trusts a pre-computed
    ``is_staff`` flag when the gateway stamped one, else derives it from raw fields."""
    if not record:
        return False
    flag = record.get('is_staff')
    if flag is not None:
        return bool(flag)
    bio = record.get('bio') or {}
    return is_staff_from_fields(
        employer=record.get('employer') or bio.get('employer'),
        segment=record.get('segment') or record.get('customer_segment'),
        bank_employee_id=record.get('fk_bankemployeeid') or record.get('bank_employee_id'),
        explicit=record.get('staff'),
    )
