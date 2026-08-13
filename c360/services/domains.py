"""Non-core domain services — Whizz, Properties, Bancassurance.

These emit a *generic* domain payload (metrics + a list of charts + tables) so the
frontend renders any domain with one renderer. HFCB keeps its bespoke service; these
three follow the same visual grammar but are entirely PREVIEW until their pipelines
land, so every value carries preview provenance and the domain is flagged as preview.

Chart payload is discriminated by ``kind``:
  lines   — 1–2 line series over a period
  bars    — horizontal magnitude bars
  grouped — two-series grouped bars (e.g. value vs loan per property)
  donut   — part-to-whole
"""
from __future__ import annotations

from datetime import date
from typing import Any

from ..warehouse.factory import data_mode
from ..warehouse.gateway import WarehouseGateway
from ..warehouse.periods import ResolvedPeriod

PREVIEW = 'preview'
LIVE = 'live'


def _metric(label, value, unit, *, lead=False, tone=None, status=PREVIEW, meta=None):
    m = {'label': label, 'value': value, 'unit': unit, 'status': status}
    if lead:
        m['lead'] = True
    if tone:
        m['tone'] = tone
    if meta:
        m['meta'] = meta
    return m


def _term_months(start: str | None, end: str | None) -> int:
    """Whole months of cover between two ISO dates; defaults to 12 (annual policy)."""
    try:
        s = date.fromisoformat(start); e = date.fromisoformat(end)
        months = (e.year - s.year) * 12 + (e.month - s.month)
        return max(months, 1)
    except (TypeError, ValueError):
        return 12


def _empty(cust_id, domain, reason):
    return {'cust_id': cust_id, 'domain': domain, 'preview': True,
            'metrics': [], 'charts': [], 'tables': [], 'empty_reason': reason}


