"""Derived risk & KYC — computed from data we already hold, no dedicated feed.

The warehouse has no populated risk-classification column (``aml_customer.risk_class``
is blank) and there is no "KYC status" column at all. But both are *derivable* from
live data, which is the honest way to fill the gap:

* **KYC** is completeness-of-identity. A customer who has been through KYC has a
  valid national ID, a KRA PIN, and reachable contacts on file. So we score the
  identity attributes we already store on ``dim_customer`` and classify:
  Verified / Partial / Incomplete. No KYC column required.

* **Risk** is operational/credit exposure. We read it from loan performance (the
  loan classification on ``eom_loans``) and balance-sheet leverage (loans vs
  deposits), nudged by KYC completeness. Low / Medium / High.

These are pure functions over already-fetched inputs so they are trivially testable
and identical in mock and live mode. They are *derived* judgements, not an external
rating (a CRB bureau score, say, still genuinely requires a feed we don't have), so
callers badge them ``DERIVED`` and surface the factor breakdown, never passing them
off as an authoritative source value.
"""
from __future__ import annotations

import re
from typing import Any

# --- KYC ---------------------------------------------------------------------
# Weighted identity checks. National ID dominates (it is the KYC anchor); the rest
# are corroborating contactability / completeness signals. Weights sum to 1.0.
_KYC_WEIGHTS = {
    'national_id': 0.34,
    'kra_pin': 0.20,
    'mobile': 0.18,
    'date_of_birth': 0.12,
    'address': 0.09,
    'email': 0.07,
}
_KYC_VERIFIED_AT = 0.80   # ID present + strong corroboration
_KYC_PARTIAL_AT = 0.50

_PLACEHOLDER_IDS = {'', 'N.A.', 'NA', 'NULL', 'NONE', '0', '000000000', 'PENDING'}
_KENYA_MOBILE = re.compile(r'^(?:\+?254|0)?7\d{8}$')


def _valid_national_id(raw: Any) -> bool:
    if raw is None:
        return False
    v = str(raw).strip().upper()
    if v in _PLACEHOLDER_IDS or len(v) < 5:
        return False
    # Must carry at least a few digits and not be a single repeated char.
    digits = sum(c.isdigit() for c in v)
    return digits >= 4 and len(set(v)) > 1


def _valid_mobile(raw: Any) -> bool:
    if raw is None:
        return False
    v = re.sub(r'[\s\-()]', '', str(raw).strip())
    return bool(_KENYA_MOBILE.match(v))


def _valid_email(raw: Any) -> bool:
    if raw is None:
        return False
    v = str(raw).strip()
    return '@' in v and '.' in v.split('@')[-1] and len(v) >= 6


def _kra_ok(raw: Any) -> bool:
    """dim_customer.kra_pin_status — treat an explicit positive as verified.
    Values seen are single-char codes; '1'/'Y'/'A'/'VERIFIED' read as present."""
    if raw is None:
        return False
    v = str(raw).strip().upper()
    return v in {'1', 'Y', 'YES', 'A', 'ACTIVE', 'VERIFIED', 'V', 'T'}


def derive_kyc(identity: dict[str, Any]) -> dict[str, Any]:
    """Score identity completeness → KYC status + the passing/failing checks.

    ``identity`` keys (any missing → treated as absent): id_no, kra_pin_status,
    mobile, email, date_of_birth, address.
    """
    checks = [
        ('national_id', 'National ID', _valid_national_id(identity.get('id_no'))),
        ('kra_pin', 'KRA PIN', _kra_ok(identity.get('kra_pin_status'))),
        ('mobile', 'Mobile number', _valid_mobile(identity.get('mobile'))),
        ('date_of_birth', 'Date of birth', bool(identity.get('date_of_birth'))),
        ('address', 'Postal address', bool(str(identity.get('address') or '').strip())),
        ('email', 'Email', _valid_email(identity.get('email'))),
    ]
    score = sum(_KYC_WEIGHTS[key] for key, _, ok in checks if ok)
    has_id = checks[0][2]
    if has_id and score >= _KYC_VERIFIED_AT:
        status = 'Verified'
    elif score >= _KYC_PARTIAL_AT:
        status = 'Partial'
    else:
        status = 'Incomplete'
    missing = [label for _, label, ok in checks if not ok]
    note = (f'Derived from {sum(ok for *_ , ok in checks)}/{len(checks)} identity attributes on file'
            + (f' · missing: {", ".join(missing)}' if missing else ' · all present'))
    return {
        'status': status,
        'score': round(score, 3),
        'checks': [{'key': k, 'label': lbl, 'ok': ok} for k, lbl, ok in checks],
        'note': note,
    }


