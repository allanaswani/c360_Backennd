"""Level 1 aggregation — totals must reconcile, provenance must be honest."""
from datetime import date

from django.test import SimpleTestCase

from c360.services.portfolio import build_portfolio_overview
from c360.warehouse.mock.mock_gateway import MockWarehouse
from c360.warehouse.periods import resolve_period


class PortfolioTests(SimpleTestCase):
    def setUp(self):
        self.gw = MockWarehouse()
        self.period = resolve_period('30D', as_of=date(2026, 7, 22))

    def test_totals_reconcile_with_customer_list(self):
        customers = self.gw.list_customers(sales_codes=None)
        expected_value = sum(c['value'] for c in customers)
        ov = build_portfolio_overview(self.gw, None, self.period)
        self.assertEqual(ov['summary']['customers']['value'], len(customers))
        self.assertEqual(ov['summary']['relationship_value']['value'], expected_value)

    def test_summary_and_segment_mix_are_live(self):
        ov = build_portfolio_overview(self.gw, None, self.period)
        self.assertEqual(ov['summary']['deposits']['status'], 'live')
        self.assertEqual(ov['segment_mix']['status'], 'live')

    def test_risk_and_trends_are_flagged(self):
        ov = build_portfolio_overview(self.gw, None, self.period)
        # Risk distribution is now DERIVED from real balances (leverage), not simulated.
        self.assertEqual(ov['risk_distribution']['status'], 'derived')
        self.assertTrue(ov['risk_distribution']['note'])          # honest caveat present
        # Trends still need a historical snapshot → preview.
        self.assertEqual(ov['book_trend']['status'], 'preview')
        self.assertEqual(ov['segment_value_trend']['status'], 'preview')
        self.assertEqual(ov['top_movers']['status'], 'preview')

    def test_risk_distribution_sums_to_customer_count(self):
        ov = build_portfolio_overview(self.gw, None, self.period)
        total = sum(r['customers'] for r in ov['risk_distribution']['rows'])
        self.assertEqual(total, ov['summary']['customers']['value'])

    def test_scoping_reduces_the_book(self):
        scoped = build_portfolio_overview(self.gw, ['SC-1077'], self.period)
        full = build_portfolio_overview(self.gw, None, self.period)
        self.assertLess(scoped['scope']['customers_in_view'], full['scope']['customers_in_view'])

    def test_segment_value_trend_rows_carry_every_segment(self):
        ov = build_portfolio_overview(self.gw, None, self.period)
        segs = ov['segment_value_trend']['segments']
        for row in ov['segment_value_trend']['data']:
            for s in segs:
                self.assertIn(s, row)
