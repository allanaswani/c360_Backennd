"""Level 1 — Portfolio aggregation service.

Answers "where should attention go across the whole book?" Aggregates the visible
customer set (RBAC-scoped) into: headline totals, segment mix, risk distribution,
deposit/loan book trend, segment value trend, and top movers.

Provenance is honest per the whole-app rule:
* totals + segment mix are LIVE — derived from the real per-customer snapshot.
* risk distribution is PREVIEW — risk classification isn't sourced, so the split is
  simulated and clearly flagged (never a fake "classified" claim).
* book trend / segment value trend / top movers are PREVIEW — they need a historical
  snapshot that doesn't exist yet; simulated deterministically and flagged. In
  production these read the nightly precomputed summary table (§6), which is why the
  whole overview is served through ``portfolio_cache`` rather than aggregated live.
"""
from __future__ import annotations

import hashlib
from datetime import date, timedelta
from typing import Any

from ..warehouse.factory import data_mode
from ..warehouse.gateway import WarehouseGateway
from ..warehouse.periods import ResolvedPeriod
from ..warehouse.provenance import Provenance, live

PREVIEW = Provenance.PREVIEW.value
DERIVED = Provenance.DERIVED.value
LIVE = Provenance.LIVE.value
_RISK_CLASSES = ['Low', 'Medium', 'High', 'Unclassified']


def _stream(seed: str) -> 'Stream':
    return Stream(seed)


class Stream:
    def __init__(self, seed: str):
        self._h = hashlib.sha256(seed.encode()).digest()
        self._i = 0

    def next(self) -> float:
        b = self._h[self._i % len(self._h)]
        self._i += 1
        if self._i % len(self._h) == 0:
            self._h = hashlib.sha256(self._h).digest()
        return b / 255.0


def _walk(base: float, slope: float, dates: list[date], seed: str, vol: float = 0.05) -> list[dict]:
    r = _stream(seed)
    n = len(dates)
    out = []
    for i, d in enumerate(dates):
        drift = slope * base * (i / max(n - 1, 1)) * 0.3
        noise = (r.next() - 0.5) * 2 * vol * base
        out.append({'period': d.isoformat(), 'balance': round(max(base * (1 - slope * 0.15) + drift + noise, 0))})
    return out


def _dates(period: ResolvedPeriod) -> list[date]:
    span = max(period.days, 1)
    step = 1 if span <= 31 else (7 if span <= 130 else 30)
    out, cur = [], period.start
    while cur <= period.end:
        out.append(cur)
        cur = cur + timedelta(days=step)
    if out[-1] != period.end:
        out.append(period.end)
    return out


def build_portfolio_overview(gateway: WarehouseGateway, sales_codes: list[str] | None, period: ResolvedPeriod) -> dict[str, Any]:
    # Whole-book live view (management, unscoped): real aggregates over the entire
    # book — no sampling. Falls back to the bounded roster for mock / RM-scoped views.
    if data_mode() == 'live' and sales_codes is None:
        try:
            wb = gateway.portfolio_whole_book(period)
        except Exception:
            wb = None
        if wb:
            return _assemble_whole_book(wb, period)
    return _build_from_roster(gateway, sales_codes, period)


