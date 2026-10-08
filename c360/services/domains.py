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
#: A figure the source genuinely does not state, as opposed to one that is zero.
TO_SOURCE = 'to_source'


def _metric(label, value, unit, *, lead=False, tone=None, status=PREVIEW, meta=None, spark=None):
    m = {'label': label, 'value': value, 'unit': unit, 'status': status}
    if lead:
        m['lead'] = True
    if tone:
        m['tone'] = tone
    if meta:
        m['meta'] = meta
    # A short trend to draw under the figure. Only attached when there are ≥2 points
    # (a single point would render a misleading flat line).
    if spark and len(spark) >= 2:
        m['spark'] = spark
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


def _unavailable(cust_id, domain, detail=None):
    """The source couldn't be read — an error, or the source table itself is empty/
    unreachable — as opposed to the customer genuinely holding none. Honesty rule
    (never a silent —): an RM must not see "nothing linked" when the truth is
    "not loaded", so this is flagged distinctly for the UI to render as such."""
    return {'cust_id': cust_id, 'domain': domain, 'preview': True,
            'metrics': [], 'charts': [], 'tables': [], 'unavailable': True,
            'empty_reason': detail or (
                f'{domain} data could not be loaded right now. Its source may be '
                'temporarily unavailable. This is a data-source issue, not a gap in '
                'this customer’s record.')}


def _mobile_loans(gateway: WarehouseGateway, cust_id: str) -> dict | None:
    """The customer's mobile-loan history from the loan agreements, or None. A failure
    here only drops this section; it never takes the Whizz tab down with it."""
    try:
        fac = gateway.get_facilities(cust_id)
    except Exception:
        return None
    return (fac or {}).get('mobile')


_MOBILE_NOTE = ('Each mobile loan is approved separately, so these are the amounts approved '
                'per loan, not a standing limit. The Whizz eligibility limit is held in the '
                'Whizz system and is not in the warehouse.')


def _mobile_sections(mob: dict, st: str) -> tuple[list, list, list]:
    metrics = [
        _metric('Mobile loans taken', mob['loans_taken'], 'count', status=st,
                meta=(f"Since {mob['first_issued'][:4]}" if mob.get('first_issued') else None)),
        _metric('Latest approved', mob['latest_amount'], 'KES', status=st,
                meta=(f"On {mob['latest_issued']}" if mob.get('latest_issued') else None)),
        _metric('Highest approved', mob['highest_amount'], 'KES', status=st),
        _metric('Mobile loan outstanding', mob['outstanding'], 'KES', status=st),
    ]
    charts = []
    if len(mob['history']) >= 2:
        charts.append({'kind': 'lines', 'id': 'mobile_loans', 'title': 'Mobile loan approved per loan',
                       'question': 'Is the amount they are approved for growing?', 'status': st,
                       'fmt': 'kes', 'note': _MOBILE_NOTE,
                       'series': [{'name': 'Approved', 'dataKey': 'amount', 'colorRole': 1}],
                       'data': mob['history']})
    rows = [{'date': h['period'], 'description': 'Mobile loan approved', 'amount': h['amount'],
             'balance': h['outstanding']} for h in reversed(mob['history'][-24:])]
    tables = [{'id': 'mobile_loans', 'title': 'Mobile loans (latest first)', 'status': st,
               'note': _MOBILE_NOTE, 'columns': ['date', 'description', 'amount', 'balance'],
               'rows': rows}]
    return metrics, charts, tables


def _flow_buckets(activity: list[dict], start: date, end: date) -> list[dict[str, Any]]:
    """Daily received/sent summed into weeks (periods up to ~2 months) or months, with
    every bucket in the range present - a quiet week is a zero bar, not a missing one."""
    from datetime import timedelta
    weekly = (end - start).days <= 62
    def key(d: date) -> date:
        return d - timedelta(days=d.weekday()) if weekly else d.replace(day=1)
    out: dict[date, dict[str, Any]] = {}
    k = key(start)
    while k <= end:
        out[k] = {'label': (f"w/c {k.strftime('%d %b')}" if weekly else k.strftime('%b %Y')), 'a': 0, 'b': 0}
        k = (k + timedelta(days=7)) if weekly else (k.replace(day=28) + timedelta(days=4)).replace(day=1)
    for p in activity:
        try:
            d = date.fromisoformat(str(p['period'])[:10])
        except (TypeError, ValueError):
            continue
        b = out.get(key(d))
        if b is not None:
            b['a'] += p.get('in', 0) or 0
            b['b'] += p.get('out', 0) or 0
    return [out[k] for k in sorted(out)]


