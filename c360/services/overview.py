"""Level 2 — Customer overview service.

The landing view for a single customer: where their relationship value sits across
domains, whether they're becoming more or less valuable over time, and per-domain
snapshots that drill into Level 3. The recommendation panel (the other Level 2 piece)
has its own endpoint.

Provenance is honest: HFCB value is LIVE (core-banking snapshot); the other domains'
contributions are PREVIEW (computed from preview domain data, cross-domain roll-up
not yet reconciled to a golden record); the relationship trend is PREVIEW (per-customer
history is a "must build", derived here from the movement-based balance series).
"""
from __future__ import annotations

from typing import Any

from ..warehouse.factory import data_mode
from ..warehouse.gateway import WarehouseGateway
from ..warehouse.periods import ResolvedPeriod
from ..warehouse.provenance import Provenance, live

LIVE = Provenance.LIVE.value
PREVIEW = Provenance.PREVIEW.value


def build_customer_overview(gateway: WarehouseGateway, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
    customer = gateway.get_customer(cust_id)
    if not customer:
        return None

    value = gateway.get_relationship_value(cust_id)
    rel_value = value['relationship_value']

    whizz = _safe(gateway.get_whizz, cust_id, period)
    props = _safe(gateway.get_properties, cust_id)
    banc = _safe(gateway.get_bancassurance, cust_id, period)

    # ---- value-by-domain slices ----------------------------------------
    slices = [{'domain': 'HFCB', 'value': rel_value, 'status': LIVE}]
    snapshots = [{
        'domain': 'HFCB', 'tab': 'hfcb', 'status': LIVE,
        'label': 'Relationship value', 'value': rel_value,
        'sub': f"{_m(value['deposits'])} deposits · {_m(value['loans'])} loans",
    }]

    if whizz:
        wv = whizz.get('txn_value') or 0
        wst = LIVE if data_mode() == 'live' else PREVIEW
        slices.append({'domain': 'Whizz', 'value': wv, 'status': wst})
        snapshots.append({'domain': 'Whizz', 'tab': 'whizz', 'status': wst,
                          'label': 'Whizz value moved', 'value': wv,
                          'sub': f"{whizz.get('txn_count', 0)} transactions this period"})
    else:
        snapshots.append(_empty_snapshot('Whizz', 'whizz', 'No Whizz activity'))

    if props:
        pv = sum(p['value'] for p in props['properties'])
        pst = LIVE if data_mode() == 'live' else PREVIEW
        slices.append({'domain': 'Properties', 'value': pv, 'status': pst})
        snapshots.append({'domain': 'Properties', 'tab': 'properties', 'status': pst,
                          'label': 'Property value', 'value': pv,
                          'sub': f"{len(props['properties'])} propert{'y' if len(props['properties']) == 1 else 'ies'}"})
    else:
        snapshots.append(_empty_snapshot('Properties', 'properties', 'No linked properties'))

    if banc:
        bv = sum(p['premium'] for p in banc['policies'])
        bst = LIVE if data_mode() == 'live' else PREVIEW
        slices.append({'domain': 'Bancassurance', 'value': bv, 'status': bst})
        snapshots.append({'domain': 'Bancassurance', 'tab': 'bancassurance', 'status': bst,
                          'label': 'Annual premium', 'value': bv,
                          'sub': f"{len(banc['policies'])} polic{'y' if len(banc['policies']) == 1 else 'ies'}"})
    else:
        snapshots.append(_empty_snapshot('Bancassurance', 'bancassurance', 'No policies'))

    # ---- relationship trend, split into deposits vs loans ----
    # Real EOM day-grouped balances in live mode; simulated in mock.
    dl = gateway.deposit_loan_series(cust_id, period)
    trend = _split_trend(dl)
    trend_live = data_mode() == 'live'
    trend_status = LIVE if trend_live else PREVIEW
    trend_note = (None if trend_live else
                  'Per-customer trend — preview, derived from the balance movement series until history is materialised.')

    return {
        'cust_id': cust_id,
        'period': period.to_dict(),
        'relationship_value': live(rel_value, unit='KES').to_dict(),
        'value_by_domain': {
            'question': "Where does this customer's relationship value actually sit?",
            'note': 'Non-core domains are preview — cross-domain roll-up not yet reconciled to a golden record.',
            'slices': slices,
        },
        'relationship_trend': {
            'question': 'Is this customer becoming more or less valuable over time?',
            'status': trend_status,
            'note': trend_note,
            'split': ['deposits', 'loans'],   # stacked: total height = deposits + loans
            'series': trend,
        },
        'domain_snapshots': snapshots,
    }


def _split_trend(dl: dict[str, list[dict]]) -> list[dict]:
    """Long-format rows carrying deposits and loans per period, so the frontend can
    stack them (total = relationship value proxy) or read the split."""
    rows: dict[str, dict] = {}
    for p in dl.get('deposits', []):
        rows.setdefault(p['period'], {'period': p['period'], 'deposits': 0, 'loans': 0})['deposits'] = round(p['balance'])
    for p in dl.get('loans', []):
        rows.setdefault(p['period'], {'period': p['period'], 'deposits': 0, 'loans': 0})['loans'] = round(p['balance'])
    return [rows[k] for k in sorted(rows)]


def _empty_snapshot(domain: str, tab: str, reason: str) -> dict:
    return {'domain': domain, 'tab': tab, 'status': 'to_source', 'label': reason, 'value': None, 'sub': ''}


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def _m(v: int | float | None) -> str:
    if not v:
        return 'KES 0'
    if abs(v) >= 1_000_000:
        return f'KES {v / 1_000_000:.1f}M'
    return f'KES {v / 1_000:.0f}K'
