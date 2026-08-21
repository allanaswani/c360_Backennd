"""HFCB (core banking) domain service — Level 3.

This is the reference implementation every other domain (Whizz, Properties,
Bancassurance) will copy. Structure:

* headline metrics (LIVE snapshot)
* five charts, each tagged with the *question it answers* (§5.3) and its provenance
* two supporting tables (product uptake LIVE, recent transactions PREVIEW)

Per-customer time series are PREVIEW: only segment/branch/RM trends exist in the
warehouse today, so a per-customer series is a "must build" derivation from the
pivoted movement tables. The service tags them accordingly; the shape it returns is
identical to what the live long-format view will return, so the frontend never
changes when real data lands.
"""
from __future__ import annotations

from typing import Any

from ..warehouse.factory import data_mode
from ..warehouse.gateway import WarehouseGateway
from ..warehouse.periods import ResolvedPeriod
from ..warehouse.provenance import Provenance, Series, derived, live, to_source

# In mock, per-customer series are a simulated must-build derivation → PREVIEW.
# In live, they are real (EOM day-grouped balances / rpt_c360 summaries) → LIVE.
_SERIES_NOTE = 'Per-customer series — preview, derived from the movement tables until the long-format view is built.'


def _series(key: str, name: str, points: list[dict], status=Provenance.PREVIEW, note=_SERIES_NOTE) -> dict:
    return Series(key=key, name=name, points=points, status=status, note=note).to_dict()


def _provenance() -> tuple[Provenance, str | None]:
    """Per-customer live data is real in live mode, simulated in mock."""
    if data_mode() == 'live':
        return Provenance.LIVE, None
    return Provenance.PREVIEW, _SERIES_NOTE