def build_whizz(gateway: WarehouseGateway, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
    if gateway.get_customer(cust_id) is None:
        return None
    live = data_mode() == 'live'
    st = LIVE if live else PREVIEW
    mob = _mobile_loans(gateway, cust_id)
    try:
        data = gateway.get_whizz(cust_id, period)
    except Exception:
        if mob:
            m, c, t = _mobile_sections(mob, st)
            return {'cust_id': cust_id, 'domain': 'Whizz', 'preview': not live, 'period': period.to_dict(),
                    'metrics': m, 'charts': c, 'tables': t,
                    'note': 'Whizz transactions could not be loaded right now; mobile loans are shown.'}
        return _unavailable(cust_id, 'Whizz')
    if data is None:
        if mob:
            m, c, t = _mobile_sections(mob, st)
            return {'cust_id': cust_id, 'domain': 'Whizz', 'preview': not live, 'period': period.to_dict(),
                    'metrics': m, 'charts': c, 'tables': t,
                    'note': 'No Whizz / M-Pesa transactions in the selected period.'}
        return _empty(cust_id, 'Whizz', 'No Whizz / M-Pesa activity for this customer in the selected period.')

    avg = round(data['txn_value'] / data['txn_count']) if data.get('txn_count') else 0
    # Daily trends for the tile sparklines.
    spark_count = [p['count'] for p in data['activity']]
    since = data.get('registered_since')
    profile_note = (f"Whizz customer since {since[:4]} · {data.get('status', '')}".strip(' ·')
                    if since else (f"Whizz status: {data.get('status')}" if data.get('status') else None))
    # Direction is known when the gateway read the signed amount (live). The preview
    # gateway does not carry it, and then the single "value moved" view is kept.
    directional = data.get('money_in') is not None
    metrics = [_metric('Transactions', data['txn_count'], 'count', lead=True, status=st, spark=spark_count)]
    if directional:
        metrics += [
            _metric('Received via Whizz', data['money_in'], 'KES', status=st, tone='pos',
                    spark=[p.get('in', 0) for p in data['activity']]),
            _metric('Sent via Whizz', data['money_out'], 'KES', status=st, tone='neg',
                    spark=[p.get('out', 0) for p in data['activity']]),
            _metric('Charges paid', data.get('charges', 0), 'KES', status=st,
                    meta='Debited above the payment value, plus fee rows'),
        ]
    else:
        metrics.append(_metric('Value moved', data['txn_value'], 'KES', status=st,
                               spark=[p.get('value', 0) for p in data['activity']]))
    metrics += [
        _metric('Services used', data['services_used'], 'count', status=st,
                meta=', '.join(c['label'] for c in data['categories']) or None),
        _metric('Avg / transaction', avg, 'KES', status=st),
    ]
    charts: list[dict[str, Any]] = [
        {'kind': 'lines', 'id': 'activity', 'title': 'Whizz activity',
         'question': 'Is Whizz engagement growing, flat, or dropping off?', 'status': st, 'fmt': 'count',
         'series': [{'name': 'Transactions', 'dataKey': 'count', 'colorRole': 1}],
         'data': data['activity']},
    ]
    if directional:
        buckets = _flow_buckets(data['activity'], period.start, period.end)
        if len(buckets) >= 2:
            charts.append({'kind': 'grouped', 'id': 'flow', 'title': 'Received vs sent via Whizz',
                           'question': 'Is more money coming in through Whizz than going out?',
                           'status': st, 'fmt': 'kes', 'seriesNames': ['Received', 'Sent'],
                           'data': buckets})
    else:
        charts.append({'kind': 'lines', 'id': 'value_moved', 'title': 'Value moved over time',
                       'question': 'How much money is flowing through Whizz over the period?',
                       'status': st, 'fmt': 'kes',
                       'series': [{'name': 'Value moved', 'dataKey': 'value', 'colorRole': 2}],
                       'data': data['activity']})
    charts += [
        {'kind': 'bars', 'id': 'categories', 'title': 'What they use Whizz for',
         'question': 'Which Whizz services move the most money?', 'status': st, 'fmt': 'kes',
         'data': [{'label': c['label'], 'value': c['value'], 'colorRole': 1} for c in data['categories']]},
        # Count, not value: the bars above already show value, and how OFTEN a service
        # is used is a different question (airtime is small money, many times).
        {'kind': 'donut', 'id': 'service_mix', 'title': 'How often each service is used',
         'question': 'Which services do they reach for most often?', 'status': st, 'fmt': 'count',
         'data': [{'label': c['label'], 'value': c['count']} for c in data['categories'] if c['count'] > 0]},
    ]
    payload = {
        'cust_id': cust_id, 'domain': 'Whizz', 'preview': not live, 'period': period.to_dict(),
        'metrics': metrics,
        'charts': charts,
        'tables': [
            {'id': 'recent', 'title': 'Whizz transactions (latest 50)', 'status': st, 'note': profile_note,
             'columns': ['date', 'description', 'amount'], 'rows': data['recent'],
             # Signed amounts (live), so the table totals money in, out and net.
             'flow': directional},
        ],
    }
    if mob:
        m, c, t = _mobile_sections(mob, st)
        payload['metrics'] += m
        payload['charts'] += c
        payload['tables'] += t
    return payload


def build_properties(gateway: WarehouseGateway, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
    if gateway.get_customer(cust_id) is None:
        return None
    try:
        data = gateway.get_properties(cust_id)
    except Exception:
        return _unavailable(cust_id, 'Properties')
    if data is None:
        return _empty(cust_id, 'Properties', 'No properties linked to this customer.')

    props = data['properties']
    live = data_mode() == 'live'
    st = LIVE if live else PREVIEW
    total_value = sum(p['value'] for p in props)
    n_units = len(props)
    n_mortgaged = sum(1 for p in props if p.get('mortgage'))
    projects = {p['project'] for p in props}

    # Paid to date. Live: from the property payment register (the register's own
    # perc_paid is 0 on 9,686 of 9,708 units and is not used). A unit with no payment
    # on record has paid_pct None - "not known", never "0% paid".
    for p in props:
        if p.get('paid_pct') is not None and 'paid' not in p:
            p['paid'] = round(p['value'] * p['paid_pct'])
            p['outstanding'] = max(p['value'] - p['paid'], 0)
    known = [p for p in props if p.get('paid_pct') is not None]
    unknown = n_units - len(known)
    paid_total = sum(p['paid'] for p in known)
    owed_total = sum(p['outstanding'] for p in known)
    known_note = (None if not unknown else
                  f"{len(known)} of {n_units} units; {unknown} "
                  f"{'has' if unknown == 1 else 'have'} no payment on record")

    def unit_label(p: dict) -> str:
        return f"{p['project']} · {p['unit']}"

    mortgaged_value = sum(p['value'] for p in props if p.get('mortgage'))
    outright_value = total_value - mortgaged_value

    metrics = [
        _metric('Property value', total_value, 'KES', lead=True, status=st),
        _metric('Properties', n_units, 'count', status=st,
                meta=f"{len(projects)} project{'s' if len(projects) != 1 else ''}"),
    ]
    if known:
        metrics += [
            _metric('Paid to date', paid_total, 'KES', status=st, tone='pos', meta=known_note),
            _metric('Still to pay', owed_total, 'KES', status=st, meta=known_note),
        ]
    else:
        metrics.append(_metric('Paid to date', None, 'KES', status=TO_SOURCE,
                               meta='no payment on record for these units'))
    metrics.append(_metric('Under mortgage', n_mortgaged, 'count', status=st))

    charts: list[dict[str, Any]] = []
    if len(projects) > 1:
        by_project: dict[str, int] = {}
        for p in props:
            by_project[p['project']] = by_project.get(p['project'], 0) + p['value']
        charts.append({'kind': 'donut', 'id': 'by_project', 'title': 'Property value by project',
                       'question': 'Where is the customer\u2019s property wealth concentrated?', 'status': st,
                       'fmt': 'kes', 'data': [{'label': k, 'value': v} for k, v in
                                              sorted(by_project.items(), key=lambda kv: kv[1], reverse=True)]})
    # Only when both slices exist: a single slice is a fact for a tile, not a chart.
    if mortgaged_value > 0 and outright_value > 0:
        charts.append({'kind': 'donut', 'id': 'financed', 'title': 'Financed vs owned outright',
                       'question': 'How much of the property book is still under mortgage?', 'status': st,
                       'fmt': 'kes', 'data': [{'label': 'Under mortgage', 'value': mortgaged_value},
                                              {'label': 'Owned outright', 'value': outright_value}]})
    if known:
        charts.append({'kind': 'meters', 'id': 'progress', 'title': 'Payment progress per unit',
                       'question': 'How far along is each unit toward being fully paid?',
                       'status': st, 'fmt': 'pct',
                       'data': [{'label': unit_label(p), 'value': p['paid_pct'],
                                 'paid': p['paid'], 'total': p['value']} for p in known]})

    payments = data.get('payments') or []
    unit_names = {p['unit_id']: unit_label(p) for p in props if p.get('unit_id') is not None}
    if payments:
        series = _payment_buckets(payments)
        if len(series) >= 2:
            charts.append({'kind': 'grouped', 'id': 'paid_over_time', 'title': 'Payments over time',
                           'question': 'Are they paying steadily, or has paying slowed or stopped?',
                           'status': st, 'fmt': 'kes', 'seriesNames': ['Paid'], 'data': series})
        by_mode: dict[str, int] = {}
        for x in payments:
            if x['amount'] > 0:
                by_mode[x['mode']] = by_mode.get(x['mode'], 0) + x['amount']
        if len(by_mode) > 1:
            charts.append({'kind': 'donut', 'id': 'pay_mode', 'title': 'How they pay',
                           'question': 'Which channels do their property payments come through?',
                           'status': st, 'fmt': 'kes',
                           'data': [{'label': k, 'value': v} for k, v in
                                    sorted(by_mode.items(), key=lambda kv: kv[1], reverse=True)]})

    register_note = ('Paid to date is from the property payment register, which starts in April 2021: '
                     'a unit paid before then shows less paid than it cost.'
                     if data.get('payments_available') else None)
    tables = [
        {'id': 'props', 'title': 'Properties held', 'status': st, 'note': register_note,
         'columns': ['project', 'unit', 'value', 'paid', 'outstanding', 'paid_pct',
                     'last_payment', 'mortgage'],
         'rows': props, 'sum_columns': ['value', 'paid', 'outstanding']},
    ]
    if payments:
        tables.append({
            'id': 'payments', 'title': 'Property payments (latest first)', 'status': st,
            'note': ('One line per receipt. A move of money between two holdings of the same '
                     'unit nets to nothing and is not listed.'),
            'columns': ['date', 'unit', 'mode', 'amount'],
            'rows': [{'date': x['date'], 'unit': unit_names.get(x['unit_id'], f"Unit {x['unit_id']}"),
                      'mode': x['mode'], 'amount': x['amount']} for x in payments],
            'flow': True})

    return {
        'cust_id': cust_id, 'domain': 'Properties', 'preview': not live, 'period': period.to_dict(),
        'metrics': metrics, 'charts': charts, 'tables': tables,
    }


def _payment_buckets(payments: list[dict]) -> list[dict[str, Any]]:
    """Payments summed by month (up to two years of history) or by quarter (longer),
    with every bucket from first to last payment present, so a gap shows as a zero."""
    dated = sorted(date.fromisoformat(x['date']) for x in payments if x.get('date'))
    if not dated:
        return []
    first, last = dated[0], dated[-1]
    quarterly = (last.year - first.year) * 12 + last.month - first.month + 1 > 24

    def key(d: date) -> tuple[int, int]:
        return (d.year, (d.month - 1) // 3) if quarterly else (d.year, d.month - 1)

    def label(k: tuple[int, int]) -> str:
        return f'Q{k[1] + 1} {k[0]}' if quarterly else date(k[0], k[1] + 1, 1).strftime('%b %Y')

    out: dict[tuple[int, int], dict[str, Any]] = {}
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        k = key(date(y, m, 1))
        out.setdefault(k, {'label': label(k), 'a': 0})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    for x in payments:
        if x.get('date'):
            out[key(date.fromisoformat(x['date']))]['a'] += x['amount']
    return [out[k] for k in sorted(out)]


def build_bancassurance(gateway: WarehouseGateway, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
    if gateway.get_customer(cust_id) is None:
        return None
    try:
        data = gateway.get_bancassurance(cust_id, period)
    except Exception:
        return _unavailable(cust_id, 'Bancassurance')
    if data is None:
        return _empty(cust_id, 'Bancassurance', 'No insurance policies linked to this customer.')

    policies = data['policies']
    live = data_mode() == 'live'
    st = LIVE if live else PREVIEW
    receipts = data.get('receipts') or {}
    paid = receipts.get('amount_paid') if receipts.get('amounts_available') else None
    paid_metric = ([_metric('Premiums paid', paid, 'KES', status=st,
                            meta=f"{receipts['receipts']} receipt{'' if receipts['receipts'] == 1 else 's'}, "
                                 'all years')]
                   if paid is not None else [])

    if not policies:
        # Receipts prove premiums were paid, but no policy record survived the
        # insurance extract. A live "KES 0 in force / 0 policies" would state as fact
        # what the source cannot say, so those read as not stated, and what IS known
        # (the premiums paid) leads.
        unknown = 'no policy record in the insurance extract'
        return {
            'cust_id': cust_id, 'domain': 'Bancassurance', 'preview': not live, 'period': period.to_dict(),
            'metrics': [{**m, 'lead': True} for m in paid_metric] + [
                _metric('Annual premium in force', None, 'KES', status=TO_SOURCE, meta=unknown),
                _metric('Sum insured in force', None, 'KES', status=TO_SOURCE, meta=unknown),
                _metric('Policies', None, 'count', status=TO_SOURCE, meta=unknown),
            ],
            'match_note': data.get('match_note'),
            'claims': data.get('claims'),
            'coverage': {'active': 0, 'expired': 0, 'unnumbered': 0,
                         'matched_by_phone': data.get('phone_matched', 0),
                         'matched_by_name': data.get('name_matched', 0)},
            'charts': [],
            'note': data.get('match_note'),
            'tables': [],
        }

    # Per-policy monthly payment = premium spread over the policy's term of cover
    # (annual by default). Injected onto each row so the table shows it too.
    for p in policies:
        p['monthly'] = round(p['premium'] / _term_months(p.get('start'), p.get('end')))
        # The feed carries a policy number on 1,316 of 58,504 rows. Say it is absent
        # rather than leaving a blank cell that looks like a failure to load.
        if not p.get('policy'):
            p['policy'] = 'No policy number on file'

    claims = data.get('claims')
    # Premium, monthly and sum insured are for policies IN FORCE. The book repeats a
    # policy at every annual renewal, so summing every row added years of expired
    # renewals together (one customer: 66 rows over 9 years, 1 active).
    active = [p for p in policies if str(p.get('status', '')).lower() == 'active']
    n_active = len(active)
    total_premium = sum(p['premium'] for p in active)
    total_insured = sum(p['sum_insured'] for p in active)
    total_monthly = sum(p['monthly'] for p in active)
    lifetime_premium = sum(p['premium'] for p in policies)
    years = sorted(str(p.get('start') or p.get('end') or '')[:4] for p in policies
                   if str(p.get('start') or p.get('end') or '')[:4].isdigit())

    # Premium by class of cover, kept ONLY when more than one class is held (one
    # slice is not a chart). The summary's own product is blank on every row, so the
    # class comes from the policy feed (gateway _policy_cover); a policy whose class
    # could not be read keeps the description "Insurance policy" and is left out.
    by_product: dict[str, int] = {}
    for p in policies:
        k = p.get('cover_class') or p['product']
        by_product[k] = by_product.get(k, 0) + p['premium']
    named_products = {k: v for k, v in by_product.items() if k and k != 'Insurance policy'}
    by_insurer: dict[str, int] = {}
    for p in policies:
        if p.get('insurer'):
            by_insurer[p['insurer']] = by_insurer.get(p['insurer'], 0) + p['premium']

    # The next renewal: the soonest end date among policies still in force.
    today = date.today().isoformat()
    upcoming = sorted(str(p['end']) for p in policies
                      if str(p.get('status', '')).lower() == 'active' and p.get('end') and str(p['end']) >= today)
    next_renewal = date.fromisoformat(upcoming[0][:10]).strftime('%d %b %Y') if upcoming else None

    # These are annual renewals, so the year is the axis the data supports. It turns
    # 28 identical bars into a handful of meaningful ones and answers the question an
    # RM actually has: is this relationship growing or shrinking?
    by_year: dict[str, int] = {}
    for p in policies:
        term = p.get('end') or p.get('start')
        year = str(term)[:4] if term else None
        if year and year.isdigit():
            by_year[year] = by_year.get(year, 0) + p['premium']
    year_rows = [{'label': y, 'value': v, 'colorRole': 1}
                 for y, v in sorted(by_year.items())]

    # Sum insured is zero on 21% of the book, so the total is only meaningful when
    # something carries a figure. A bare KES 0 reads as "no cover", which is a
    # different claim from "the feed does not say".
    insured_known = sum(1 for p in active if p['sum_insured'])

    return {
        'cust_id': cust_id, 'domain': 'Bancassurance', 'preview': not live, 'period': period.to_dict(),
        'metrics': [
            _metric('Annual premium in force', total_premium, 'KES', lead=True, status=st,
                    meta=(f"{n_active} active polic{'y' if n_active == 1 else 'ies'}" if n_active
                          else 'no policy currently active')),
            _metric('Monthly payments', total_monthly, 'KES', status=st,
                    meta='on active policies' if n_active else None),
            (_metric('Sum insured in force', total_insured, 'KES', status=st,
                     meta=(None if insured_known == n_active
                           else f'{n_active - insured_known} of {n_active} '
                                f'active policies carry no sum insured'))
             if insured_known else
             _metric('Sum insured in force', None, 'KES', status=TO_SOURCE,
                     meta=('not stated on the active policies' if n_active
                           else 'no policy currently active'))),
            _metric('Premium on all policies', lifetime_premium, 'KES', status=st,
                    meta=(f'every renewal since {years[0]}' if years else None)),
            _metric('Policies', len(policies), 'count', status=st,
                    meta=((f'{n_active} active' + (f', next renewal {next_renewal}' if next_renewal else ''))
                          if n_active else 'none currently active')),
        ] + paid_metric + ([
            # Only when there IS a claim. 223 exist across 11,105 clients, so this
            # tile is absent almost always - and when it appears it is the most
            # important thing on the panel. Walking into a renewal without knowing
            # there is an open claim is the failure this prevents.
            _metric('Claims', claims['total'], 'count', status=st,
                    meta=', '.join(f'{n} {label.lower()}'
                                   for label, n in sorted(claims['by_status'].items())))
        ] if claims else []),
        # How these policies were linked to this customer, when it was by something
        # weaker than a national ID. Null when every one matched on ID.
        'match_note': data.get('match_note'),
        # Claims are rare (223 across the whole book) and that is why they are on the
        # page rather than in a report. None when this customer has never claimed.
        'claims': data.get('claims'),
        'coverage': {
            'active': n_active,
            'expired': len(policies) - n_active,
            # 98% of the policy feed carries no policy number. Stated, so a blank
            # column reads as a gap in the source rather than a rendering fault.
            'unnumbered': data.get('unnumbered', 0),
            'matched_by_phone': data.get('phone_matched', 0),
            'matched_by_name': data.get('name_matched', 0),
        },
        'charts': [
            c for c in [
                ({'kind': 'bars', 'id': 'by_year', 'title': 'Premium by year',
                  'question': 'Is this insurance relationship growing or shrinking?',
                  'status': st, 'fmt': 'kes', 'data': year_rows}
                 if len(year_rows) > 1 else None),
                # Only when more than one class is held; one class is said in the table.
                ({'kind': 'donut', 'id': 'mix', 'title': 'Premium by class of cover, all policies',
                  'question': "What's insured, and where is the premium concentrated?",
                  'status': st, 'fmt': 'kes',
                  'data': [{'label': prod, 'value': val} for prod, val in
                           sorted(named_products.items(), key=lambda kv: kv[1], reverse=True)]}
                 if len(named_products) > 1 else None),
                ({'kind': 'bars', 'id': 'by_insurer', 'title': 'Premium by insurer, all policies',
                  'question': 'Which insurers carry this customer’s cover?',
                  'status': st, 'fmt': 'kes',
                  'data': [{'label': k, 'value': v, 'colorRole': 1} for k, v in
                           sorted(by_insurer.items(), key=lambda kv: kv[1], reverse=True)]}
                 if len(by_insurer) > 1 else None),
            ] if c
        ],
        # How the policies were linked, when by something weaker than a national ID.
        'note': data.get('match_note'),
        'tables': [
            {'id': 'policies', 'title': 'Policies held', 'status': st,
             'columns': ['policy', 'product', 'insurer', 'premium', 'monthly', 'sum_insured',
                         'status', 'end', 'matched_by'],
             'rows': policies, 'sum_columns': ['premium', 'monthly']},
        ] + ([
            # Rare and high-salience: an open claim changes the renewal conversation.
            {'id': 'claims', 'title': 'Claims', 'status': st,
             'note': ((f"Latest incident {claims['latest_incident']}. " if claims.get('latest_incident') else '')
                      + 'Most claims carry no estimated loss in the source, so no amount is totalled.'),
             'columns': ['status', 'claims'],
             'rows': [{'status': k, 'claims': n} for k, n in sorted(claims['by_status'].items())]},
        ] if claims else []),
    }


DOMAIN_BUILDERS = {
    'whizz': build_whizz,
    'properties': build_properties,
    'bancassurance': build_bancassurance,
}
