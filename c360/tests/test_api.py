"""API smoke + RBAC enforcement at the HTTP layer, and the never-a-silent-dash rule."""
from django.conf import settings
from django.test import Client, TestCase

from c360.services import portfolio_cache
from c360.warehouse.factory import get_gateway


class ApiTests(TestCase):
    def setUp(self):
        self.c = Client()
        portfolio_cache.invalidate()  # cache is process-global; start clean
        # These tests exercise the seeded roster, so pin mock regardless of the
        # ambient DATA_MODE (which may be 'live' for the running app).
        settings.C360['DATA_MODE'] = 'mock'
        get_gateway.cache_clear()

    def test_meta(self):
        r = self.c.get('/api/meta/')
        self.assertEqual(r.status_code, 200)
        self.assertIn(r.json()['data_mode'], ('mock', 'live'))

    def test_customer_detail_shape(self):
        r = self.c.get('/api/customers/HF-100238/')
        self.assertEqual(r.status_code, 200)
        body = r.json()
        # identity is live; risk/KYC are DERIVED from live data; CRB stays not-sourced.
        self.assertEqual(body['header']['identity']['name']['status'], 'live')
        self.assertEqual(body['header']['risk']['risk_class']['status'], 'derived')
        self.assertEqual(body['header']['risk']['kyc_status']['status'], 'derived')
        self.assertEqual(body['header']['risk']['crb_status']['status'], 'to_source')
        # never a silent dash — the derived values are real strings.
        self.assertIn(body['header']['risk']['risk_class']['value'], ('Low', 'Medium', 'High'))

    def test_rm_cannot_reach_customer_outside_book(self):
        r = self.c.get('/api/customers/HF-100238/', HTTP_X_C360_ROLE='rm', HTTP_X_C360_SALES_CODES='SC-1077')
        self.assertEqual(r.status_code, 403)

    def test_customer_detail_has_plain_language_summary(self):
        r = self.c.get('/api/customers/HF-100238/')
        summary = r.json()['header'].get('summary')
        self.assertTrue(summary)
        self.assertIn('customer', summary.lower())   # a real sentence, not a bare value
        self.assertTrue(summary.endswith('.'))

    def test_hfcb_domain_has_all_charts_with_questions(self):
        r = self.c.get('/api/customers/HF-100238/domains/hfcb/?period=30D')
        self.assertEqual(r.status_code, 200)
        charts = r.json()['charts']
        self.assertEqual(set(charts), {'balance_trend', 'disbursement_vs_balance',
                                       'product_holdings', 'transaction_trend', 'channel_usage'})
        for c in charts.values():
            self.assertTrue(c.get('question'))   # every chart answers a stated question

    def test_preview_domain_empty_state_is_explained(self):
        # Retail customer with no mortgage → Properties has an honest empty reason.
        r = self.c.get('/api/customers/HF-100571/domains/properties/?period=30D')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['empty_reason'])

    def test_portfolio_overview_and_cache(self):
        r1 = self.c.get('/api/portfolio/overview/?period=30D')
        self.assertEqual(r1.status_code, 200)
        self.assertFalse(r1.json()['cache']['cached'])
        r2 = self.c.get('/api/portfolio/overview/?period=30D')
        self.assertTrue(r2.json()['cache']['cached'])   # second hit served from cache

    def test_portfolio_rm_without_book_is_forbidden(self):
        r = self.c.get('/api/portfolio/overview/', HTTP_X_C360_ROLE='rm')
        self.assertEqual(r.status_code, 403)

    def test_customer_overview_value_by_domain_provenance(self):
        # Flagship customer: HFCB value is LIVE, non-core domains are PREVIEW.
        r = self.c.get('/api/customers/HF-102377/overview/?period=30D')
        self.assertEqual(r.status_code, 200)
        slices = {s['domain']: s['status'] for s in r.json()['value_by_domain']['slices']}
        self.assertEqual(slices['HFCB'], 'live')
        self.assertEqual(slices['Whizz'], 'preview')
        self.assertEqual(r.json()['relationship_trend']['status'], 'preview')

    def test_customer_overview_empty_domain_snapshot(self):
        # Retail customer with no mortgage / policies → those snapshots are not-sourced,
        # never a fake value, and are excluded from the value donut.
        r = self.c.get('/api/customers/HF-100571/overview/?period=30D')
        snaps = {s['domain']: s for s in r.json()['domain_snapshots']}
        self.assertIsNone(snaps['Properties']['value'])
        self.assertEqual(snaps['Properties']['status'], 'to_source')
        domains_in_donut = {s['domain'] for s in r.json()['value_by_domain']['slices']}
        self.assertNotIn('Properties', domains_in_donut)


class DomainUnavailableTests(TestCase):
    """Honesty rule: a domain whose SOURCE can't be read must say 'couldn't load',
    never masquerade as 'this customer has nothing' (a genuine None)."""

    def _period(self):
        from c360.warehouse.periods import resolve_period
        from datetime import date
        return resolve_period('30D', as_of=date(2026, 8, 18))

    def test_source_failure_is_unavailable_not_empty(self):
        from c360.services import domains

        class Boom:
            def get_customer(self, cid):
                return {'cust_id': cid}
            def get_properties(self, cid):
                raise RuntimeError('source table unreachable')
            def get_bancassurance(self, cid, period):
                raise RuntimeError('source table unreachable')

        gw, p = Boom(), self._period()
        prop = domains.build_properties(gw, 'X', p)
        banc = domains.build_bancassurance(gw, 'X', p)
        self.assertTrue(prop['unavailable'])
        self.assertTrue(banc['unavailable'])
        self.assertTrue(prop['empty_reason'])           # still explained, never bare

    def test_genuine_none_is_empty_not_unavailable(self):
        from c360.services import domains

        class NoneGw:
            def get_customer(self, cid):
                return {'cust_id': cid}
            def get_properties(self, cid):
                return None                              # customer genuinely owns none
            def get_bancassurance(self, cid, period):
                return None

        gw, p = NoneGw(), self._period()
        prop = domains.build_properties(gw, 'X', p)
        banc = domains.build_bancassurance(gw, 'X', p)
        self.assertFalse(prop.get('unavailable'))
        self.assertFalse(banc.get('unavailable'))
        self.assertTrue(prop['empty_reason'])           # honest, explained empty state