def build_whizz(gateway: WarehouseGateway, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
    if gateway.get_customer(cust_id) is None:
        return None
    try:
        data = gateway.get_whizz(cust_id, period)
    except Exception:
        data = None
    if data is None:
        return _empty(cust_id, 'Whizz', 'No Whizz / M-Pesa activity for this customer in the selected period.')

    live = data_mode() == 'live'
    st = LIVE if live else PREVIEW
    avg = round(data['txn_value'] / data['txn_count']) if data.get('txn_count') else 0
    since = data.get('registered_since')
    profile_note = (f"Whizz customer since {since[:4]} · {data.get('status', '')}".strip(' ·')
                    if since else (f"Whizz status: {data.get('status')}" if data.get('status') else None))
    return {
        'cust_id': cust_id, 'domain': 'Whizz', 'preview': not live, 'period': period.to_dict(),
        'metrics': [
            _metric('Transactions', data['txn_count'], 'count', lead=True, status=st),
            _metric('Value moved', data['txn_value'], 'KES', status=st),
            _metric('Services used', data['services_used'], 'count', status=st,
                    meta=', '.join(c['label'] for c in data['categories']) or None),
            _metric('Avg / transaction', avg, 'KES', status=st),
        ],
        'charts': [
            {'kind': 'lines', 'id': 'activity', 'title': 'Whizz activity',
             'question': 'Is Whizz engagement growing, flat, or dropping off?', 'status': st, 'fmt': 'count',
             'series': [{'name': 'Transactions', 'dataKey': 'count', 'colorRole': 1}],
             'data': data['activity']},
            {'kind': 'bars', 'id': 'categories', 'title': 'What they use Whizz for',
             'question': 'Which Whizz services move the most money?', 'status': st, 'fmt': 'kes',
             'data': [{'label': c['label'], 'value': c['value'], 'colorRole': 1} for c in data['categories']]},
        ],
        'tables': [
            {'id': 'recent', 'title': 'Recent Whizz transactions', 'status': st, 'note': profile_note,
             'columns': ['date', 'description', 'amount'], 'rows': data['recent']},
        ],
    }


def build_properties(gateway: WarehouseGateway, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
    if gateway.get_customer(cust_id) is None:
        return None
    try:
        data = gateway.get_properties(cust_id)
    except Exception:
        data = None
    if data is None:
        return _empty(cust_id, 'Properties', 'No properties linked to this customer.')

    props = data['properties']
    live = data_mode() == 'live'
    st = LIVE if live else PREVIEW
    total_value = sum(p['value'] for p in props)
    n_units = len(props)
    n_mortgaged = sum(1 for p in props if p.get('mortgage'))
    n_projects = len({p['project'] for p in props})
    avg_value = round(total_value / n_units) if n_units else 0

    # Value by project (dedup-safe: props are already one row per unit).
    by_project: dict[str, int] = {}
    for p in props:
        by_project[p['project']] = by_project.get(p['project'], 0) + p['value']

    return {
        'cust_id': cust_id, 'domain': 'Properties', 'preview': not live, 'period': period.to_dict(),
        'metrics': [
            _metric('Property value', total_value, 'KES', lead=True, status=st),
            _metric('Properties', n_units, 'count', status=st),
            _metric('Under mortgage', n_mortgaged, 'count', status=st),
            _metric('Avg property value', avg_value, 'KES', status=st),
        ],
        'charts': [
            {'kind': 'donut', 'id': 'by_project', 'title': 'Property value by project',
             'question': 'Where is the customer’s property wealth concentrated?', 'status': st, 'fmt': 'kes',
             'data': [{'label': proj, 'value': val} for proj, val in
                      sorted(by_project.items(), key=lambda kv: kv[1], reverse=True)]},
            {'kind': 'bars', 'id': 'paid', 'title': 'Payment progress per unit',
             'question': 'How much of each unit is paid off?', 'status': st, 'fmt': 'pct',
             'data': [{'label': f"{p['project']} · {p['unit']}", 'value': p['paid_pct'], 'colorRole': 1}
                      for p in props]},
        ],
        'tables': [
            {'id': 'props', 'title': 'Properties held', 'status': st,
             'columns': ['project', 'unit', 'value', 'paid_pct', 'mortgage'], 'rows': props},
        ],
    }


def build_bancassurance(gateway: WarehouseGateway, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
    if gateway.get_customer(cust_id) is None:
        return None
    try:
        data = gateway.get_bancassurance(cust_id, period)
    except Exception:
        data = None
    if data is None:
        return _empty(cust_id, 'Bancassurance', 'No insurance policies linked to this customer.')

    policies = data['policies']
    live = data_mode() == 'live'
    st = LIVE if live else PREVIEW

    # Per-policy monthly payment = premium spread over the policy's term of cover
    # (annual by default). Injected onto each row so the table shows it too.
    for p in policies:
        p['monthly'] = round(p['premium'] / _term_months(p.get('start'), p.get('end')))

    total_premium = sum(p['premium'] for p in policies)
    total_insured = sum(p['sum_insured'] for p in policies)
    total_monthly = sum(p['monthly'] for p in policies)
    n_active = sum(1 for p in policies if str(p.get('status', '')).lower() == 'active')

    # Premium by product (dedup-safe: policies are already one row per policy_no).
    by_product: dict[str, int] = {}
    for p in policies:
        by_product[p['product']] = by_product.get(p['product'], 0) + p['premium']

    return {
        'cust_id': cust_id, 'domain': 'Bancassurance', 'preview': not live, 'period': period.to_dict(),
        'metrics': [
            _metric('Annual premium', total_premium, 'KES', lead=True, status=st),
            _metric('Monthly payments', total_monthly, 'KES', status=st),
            _metric('Sum insured', total_insured, 'KES', status=st),
            _metric('Policies', len(policies), 'count', status=st,
                    meta=f'{n_active} active' if n_active else 'none active'),
        ],
        'charts': [
            {'kind': 'bars', 'id': 'monthly', 'title': 'Monthly payment per policy',
             'question': 'What does this customer pay each month, per policy?', 'status': st, 'fmt': 'kes',
             'data': [{'label': p['product'], 'value': p['monthly'], 'colorRole': 1} for p in policies]},
            {'kind': 'donut', 'id': 'mix', 'title': 'Premium by product',
             'question': "What's insured, and where is the premium concentrated?", 'status': st, 'fmt': 'kes',
             'data': [{'label': prod, 'value': val} for prod, val in
                      sorted(by_product.items(), key=lambda kv: kv[1], reverse=True)]},
            {'kind': 'bars', 'id': 'insured', 'title': 'Sum insured per policy',
             'question': 'How much cover does each policy carry?', 'status': st, 'fmt': 'kes',
             'data': [{'label': p['product'], 'value': p['sum_insured'], 'colorRole': 4}
                      for p in policies]},
        ],
        'tables': [
            {'id': 'policies', 'title': 'Policies held', 'status': st,
             'columns': ['policy', 'product', 'premium', 'monthly', 'sum_insured', 'status'], 'rows': policies},
        ],
    }


DOMAIN_BUILDERS = {
    'whizz': build_whizz,
    'properties': build_properties,
    'bancassurance': build_bancassurance,
}