def _assemble_whole_book(wb: dict[str, Any], period: ResolvedPeriod) -> dict[str, Any]:
    total_value = wb['deposits'] + wb['loans']
    return {
        'scope': {
            'whole_book': True,
            'customers_in_view': wb['customers'],
            'live_sample': False,
            'sample_note': None,
        },
        'period': period.to_dict(),
        'summary': {
            'customers': live(wb['customers'], unit='count').to_dict(),
            'active_customers': live(wb.get('active_customers', 0), unit='count').to_dict(),
            'relationship_value': live(total_value, unit='KES').to_dict(),
            'deposits': live(wb['deposits'], unit='KES').to_dict(),
            'loans': live(wb['loans'], unit='KES').to_dict(),
            'avg_products': live(wb['avg_products'], unit='count').to_dict(),
        },
        'segment_mix': {
            'question': 'How many customers per segment, and their combined value?',
            'status': LIVE,
            'rows': wb['segment_mix'],
        },
        'risk_distribution': {
            'question': 'How exposed is the book right now?',
            'status': DERIVED,
            'note': 'Derived across the whole book from balance-sheet leverage (loans vs deposits) — '
                    'a book-level proxy; per-customer risk also factors loan performance & KYC.',
            'rows': wb['risk_rows'],
        },
        'book_trend': {
            'question': 'Is the balance-sheet mix shifting?',
            'status': LIVE,
            'note': 'Real whole-book balances at each daily snapshot over the period.',
            'series': wb['book_trend'],
        },
        'segment_value_trend': {
            'question': 'Which segments are growing vs shrinking?',
            'status': DERIVED,
            'note': 'Real whole-book total per day, split by segment share interpolated between the '
                    'period-start and current snapshots (the full per-day segment scan exceeds cluster memory).',
            'segments': wb['segments'],
            'data': wb['segments_data'],
        },
        'top_movers': {
            'question': 'Who needs attention this period?',
            'status': LIVE,
            'note': 'Largest whole-book value change vs the previous month-end.',
            'rows': wb['movers'],
        },
        'top_products': {
            'question': 'Which products actually hold the book?',
            'status': LIVE,
            'rows': wb.get('top_products', []),
        },
    }


def _build_from_roster(gateway: WarehouseGateway, sales_codes: list[str] | None, period: ResolvedPeriod) -> dict[str, Any]:
    customers = gateway.list_customers(sales_codes=sales_codes)
    dates = _dates(period)

    # ---- headline totals (LIVE) ----------------------------------------
    n = len(customers)
    tot_value = sum(c.get('value') or 0 for c in customers)
    tot_dep = sum(c.get('deposits') or 0 for c in customers)
    tot_loan = sum(c.get('loans') or 0 for c in customers)
    avg_products = round(sum(c.get('products_held') or 0 for c in customers) / n, 1) if n else 0

    # ---- segment mix (LIVE) --------------------------------------------
    seg: dict[str, dict] = {}
    for c in customers:
        s = seg.setdefault(c['segment'], {'segment': c['segment'], 'customers': 0, 'value': 0, 'deposits': 0, 'loans': 0})
        s['customers'] += 1
        s['value'] += c.get('value') or 0
        s['deposits'] += c.get('deposits') or 0
        s['loans'] += c.get('loans') or 0
    segment_mix = sorted(seg.values(), key=lambda x: x['value'], reverse=True)

    # ---- risk distribution (DERIVED from real balance-sheet leverage) ---
    risk_rows = _derived_risk(customers)

    # ---- trends: REAL month-end (EOM) history in live mode, simulated otherwise ---
    real = None
    if data_mode() == 'live':
        try:
            real = gateway.portfolio_trends(customers, period)
        except Exception:
            real = None

    if real:
        trend_status = LIVE
        book_trend = real['book']
        book_note = ('Real month-end (EOM) balances across the sample. Books are snapshotted '
                     'at month-end, so shorter periods show fewer points than YTD.')
        seg_names = real['segments']
        seg_trend_data = real['segments_data']
        seg_note = 'Real per-segment value at each month-end across the sample.'
        movers = real['movers']
        movers_note = 'Real value change between the two most recent month-ends.'
    else:
        trend_status = PREVIEW
        book_trend = {
            'deposits': _walk(tot_dep or 1, 0.35, dates, 'pf-dep' + _sk(sales_codes)),
            'loans': _walk(tot_loan or 1, 0.18, dates, 'pf-loan' + _sk(sales_codes), vol=0.03),
        }
        book_note = 'Portfolio book trend — preview until the nightly summary table is built.'
        seg_names = [s['segment'] for s in segment_mix]
        seg_series = {s['segment']: _walk(s['value'] or 1, 0.3 + 0.1 * (i % 3), dates, f'pf-seg{i}')
                      for i, s in enumerate(segment_mix)}
        seg_trend_data = []
        for idx, d in enumerate(dates):
            row: dict[str, Any] = {'period': d.isoformat()}
            for name in seg_names:
                row[name] = seg_series[name][idx]['balance']
            seg_trend_data.append(row)
        seg_note = 'Segment value trend — preview until per-period history is materialised.'
        movers = _simulated_movers(customers)
        movers_note = 'Period-over-period movement needs a historical snapshot — simulated for now.'

    # In live mode Level 1 is served from a bounded real sample (the whole-book
    # roll-up is nightly-precompute territory, §6). Say so — never imply the sample
    # is the entire book.
    live_sample = data_mode() == 'live'
    return {
        'scope': {
            'whole_book': sales_codes is None and not live_sample,
            'customers_in_view': n,
            'live_sample': live_sample,
            'sample_note': (
                f'Live sample of {n:,} real customers — whole-book aggregation runs in the '
                'nightly precompute (not a live full-book scan).' if live_sample else None),
        },
        'period': period.to_dict(),
        'summary': {
            'customers': live(n, unit='count').to_dict(),
            'active_customers': live(n, unit='count').to_dict(),
            'relationship_value': live(tot_value, unit='KES').to_dict(),
            'deposits': live(tot_dep, unit='KES').to_dict(),
            'loans': live(tot_loan, unit='KES').to_dict(),
            'avg_products': live(avg_products, unit='count').to_dict(),
        },
        'segment_mix': {
            'question': 'How many customers per segment, and their combined value?',
            'status': Provenance.LIVE.value,
            'rows': segment_mix,
        },
        'risk_distribution': {
            'question': 'How exposed is the book right now?',
            'status': DERIVED,
            'note': 'Derived from balance-sheet leverage (loans vs deposits) across the sample — '
                    'a book-level proxy; per-customer risk also factors loan performance & KYC.',
            'rows': risk_rows,
        },
        'book_trend': {
            'question': 'Is the balance-sheet mix shifting?',
            'status': trend_status,
            'note': book_note,
            'series': book_trend,
        },
        'segment_value_trend': {
            'question': 'Which segments are growing vs shrinking?',
            'status': trend_status,
            'note': seg_note,
            'segments': seg_names,
            'data': seg_trend_data,
        },
        'top_movers': {
            'question': 'Who needs attention this period?',
            'status': trend_status,
            'note': movers_note,
            'rows': movers,
        },
    }