def build_hfcb_domain(gateway: WarehouseGateway, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
    customer = gateway.get_customer(cust_id)
    if not customer:
        return None

    value = gateway.get_relationship_value(cust_id)
    holdings = gateway.get_product_holdings(cust_id)
    deposit_accts = gateway.get_deposit_accounts(cust_id)
    loan_accts = gateway.get_loan_accounts(cust_id)

    dl = gateway.deposit_loan_series(cust_id, period)
    disb = gateway.disbursement_vs_balance_series(cust_id, period)
    txns = gateway.transaction_series(cust_id, period)
    channels = gateway.channel_usage(cust_id, period)
    recent = gateway.recent_transactions(cust_id, period=period, limit=8)

    products_held = sum(1 for held in holdings['flags'].values() if held)
    deposits = value['deposits'] or 0
    loans = value['loans'] or 0
    net_position = deposits - loans
    series_status, series_note = _provenance()

    # AUM + profitability + NPL from the reporting Postgres (customer_allocation_base).
    # None when that source isn't wired (mock / Postgres down) → badged 'not sourced'.
    try:
        prof = gateway.get_profitability(cust_id)
    except Exception:
        prof = None

    return {
        'cust_id': cust_id,
        'period': period.to_dict(),
        # ---- headline metrics (LIVE) --------------------------------------
        'metrics': {
            'total_deposits': live(deposits, unit='KES').to_dict(),
            'total_loans': live(loans, unit='KES').to_dict(),
            'net_position': live(net_position, unit='KES').to_dict(),
            'products_held': live(products_held, unit='count').to_dict(),
            'revenue': live(value['revenue'], unit='KES').to_dict(),
            # Derived ratio reads (from the live snapshot) — leverage + channel reach.
            'loan_to_deposit': _leverage_metric(deposits, loans),
            'active_channels': live(len(channels), unit='count').to_dict(),
            # AUM + profitability from the reporting Postgres (LIVE where sourced, else
            # honestly 'not sourced' — never a fabricated figure).
            'aum': _aum_metric(prof),
            'profitability': _profitability_metric(prof),
            'npl_status': _npl_metric(prof),
        },
        # ---- charts, each answering a stated question ---------------------
        'charts': {
            'balance_trend': {
                'question': 'Is the balance-sheet mix shifting for this customer?',
                'type': 'dual_line',
                'series': [
                    _series('deposits', 'Deposit balance', dl['deposits'], status=series_status, note=series_note),
                    _series('loans', 'Loan balance', dl['loans'], status=series_status, note=series_note),
                ],
                'empty_reason': None,
            },
            'disbursement_vs_balance': {
                'question': 'Is the customer paying down faster than they are borrowing?',
                'type': 'dual_line',
                'series': [
                    _series('disbursed', 'Cumulative disbursed', disb['disbursed'], status=series_status, note=series_note),
                    _series('balance', 'Outstanding balance', disb['balance'], status=series_status, note=series_note),
                ],
                'empty_reason': None if loan_accts else 'No loan facilities on this customer.',
            },
            'product_holdings': {
                'question': "What's actually driving their balance?",
                'type': 'bar',
                'status': Provenance.LIVE.value,
                'bars': _product_holdings_bars(deposit_accts, loan_accts),
            },
            'transaction_trend': {
                'question': 'Is engagement increasing, flat, or dropping off?',
                'type': 'line',
                'series': [_series('txn_count', 'Transactions', txns, status=series_status, note=series_note)],
            },
            'channel_usage': {
                'question': 'How do they prefer to interact with us?',
                'type': 'donut',
                'status': series_status.value,
                'note': series_note or 'Channel mix — customer-facing transactions by channel this period.',
                'slices': [{'channel': c['channel'], 'share': c['share']} for c in channels],
            },
        },
        # ---- supporting tables --------------------------------------------
        'tables': {
            'product_uptake': {
                'status': Provenance.LIVE.value,
                'columns': ['product', 'account_no', 'balance', 'status'],
                'rows': _product_uptake_rows(deposit_accts, loan_accts),
            },
            'recent_transactions': {
                'status': series_status.value,
                'note': series_note or 'Most recent customer-facing activity (looks back up to 24 months so it always loads, independent of the period filter above).',
                'columns': ['date', 'description', 'channel', 'amount'],
                'rows': recent,
            },
        },
    }


def _aum_metric(prof: dict | None) -> dict[str, Any]:
    if prof and prof.get('aum') is not None:
        return live(round(prof['aum']), unit='KES',
                    note=('Assets under management from the portfolio allocation base — a '
                          'periodic management snapshot, so it can differ from the live '
                          'deposit/loan balances above.')).to_dict()
    return to_source(unit='KES', note='AUM pending the allocation feed.').to_dict()


def _profitability_metric(prof: dict | None) -> dict[str, Any]:
    if prof and prof.get('contribution') is not None:
        return live(round(prof['contribution']), unit='KES',
                    note='Net contribution after expense (customer_allocation_base).').to_dict()
    return to_source(unit='KES', note='Profitability pending the allocation feed.').to_dict()


def _npl_metric(prof: dict | None) -> dict[str, Any]:
    """Loan performance status from the allocation base's NPL flag."""
    if prof is None or prof.get('npl') is None:
        return to_source(note='Loan-performance flag pending the allocation feed.').to_dict()
    status = 'Non-performing' if prof['npl'] else 'Performing'
    return derived(status, note='From the NPL flag on customer_allocation_base.').to_dict()


def _leverage_metric(deposits: float, loans: float) -> dict[str, Any]:
    """Loan-to-deposit leverage (loans ÷ deposits) — a DERIVED gearing read. Honest when
    there is nothing to gear against, rather than a fake ratio: a borrower with no
    deposits, or a pure depositor, carries an explicit label instead of a bare number."""
    if deposits and deposits > 0:
        r = loans / deposits
        band = 'Low gearing' if r < 1 else 'Moderate gearing' if r < 3 else 'High gearing'
        return derived(round(r, 2), unit='ratio',
                       note=f'Loan-to-deposit = loans ÷ deposits. {band}.').to_dict()
    if loans and loans > 0:
        return derived(None, unit='ratio', label='Borrowing only',
                       note='Loans with no deposit balance to offset — leverage not meaningful.').to_dict()
    return derived(None, unit='ratio', label='No lending',
                   note='No loan facilities — leverage not applicable.').to_dict()


def _product_holdings_bars(deposit_accts: list[dict], loan_accts: list[dict]) -> list[dict]:
    bars: list[dict] = []
    for a in deposit_accts:
        bars.append({'product': a['product'], 'balance': a['balance'], 'side': 'deposit'})
    for a in loan_accts:
        bars.append({'product': a['product'], 'balance': a['outstanding_balance'], 'side': 'loan'})
    return sorted(bars, key=lambda b: b['balance'], reverse=True)


def _product_uptake_rows(deposit_accts: list[dict], loan_accts: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for a in deposit_accts:
        rows.append({'product': a['product'], 'account_no': a['account_no'],
                     'balance': a['balance'], 'status': a['status']})
    for a in loan_accts:
        rows.append({'product': a['product'], 'account_no': a['account_no'],
                     'balance': a['outstanding_balance'], 'status': a.get('classification', 'Active')})
    return rows
