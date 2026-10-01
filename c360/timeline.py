"""The relationship over time: when the customer joined, what they opened and closed,
what they borrowed - one line, newest first.

Sources are the core-banking registers, which keep closed accounts and repaid loans
with their dates (verified 2026-10-01, customer 121669: 88 deposit accounts since 2014
and every loan since 2015). Status codes follow the entry-status convention used
across the registers: 1 = active, 3 = closed.

Repetition is folded so the line stays readable: a product opened 60 times is one
event with a count, and mobile loans become one event per year.

Pure - no SQL.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

_STATUS = {'1': 'Active', '3': 'Closed'}


def _title(s: Any) -> str:
    return ' '.join(str(s or '').split()).title()


def _code(s: Any) -> str:
    return str(s or '').strip().split('.')[0]


def _is_mobile(product: str) -> bool:
    p = product.upper()
    return 'MOBILE LOAN' in p or 'MWANABIASHARA' in p or p.startswith('WHIZZ')


def build(*, joined: str | None, deposits: list[dict], loans: list[dict],
          whizz_registered: str | None = None) -> dict[str, Any] | None:
    """``deposits``: {product, opened, closed, status}. ``loans``: {product, opened,
    matures, status, amount}. Dates are ISO strings (placeholders already removed)."""
    events: list[dict[str, Any]] = []
    # The customer master's opening date can be a migration date (121669: 2016, while
    # his first account in the register opened in July 2014), so the relationship
    # starts at the earliest dated record of any kind.
    dated = [d['opened'] for d in deposits if d.get('opened')] +             [l['opened'] for l in loans if l.get('opened')]
    if dated:
        joined = min([joined] + dated) if joined else min(dated)

    # Deposit products, one event per product.
    by_product: dict[str, list[dict]] = defaultdict(list)
    for d in deposits:
        if d.get('opened'):
            by_product[_title(d.get('product')) or 'Account'].append(d)
    for prod, rows in by_product.items():
        rows.sort(key=lambda r: r['opened'])
        n = len(rows)
        open_now = sum(1 for r in rows if _code(r.get('status')) == '1')
        if n == 1:
            r = rows[0]
            detail = 'still open' if open_now else (
                f"closed {r['closed']}" if r.get('closed') else 'closed')
            title = f'Opened {prod}'
        else:
            detail = (f'{n} opened since then, {open_now} still open' if open_now
                      else f'{n} opened since then, none open now')
            title = f'First {prod}'
        events.append({'date': rows[0]['opened'], 'kind': 'account', 'title': title,
                       'detail': detail, 'open': bool(open_now)})

    # Loans: each facility on its own, mobile loans folded by year.
    mobile_by_year: dict[str, dict[str, Any]] = {}
    for l in loans:
        if not l.get('opened'):
            continue
        prod = _title(l.get('product')) or 'Loan'
        amount = float(l.get('amount') or 0)
        if _is_mobile(prod):
            y = l['opened'][:4]
            m = mobile_by_year.setdefault(y, {'n': 0, 'total': 0.0, 'first': l['opened']})
            m['n'] += 1
            m['total'] += amount
            m['first'] = min(m['first'], l['opened'])
            continue
        status = _STATUS.get(_code(l.get('status')), None)
        parts = [f'KES {amount:,.0f} facility' if amount else None,
                 # Some lines carry a placeholder maturity (an FCY overdraft: 2107).
                 (f"matures {l['matures']}" if l.get('matures') and status == 'Active'
                  and l['matures'][:4] < '2100' else None),
                 status.lower() if status else None]
        events.append({'date': l['opened'], 'kind': 'loan', 'title': f'Took {prod}',
                       'detail': ', '.join(p for p in parts if p), 'open': status == 'Active'})
    for y, m in mobile_by_year.items():
        events.append({'date': m['first'], 'kind': 'mobile_loan',
                       'title': f"{m['n']} mobile loan{'s' if m['n'] != 1 else ''} in {y}",
                       'detail': f"KES {m['total']:,.0f} approved in total", 'open': False})

    if whizz_registered:
        events.append({'date': whizz_registered, 'kind': 'digital', 'title': 'Registered on Whizz',
                       'detail': None, 'open': True})
    if joined:
        events.append({'date': joined, 'kind': 'joined', 'title': 'Became a customer',
                       'detail': None, 'open': True})

    if not events:
        return None
    events.sort(key=lambda e: (e['date'], e['kind'] != 'joined'), reverse=True)
    years = defaultdict(int)
    for e in events:
        years[e['date'][:4]] += 1
    first = min(e['date'] for e in events)
    return {
        'events': events,
        'since': joined or first,
        'per_year': [{'year': y, 'events': years[y]} for y in sorted(years)],
        'counts': {
            'accounts_opened': len(deposits),
            'accounts_open': sum(1 for d in deposits if _code(d.get('status')) == '1'),
            'loans_taken': sum(1 for l in loans if not _is_mobile(_title(l.get('product')))),
            'mobile_loans': sum(m['n'] for m in mobile_by_year.values()),
        },
    }
