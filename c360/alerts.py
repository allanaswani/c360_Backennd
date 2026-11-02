"""RM alerts - what changed in a book since the RM last looked, so they don't have to
open every customer to find it.

Three events, each read book-wide from the latest snapshots and handed to the RM who
holds the customer:

* **Fell into arrears** - a loan whose ``overdue_days`` is 1 to 7 on the latest loan
  snapshot: an instalment fell due in the last week and is not paid. The call that
  costs least is the one made now, before day 30. Mobile loans are left out: they are
  collected by the digital lending team, not by an RM (``facilities.is_mobile``).
* **Deposits fell sharply** - total deposits at least halved over 30 days from
  KES 1M or more. The quiet way a relationship leaves (``retention.py`` flags the
  slower 90-day drift on the customer page).
* **Deposit maturing** - a fixed or call deposit maturing in the next 14 days, so the
  rollover conversation happens before the money moves.

Pure - no SQL, so it runs live and in tests alike. Staff customers are dropped by the
gateway before they get here.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from .facilities import is_mobile

ARREARS_MAX_DAYS = 7
DROP_WINDOW_DAYS = 30
DROP_MIN_BEFORE = 1_000_000
DROP_MIN_SHARE = 0.5          # fell to half or less
MATURITY_DAYS = 14

KINDS = {
    'arrears': 'Fell into arrears',
    'deposit_drop': 'Deposits fell sharply',
    'maturity': 'Deposit maturing',
}
_ORDER = {'arrears': 0, 'deposit_drop': 1, 'maturity': 2}


def _kes(v: float) -> str:
    return f'KES {round(v):,}'


def _who(r: dict) -> dict[str, Any]:
    return {'cust_id': str(r.get('cust_id')), 'name': r.get('name'), 'segment': r.get('segment'),
            'branch': r.get('branch')}


def build(*, arrears: list[dict], drops: list[dict], maturities: list[dict],
          loan_as_of: str, deposit_as_of: str) -> list[dict[str, Any]]:
    """``arrears``: {cust_id, name, segment, branch, account_no, product, od, overdue, balance}.
    ``drops``: {cust_id, name, segment, branch, before, after, before_on}.
    ``maturities``: rows of ``td_maturities`` (cust_id, name, ..., balance, matures, days_left).
    Returns one alert per event, most urgent first."""
    out: list[dict[str, Any]] = []
    loan_day = date.fromisoformat(loan_as_of)
    for r in arrears or []:
        od = int(r.get('od') or 0)
        if not 1 <= od <= ARREARS_MAX_DAYS or is_mobile(r.get('product')):
            continue
        product = (str(r.get('product') or '').strip() or 'Loan').title()
        due = (loan_day - timedelta(days=od)).isoformat()
        overdue, balance = float(r.get('overdue') or 0), float(r.get('balance') or 0)
        out.append({**_who(r), 'kind': 'arrears', 'date': due, 'amount': round(overdue),
                    'account_no': r.get('account_no'),
                    'title': f'{product}: {od} day{"s" if od != 1 else ""} in arrears',
                    'detail': (f'{_kes(overdue)} fell due on {due} and is unpaid. '
                               f'Loan balance {_kes(balance)}.')})
    for r in drops or []:
        before, after = float(r.get('before') or 0), max(float(r.get('after') or 0), 0.0)
        if before < DROP_MIN_BEFORE or after > before * DROP_MIN_SHARE:
            continue
        pct = round((before - after) / before * 100)
        out.append({**_who(r), 'kind': 'deposit_drop', 'date': deposit_as_of,
                    'amount': round(before - after), 'account_no': None,
                    'title': f'Deposits down {pct}% in {DROP_WINDOW_DAYS} days',
                    'detail': (f'Total deposits fell from {_kes(before)} on {r.get("before_on")} '
                               f'to {_kes(after)} on {deposit_as_of}.')})
    for r in maturities or []:
        days = int(r.get('days_left') or 0)
        if days > MATURITY_DAYS:
            continue
        when = 'today' if days == 0 else f'in {days} day{"s" if days != 1 else ""}'
        out.append({**_who(r), 'kind': 'maturity', 'date': r.get('matures'),
                    'amount': round(float(r.get('balance') or 0)), 'account_no': r.get('account_no'),
                    'title': f'{r.get("product") or "Deposit"} matures {when}',
                    'detail': f'{_kes(float(r.get("balance") or 0))} matures on {r.get("matures")}.'})
    out.sort(key=lambda a: (_ORDER[a['kind']], -a['amount']))
    return out


def for_codes(alerts: list[dict], sales_codes: list[str] | None) -> list[dict]:
    """The alerts in a book. ``None`` is the whole book (management)."""
    if sales_codes is None:
        return list(alerts)
    codes = {str(c) for c in sales_codes}
    return [a for a in alerts if a.get('rm_code') and str(a['rm_code']) in codes]


def summary(alerts: list[dict]) -> list[dict[str, Any]]:
    return [{'kind': k, 'label': label,
             'count': sum(1 for a in alerts if a['kind'] == k),
             'amount': sum(a['amount'] for a in alerts if a['kind'] == k)}
            for k, label in KINDS.items()]
