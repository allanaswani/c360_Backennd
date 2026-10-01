"""Money in and money out, by month and by source - from the signed ledger amount.

``fact_dep_trx_recording.o_final_acc_amount`` is what the account actually moved by:
positive in, negative out, fees included, zero on accruals and holds (verified
2026-10-01 across 850k September movements with no sign exceptions). So a month's
money in is the sum of the positive amounts and money out the sum of the negative
ones; nothing has to be inferred about direction.

Grouping is by purpose. On September 2026 the groups below placed all but about
KES 20M of KES 80B moved (0.02%) - "Other" is genuinely small.

What this does not do: separate a customer's own transfers from real income. Moving
money from a current account into a fixed deposit shows as out of one account and
into another. The screen says so next to the figures.

Pure apart from the SQL expression.
"""
from __future__ import annotations

from typing import Any

# Purpose (``j``, upper-cased) and product (``p``) -> group. First match wins.
GROUP_SQL = """CASE
 WHEN j LIKE 'SALARY POSTINGS%' AND j NOT LIKE '%RETURN%' THEN 'Salary'
 WHEN j = 'JOURNAL CREDIT' AND p LIKE '%SALARY%' THEN 'Credits into a salary account'
 WHEN j LIKE '%MPESA%' OR j LIKE 'PAY BILL%' OR j LIKE 'BUY GOOD%' OR j LIKE 'AIRTIME%'
      OR j LIKE 'UTILITY BILL%' OR j LIKE 'CR PAYBILL%' OR j LIKE 'CR BUY GOODS%'
      OR j LIKE '%MOBILE BANKING%' OR j LIKE '%WHIZZPAY%' THEN 'M-Pesa & Whizz'
 WHEN j LIKE '%KITS%' THEN 'PesaLink'
 WHEN j IN ('DEPOSIT CASH', 'CASH DEPOSIT MACHINE (CDM)', 'CASH WITHDRAWAL',
            'OTC CASH WITHDRAWAL') THEN 'Cash'
 WHEN j LIKE '%THROUGH TILL%' THEN 'Till'
 WHEN j LIKE 'V-DB%' THEN 'Card'
 WHEN j LIKE '%CHEQUE%' OR j LIKE 'BANK DRAFT%' OR j LIKE '%CLEARING%' THEN 'Cheques & drafts'
 WHEN j LIKE '%RTGS%' OR j LIKE 'TELEGRAPHIC TRANSFER%' OR j LIKE '%SWIFT%' THEN 'RTGS & wires'
 WHEN j LIKE 'STANDING ORDER PAYMENT%' THEN 'Standing orders'
 WHEN j LIKE '%LOAN%' OR j LIKE 'PRINCIPAL PAYMENT%' THEN 'Loans'
 WHEN j LIKE '%TERM DEPOSIT%' OR j LIKE 'T/D %' THEN 'Fixed deposits'
 WHEN j LIKE '%INTEREST%' THEN 'Interest'
 WHEN j LIKE '%CHARGE%' OR j LIKE '%FEE%' OR j LIKE '%COMMISSION%' OR j LIKE 'PENALTY%'
      OR j LIKE '%LEVY%' OR j LIKE '%EXCISE%' OR j LIKE '%STATEMENT%'
      OR j LIKE 'ACCOUNT MAINTENANCE%' OR j LIKE 'AUDIT CONFIRMATION%'
      OR j LIKE 'BANK REFERENCE%' THEN 'Charges & fees'
 WHEN j LIKE 'JOURNAL%' OR j LIKE 'JC-%' OR j LIKE 'JD-%' OR j LIKE '%TRANSFER%'
      OR j LIKE 'CUSTOMER ACCOUNT DEBIT%' THEN 'Transfers & journals'
 WHEN j LIKE '%INSURANCE%' THEN 'Insurance'
 ELSE 'Other' END"""

OWN_TRANSFER_NOTE = ('Money moved between the customer’s own accounts, such as funding a fixed '
                     'deposit, shows as both out and in.')


def shape(rows: list[dict], months: list[str]) -> dict[str, Any] | None:
    """``rows``: {month 'YYYY-MM-01', grp, inflow, outflow, n_in, n_out}.
    ``months``: every month in the window, oldest first, so a quiet month is a zero
    bar rather than a missing one. None when nothing moved at all."""
    if not rows:
        return None
    by_month = {m: {'period': m, 'in': 0, 'out': 0} for m in months}
    sources: dict[str, dict[str, float]] = {}
    uses: dict[str, dict[str, float]] = {}
    for r in rows:
        m = str(r.get('month'))[:10]
        g = r.get('grp') or 'Other'
        i, o = float(r.get('inflow') or 0), float(r.get('outflow') or 0)
        if m in by_month:
            by_month[m]['in'] += i
            by_month[m]['out'] += o
        if i:
            s = sources.setdefault(g, {'value': 0.0, 'count': 0})
            s['value'] += i
            s['count'] += int(r.get('n_in') or 0)
        if o:
            u = uses.setdefault(g, {'value': 0.0, 'count': 0})
            u['value'] += o
            u['count'] += int(r.get('n_out') or 0)
    series = []
    for m in months:
        b = by_month[m]
        series.append({'period': m, 'in': round(b['in']), 'out': -round(b['out']),
                       'net': round(b['in'] - b['out'])})
    total_in = sum(p['in'] for p in series)
    total_out = -sum(p['out'] for p in series)

    def ranked(d: dict[str, dict[str, float]], total: float) -> list[dict]:
        return sorted(({'group': k, 'value': round(v['value']), 'count': int(v['count']),
                        'share': round(v['value'] / total, 4) if total else 0.0}
                       for k, v in d.items()), key=lambda x: x['value'], reverse=True)

    return {
        'months': series,
        'total_in': round(total_in),
        'total_out': round(total_out),
        'net': round(total_in - total_out),
        'sources': ranked(sources, total_in),
        'uses': ranked(uses, total_out),
        'note': OWN_TRANSFER_NOTE,
    }


def month_starts(first: Any, last: Any) -> list[str]:
    """'YYYY-MM-01' for every month from ``first`` to ``last`` inclusive."""
    y, m = first.year, first.month
    out = []
    while (y, m) <= (last.year, last.month):
        out.append(f'{y:04d}-{m:02d}-01')
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
    return out