# --- Risk --------------------------------------------------------------------
# Loan-classification keywords → severity. Read from eom_loans loan_status_ind_name
# / final_sub_class (the bank's own IFRS-9 style buckets).
_NPL_TOKENS = ('non-perform', 'non perform', 'substandard', 'doubtful', 'loss', 'default', 'npl', 'write')
_WATCH_TOKENS = ('watch', 'special mention', 'overdue', 'arrears', 'past due', 'delinq')


def _worst_loan_severity(loan_statuses: list[str]) -> tuple[int, str | None]:
    """0 = performing/none, 1 = watch, 2 = non-performing. Returns (severity, label)."""
    worst, label = 0, None
    for raw in loan_statuses:
        if not raw:
            continue
        s = str(raw).strip().lower()
        if any(tok in s for tok in _NPL_TOKENS):
            return 2, str(raw).strip()
        if any(tok in s for tok in _WATCH_TOKENS) and worst < 1:
            worst, label = 1, str(raw).strip()
    return worst, label


def derive_risk(
    loan_statuses: list[str],
    deposits: float,
    loans: float,
    kyc_status: str,
) -> dict[str, Any]:
    """Operational risk class from loan performance + leverage + KYC completeness.

    * A non-performing facility → High outright.
    * A watch/arrears facility → at least Medium.
    * High leverage (loans dwarf deposits, with material absolute exposure) → nudge up.
    * Incomplete KYC → nudge up (an under-verified borrower is riskier to action).
    Customers with no credit exposure default to Low.
    """
    factors: list[str] = []
    severity, worst_label = _worst_loan_severity(loan_statuses)

    if severity == 2:
        factors.append(f'Non-performing facility ({worst_label})')
        base = 2  # High
    elif severity == 1:
        factors.append(f'Facility on watch/arrears ({worst_label})')
        base = 1  # Medium
    elif loans > 0:
        factors.append('All facilities performing')
        base = 0  # Low
    else:
        factors.append('No credit exposure')
        base = 0

    # Leverage: only meaningful with material absolute exposure.
    if loans >= 1_000_000:
        ratio = loans / (deposits + 1.0)
        if ratio >= 8:
            factors.append(f'High leverage — loans {ratio:.0f}× deposits')
            base = min(base + 1, 2)
        elif ratio >= 3:
            factors.append(f'Elevated leverage — loans {ratio:.1f}× deposits')

    if kyc_status != 'Verified':
        factors.append(f'KYC {kyc_status.lower()} — verification gap')
        if base == 0:
            base = 1  # never rate an under-verified customer as Low

    cls = ['Low', 'Medium', 'High'][base]
    return {
        'class': cls,
        'score': round(base / 2, 3),
        'factors': factors,
        'note': 'Derived from loan performance, leverage and KYC completeness — '
                'operational risk, not an external credit-bureau rating.',
    }


def derive_profile(
    identity: dict[str, Any],
    loan_statuses: list[str],
    deposits: float,
    loans: float,
) -> dict[str, Any]:
    """Convenience: full derived risk/KYC profile for one customer."""
    kyc = derive_kyc(identity)
    risk = derive_risk(loan_statuses, deposits, loans, kyc['status'])
    return {'kyc': kyc, 'risk': risk}
