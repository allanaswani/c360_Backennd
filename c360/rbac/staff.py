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
    C360_STAFF_EMPID_PLACEHOLDERS  fk_bankemployeeid values that are NOT a real link

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


# Employer text that means "works for HF" (case-insensitive substring match).
STAFF_EMPLOYER_PATTERNS = _csv_env(
    'C360_STAFF_EMPLOYER_PATTERNS',
    'HOUSING FINANCE,HF GROUP,HFC LTD,HFC LIMITED,HFCB,HF BANK,HFDI,HF FOUNDATION,HF CUSTODY,HF INSURANCE',
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
    """The single staff rule, fed raw ``dim_customer`` fields. ANY signal → staff."""
    if explicit is True:
        return True
    emp = _up(employer)
    if emp and any(pat in emp for pat in STAFF_EMPLOYER_PATTERNS):
        return True
    seg = _up(segment)
    if seg and seg in STAFF_SEGMENTS:
        return True
    return is_real_employee_id(bank_employee_id)


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
