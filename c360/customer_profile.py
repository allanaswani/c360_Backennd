"""The customer's own record in the core-banking category register, plus what the
bank earned from them.

Categories come from ``customer_category`` decoded through ``generic_detail``: one row
per customer per category code (verified 2026-09-30, no duplicates). Only codes whose
values carry information are surfaced. Checked against the whole book:

* ``CRCAMLC`` - the bank's AML risk rating (LOW 385k / AVERAGE 37k / HIGH 31k). The
  real rating; ``aml_customer.risk_class`` and ``w_dim_customer.aml_status`` are blank.
* ``SECRET`` = POLITICALLY EXPOSED marks a PEP (5 customers).
* ``INCLEVEL`` income band, ``PROFES`` profession, ``PROFLEVL`` employment,
  ``ACTIVITY`` economic sector, ``CUSTYPE`` local / diaspora.

Left out on purpose: ``FAMILY`` (1.01M of 1.12M read MARRIED - a default, not a fact),
``EDULEVEL`` (931k read DIPLOMA) and ``ECONOMY`` (87% OTHERS).

Revenue comes from ``rpt_ceo_all_revenue_trend``: one row per customer per DAY
(its ``month_name`` column is a date) and income category, Jan-Sep 2026. Interest
income and non-funded income (fees and commissions) are earned; interest expense is
what the bank paid the customer on deposits.

Pure - no SQL.
"""
from __future__ import annotations

from typing import Any

# Values that mean "not captured", never shown as a fact.
_PLACEHOLDERS = frozenset({'', 'OTHER', 'OTHERS', 'MIGRATION', 'TO COMPLETE', 'DUMMY',
                           'NO REASON DECLARED', 'NOT CONFIRMED', 'XXXXX', 'OTHERS (SPECIFY)'})

FIELDS = (
    ('aml_risk', 'CRCAMLC', 'AML risk rating'),
    ('income_band', 'INCLEVEL', 'Income band'),
    ('profession', 'PROFES', 'Profession'),
    ('employment', 'PROFLEVL', 'Employment'),
    ('sector', 'ACTIVITY', 'Economic sector'),
    ('residency', 'CUSTYPE', 'Residency'),
)


def _clean(v: Any) -> str | None:
    s = ' '.join(str(v or '').split())
    return None if s.upper() in _PLACEHOLDERS else s


def _nice(s: str | None) -> str | None:
    if s is None:
        return None
    # Numeric bands ('200001 AND ABOVE', '0 - 50000') keep their own wording.
    return s if any(ch.isdigit() for ch in s) else s.capitalize()


def from_categories(rows: list[dict]) -> dict[str, Any]:
    """``rows``: {cat, val}. Returns {aml_risk, pep, income_band, ..., fields:[...]}."""
    by_code = {str(r.get('cat') or '').strip().upper(): r.get('val') for r in rows or []}
    out: dict[str, Any] = {}
    fields = []
    for key, code, label in FIELDS:
        val = _nice(_clean(by_code.get(code)))
        out[key] = val
        if val:
            fields.append({'key': key, 'label': label, 'value': val})
    secret = _clean(by_code.get('SECRET'))
    out['pep'] = bool(secret and secret.upper() == 'POLITICALLY EXPOSED')
    out['fields'] = fields
    return out


def aml_tone(aml_risk: str | None) -> str | None:
    """'neg' for high/AML risk, 'warn' for average, 'pos' for low, None if unknown."""
    if not aml_risk:
        return None
    s = aml_risk.upper()
    if 'HIGH' in s or s.startswith('AML'):
        return 'neg'
    if 'AVERAGE' in s:
        return 'warn'
    if 'LOW' in s:
        return 'pos'
    return None


_REV_KEYS = {'Interest_Income': 'interest_income', 'NFI': 'nfi',
             'Interest_Expenses': 'interest_expense'}


def revenue(rows: list[dict], *, year: int) -> dict[str, Any] | None:
    """``rows``: {month (int), category, value}. None when there is nothing on record."""
    months: dict[int, dict[str, float]] = {}
    for r in rows or []:
        key = _REV_KEYS.get(str(r.get('category') or '').strip())
        m = r.get('month')
        if key is None or m is None:
            continue
        slot = months.setdefault(int(m), {'interest_income': 0.0, 'nfi': 0.0, 'interest_expense': 0.0})
        slot[key] += float(r.get('value') or 0)
    if not months:
        return None
    series = []
    for m in sorted(months):
        s = months[m]
        series.append({'period': f'{year}-{m:02d}-01',
                       'interest_income': round(s['interest_income']),
                       'nfi': round(s['nfi']),
                       'interest_expense': round(s['interest_expense']),
                       'net': round(s['interest_income'] + s['nfi'] - s['interest_expense'])})
    tot = {k: sum(p[k] for p in series) for k in ('interest_income', 'nfi', 'interest_expense', 'net')}
    return {'year': year, 'first_month': min(months), 'last_month': max(months),
            'months': series, 'totals': tot}