def _sk(sales_codes: list[str] | None) -> str:
    return 'ALL' if sales_codes is None else '-'.join(sorted(sales_codes))


def _derived_risk(customers: list[dict]) -> list[dict]:
    """Book-level risk distribution derived from each customer's real balances —
    leverage (loans vs deposits) as the proxy, no simulation. Mirrors the
    per-customer leverage logic in ``c360.risk.derive_risk``."""
    buckets = {c: 0 for c in _RISK_CLASSES}
    for c in customers:
        dep = c.get('deposits') or 0
        loan = c.get('loans') or 0
        if loan <= 0 and dep <= 0:
            buckets['Unclassified'] += 1
        elif loan <= 0:
            buckets['Low'] += 1                       # deposits only, no credit exposure
        else:
            ratio = loan / (dep + 1)
            if ratio >= 8 and loan >= 1_000_000:
                buckets['High'] += 1
            elif ratio >= 3:
                buckets['Medium'] += 1
            else:
                buckets['Low'] += 1
    return [{'class': cls, 'customers': buckets[cls]} for cls in _RISK_CLASSES]


def _simulated_movers(customers: list[dict], limit: int = 6) -> list[dict]:
    rows = []
    for c in customers:
        r = _stream(c['cust_id'] + 'mover')
        pct = round((r.next() - 0.45) * 0.4, 3)  # -18%..+22%
        value = c.get('value') or 0
        rows.append({
            'cust_id': c['cust_id'], 'name': c['name'], 'segment': c['segment'],
            'value': value, 'delta_pct': pct, 'delta_value': round(value * pct),
            'direction': 'up' if pct >= 0 else 'down',
        })
    rows.sort(key=lambda x: abs(x['delta_value']), reverse=True)
    return rows[:limit]
