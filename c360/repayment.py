"""How a customer has repaid each loan, rebuilt from the daily loan-book snapshots.

``eom_loans`` holds one row per loan per business day, with the balance, the amount
overdue and the days overdue. Reading those rows in date order gives the track record
nobody has to key in:

* **Paid down** - each fall in the balance from one snapshot to the next. Interest and
  penalties raise the balance between payments, so this is what the customer paid in,
  dated by the snapshot that first showed it, not by the transaction itself.
* **Late instalments** - each unbroken run of days with ``overdue_days > 0``. Its due
  date is the first such day less its days overdue (1 Sep at 3 days -> due 29 Aug,
  checked against the instalment date on 2026-11-02). The days late shown are the most
  the book recorded, so a payment made between two snapshots can be up to a weekend
  later than shown.
* **One-day blips are dropped** - a day whose balance differs from the days either
  side while those two agree is a load glitch (a live loan read 0 for one day).
* **Closed loans leave the snapshot** - a repaid loan simply stops appearing (often
  with a small balance on its last day). ``loan_account`` says it is Closed and gives
  the last transaction date, so the remainder is shown as cleared on that date.

Pure - no SQL, so it runs live and in tests alike.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

#: Balance movements under a shilling are rounding, not payments.
_MIN_MOVE = 1.0


def build(snapshots: list[dict[str, Any]], registry: dict[str, dict[str, Any]],
          *, start: str, end: str) -> dict[str, Any] | None:
    """``snapshots``: {key, account_no, product, d, bal, ov, od, amount} per loan per day.
    ``registry``: key -> {opened, matures, status, last_trx} from ``loan_account``.
    Returns None when the customer had no loan in the window."""
    by_loan: dict[str, list[dict]] = defaultdict(list)
    for r in snapshots:
        if r.get('key') is not None and r.get('d'):
            by_loan[str(r['key'])].append(r)
    if not by_loan:
        return None

    by_loan = {k: _drop_blips(sorted(v, key=lambda x: str(x['d'])), end) for k, v in by_loan.items()}
    loans = [_loan(key, rows, registry.get(key) or {}, end) for key, rows in by_loan.items()]
    loans.sort(key=lambda x: (x['opened'] or x['first_seen']), reverse=True)

    # Loans open at the same time: each loan runs from its first day on the book to the
    # day it closed (or its last day on the book). Intervals, not shared snapshot days,
    # so a missing day on one loan does not hide an overlap.
    edges = sorted([(x['first_seen'], 1) for x in loans if x['first_seen']] +
                   [(x['last_day'], -1) for x in loans if x['first_seen'] and x['last_day']],
                   key=lambda e: (e[0], e[1]))   # a loan closed the day another opens is a rollover, not an overlap
    peak, peak_on, n = 0, None, 0
    for day, step in edges:
        n += step
        if n > peak:
            peak, peak_on = n, day

    late = [e for x in loans for e in x['late']]
    return {
        'from': start, 'to': end,
        'summary': {
            'loans': len(loans),
            'loans_paid_late': sum(1 for x in loans if x['late']),
            'late_instalments': len(late),
            'worst_days_late': max((e['days_late'] for e in late), default=0),
            'in_arrears_now': sum(1 for x in loans if x['days_overdue_now'] > 0),
            'max_open_together': peak,
            'max_open_on': peak_on,
        },
        'loans': loans,
    }


def _loan(key: str, rows: list[dict], reg: dict, end: str) -> dict[str, Any]:
    first, last = rows[0], rows[-1]
    payments: list[dict[str, Any]] = []
    for prev, cur in zip(rows, rows[1:]):
        drop = _f(prev.get('bal')) - _f(cur.get('bal'))
        if drop > _MIN_MOVE:
            payments.append({'date': _d(cur['d']), 'amount': round(drop), 'final': False})

    status = str(reg.get('status') or '').strip().title() or None
    closed = status == 'Closed'
    last_bal = _f(last.get('bal'))
    closed_on = _d(reg.get('last_trx')) if closed else None
    # A closed loan's last snapshot can still show a balance: the payment that cleared
    # it landed after that day, so it is shown on the closing date.
    if closed and last_bal > _MIN_MOVE:
        payments.append({'date': closed_on, 'amount': round(last_bal), 'final': True})

    late = _late_runs(rows)
    if late and late[-1]['cleared'] is None and closed:
        late[-1]['cleared'] = closed_on

    on_book_today = _d(last['d']) == end
    od_now = int(_f(last.get('od'))) if on_book_today else 0
    return {
        'account_no': _clean(first.get('account_no')),
        'product': (_clean(first.get('product')) or 'Loan').title(),
        'opened': _d(reg.get('opened')),
        'matures': _d(reg.get('matures')),
        'first_seen': _d(first['d']),
        'amount': round(max(_f(r.get('amount')) for r in rows)) or round(max(_f(r.get('bal')) for r in rows)),
        'status': 'closed' if closed else ('open' if on_book_today else 'off_book'),
        'closed_on': closed_on,
        'balance_now': round(last_bal) if on_book_today and not closed else 0,
        'paid_down': sum(p['amount'] for p in payments),
        'payments': payments,
        'late': late,
        'worst_days_late': max((e['days_late'] for e in late), default=0),
        'days_overdue_now': od_now,
        'last_day': closed_on or _d(last['d']),
    }


def _drop_blips(rows: list[dict], end: str) -> list[dict]:
    """Drop a day whose balance differs from both neighbours while the neighbours agree:
    a load glitch, not a payment that was reversed the next day."""
    if len(rows) < 3:
        return rows
    keep = [rows[0]]
    for prev, cur, nxt in zip(rows, rows[1:], rows[2:]):
        b0, b1, b2 = _f(prev.get('bal')), _f(cur.get('bal')), _f(nxt.get('bal'))
        if abs(b0 - b2) <= _MIN_MOVE and abs(b1 - b0) > _MIN_MOVE:
            continue
        keep.append(cur)
    keep.append(rows[-1])
    return keep


def _late_runs(rows: list[dict]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    run: list[dict] = []
    for r in rows + [None]:
        if r is not None and _f(r.get('od')) > 0:
            run.append(r)
            continue
        if run:
            head = run[0]
            due = date.fromisoformat(_d(head['d'])) - timedelta(days=int(_f(head.get('od'))))
            out.append({
                'due': due.isoformat(),
                'days_late': int(max(_f(x.get('od')) for x in run)),
                'peak_arrears': round(max(_f(x.get('ov')) for x in run)),
                'cleared': _d(r['d']) if r is not None else None,
            })
            run = []
    return out


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _d(v: Any) -> str | None:
    s = str(v or '')[:10]
    return s if len(s) == 10 and s[:2] in ('19', '20') else None


def _clean(v: Any) -> str | None:
    s = ' '.join(str(v or '').split())
    return s or None
