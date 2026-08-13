"""Exercise every live service payload for one real customer + the portfolio sample.

Forces DATA_MODE=live in-process, then runs the actual service/engine code (not
ad-hoc SQL) so the output is exactly what the API would return. Read-only.

    python manage.py validate_live_full 121669
"""
from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand

from c360.recommendations.engine import recommend_for_customer
from c360.services.customer import build_customer_header, build_value_summary
from c360.services.domains import build_whizz, build_properties, build_bancassurance
from c360.services.hfcb import build_hfcb_domain
from c360.services.overview import build_customer_overview
from c360.services.portfolio import build_portfolio_overview
from c360.warehouse.factory import get_gateway
from c360.warehouse.periods import resolve_period


class Command(BaseCommand):
    help = 'Validate all live service payloads (read-only).'

    def add_arguments(self, parser):
        parser.add_argument('cust_id', nargs='?', default='121669')
        parser.add_argument('--period', default='90D')

    def handle(self, *args, **opts):
        settings.C360['DATA_MODE'] = 'live'
        get_gateway.cache_clear()
        gw = get_gateway()
        cid = opts['cust_id']
        period = resolve_period(opts['period'], as_of=gw.as_of_date())

        def ok(msg):
            self.stdout.write(self.style.SUCCESS(msg))

        def timeit(label, fn):
            t0 = time.time()
            try:
                r = fn()
                self.stdout.write(f'  {label:26} OK   ({time.time() - t0:.1f}s)')
                return r
            except Exception as e:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f'  {label:26} FAIL {type(e).__name__}: {e}'))
                return None

        ok(f'\n=== Level 2/3 for {cid} (period {period.token}) ===')
        header = timeit('customer_header', lambda: build_customer_header(gw, cid))
        timeit('value_summary', lambda: build_value_summary(gw, cid))
        overview = timeit('customer_overview', lambda: build_customer_overview(gw, cid, period))
        hfcb = timeit('hfcb_domain', lambda: build_hfcb_domain(gw, cid, period))
        rec = timeit('recommendations', lambda: recommend_for_customer(gw, cid))
        timeit('whizz_domain', lambda: build_whizz(gw, cid, period))
        timeit('properties_domain', lambda: build_properties(gw, cid, period))
        timeit('bancassurance_domain', lambda: build_bancassurance(gw, cid, period))

        if header:
            idn = header['identity']
            ok('\nIdentity:')
            self.stdout.write(f"  {idn['name']['value']}  · {idn['segment']['value']} · {idn['branch']['value']}")
            self.stdout.write(f"  name={idn['name']['status']} rel_since={header['risk']['relationship_since']['status']} "
                              f"risk={header['risk']['risk_class']['status']}")
        if hfcb:
            m = hfcb['metrics']
            ok('\nHFCB metrics (live):')
            self.stdout.write(f"  deposits={m['total_deposits']['value']:,} loans={m['total_loans']['value']:,} "
                              f"products={m['products_held']['value']}")
            ch = hfcb['charts']
            for name in ('balance_trend', 'disbursement_vs_balance', 'transaction_trend'):
                pts = sum(len(s['points']) for s in ch[name]['series'])
                st = ch[name]['series'][0]['status'] if ch[name]['series'] else 'n/a'
                self.stdout.write(f"  {name:24} points={pts:<4} status={st}")
            self.stdout.write(f"  channel_usage            slices={len(ch['channel_usage']['slices'])} status={ch['channel_usage']['status']}")
            self.stdout.write(f"  recent_transactions      rows={len(hfcb['tables']['recent_transactions']['rows'])}")
            for s in ch['channel_usage']['slices']:
                self.stdout.write(f"      {s['channel']:10} {s['share'] * 100:.0f}%")
        if overview:
            ok('\nOverview relationship_trend:')
            rt = overview['relationship_trend']
            self.stdout.write(f"  status={rt['status']} points={len(rt['series'])}")
        if rec:
            ok('\nRecommendations:')
            self.stdout.write(f"  status={rec.status} items={len(rec.items)} withheld={len(rec.withheld)}")
            for it in (rec.items or rec.withheld):
                self.stdout.write(f"      [{it['rule_id']}] {it['product_name']} — {it['reason'][:70]}")

        ok('\n=== Level 1 portfolio (bounded live sample) ===')
        t0 = time.time()
        pf = timeit('portfolio_overview', lambda: build_portfolio_overview(gw, None, period))
        if pf:
            s = pf['summary']
            self.stdout.write(f"  customers={s['customers']['value']:,} value={s['relationship_value']['value']:,} "
                              f"avg_products={s['avg_products']['value']}")
            self.stdout.write(f"  sample_note: {pf['scope'].get('sample_note')}")
            self.stdout.write(f"  segments: {', '.join(r['segment'] for r in pf['segment_mix']['rows'][:6])}")
        self.stdout.write(f'  (portfolio build {time.time() - t0:.1f}s)')
        ok('\nDone.')
