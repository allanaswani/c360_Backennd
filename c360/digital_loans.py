"""Digital (Whizz) loans - every loan the customer took through the app, from
``lns_instant_loans``: one row per loan since 2018, all programmes (mobile loan,
top-ups, Mwanabiashara 1/3/6/12-month, school fees), with the amount and the fee
charged at disbursement.

Why not the agreement table: ``eom_agreement`` only types the plain mobile loans as
"MOBILE LOAN", so the facilities card missed customer 8851's KES 100,000 and
KES 90,000 Mwanabiashara loans and showed KES 30,000 as their highest (2026-11-02).

The fee is ``loan_commission`` - 7.2% of the amount on every 2026 loan checked. The
table's ``loan_interest`` is left out: it is 0 on the three-month loans, whose interest
is charged with each instalment, so it would understate what those loans cost.

The amount borrowed is not the customer's limit. Whizz sets the limit and does not
load it into the warehouse, so the screen says what was taken, never what was offered.

Pure - no SQL.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

#: Most recent loans listed on screen; totals always cover every loan.
LIST_LIMIT = 60


def build(rows: list[dict[str, Any]], names: dict[int, str], *, as_of: str) -> dict[str, Any] | None:
    """``rows``: {account_no, opened, due, amount, fee, programme (int)}.
    ``names``: programme id -> product name. None when the customer has no digital loan."""
    loans = []
    for r in rows or []:
        amount = float(r.get('amount') or 0)
        if amount <= 0 or not r.get('opened'):
            continue
        pid = int(r['programme']) if r.get('programme') is not None else None
        loans.append({'account_no': r.get('account_no'), 'opened': r['opened'], 'due': r.get('due'),
                      'amount': round(amount), 'fee': round(float(r.get('fee') or 0)),
                      'programme': _name(names.get(pid) if pid is not None else None, pid)})
    if not loans:
        return None
    loans.sort(key=lambda x: x['opened'], reverse=True)
    year_ago = (date.fromisoformat(as_of) - timedelta(days=365)).isoformat()
    recent = [x for x in loans if x['opened'] > year_ago]

    progs: dict[str, dict[str, Any]] = {}
    for x in loans:                                   # newest first
        p = progs.setdefault(x['programme'], {'programme': x['programme'], 'loans': 0, 'borrowed': 0,
                                              'highest': 0, 'latest_opened': x['opened'],
                                              'latest_amount': x['amount']})
        p['loans'] += 1
        p['borrowed'] += x['amount']
        p['highest'] = max(p['highest'], x['amount'])
    biggest = max(loans, key=lambda x: (x['amount'], x['opened']))
    return {
        'loans_taken': len(loans),
        'first_opened': loans[-1]['opened'],
        'latest': {k: loans[0][k] for k in ('opened', 'amount', 'programme', 'due')},
        'largest': {k: biggest[k] for k in ('opened', 'amount', 'programme')},
        'borrowed_total': sum(x['amount'] for x in loans),
        'fees_total': sum(x['fee'] for x in loans),
        'last_12m': {'loans': len(recent), 'borrowed': sum(x['amount'] for x in recent),
                     'fees': sum(x['fee'] for x in recent),
                     'highest': max((x['amount'] for x in recent), default=0)},
        'programmes': sorted(progs.values(), key=lambda p: p['latest_opened'], reverse=True),
        'loans': loans[:LIST_LIMIT],
        'listed': min(len(loans), LIST_LIMIT),
    }


def _name(raw: str | None, pid: int | None) -> str:
    s = ' '.join(str(raw or '').split())
    if not s:
        return f'Programme {pid}' if pid is not None else 'Digital loan'
    s = s.title().replace(' - ', '-').replace('- ', '-')
    return s
