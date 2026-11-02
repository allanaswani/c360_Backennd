"""Revenue quarter-on-quarter trend (customer_profile.revenue_trend). The figures are
customer 37565's live 2026 net revenue, checked by hand on 2026-11-02."""
from django.test import SimpleTestCase

from c360.customer_profile import revenue, revenue_trend

NET = [165883, 146533, 184628, 148448, 161480, 144135, 152323, 150364, 134329, 156337]
SERIES = [{'period': f'2026-{i + 1:02d}-01', 'net': v} for i, v in enumerate(NET)]


class RevenueTrendTests(SimpleTestCase):
    def test_last_three_complete_months_against_the_three_before(self):
        t = revenue_trend(SERIES, year=2026, loaded_through='2026-10-31')
        self.assertEqual((t['recent'], t['prior'], t['change_pct'], t['direction']),
                         (441030, 457938, -3.7, 'flat'))
        self.assertEqual((t['recent_from'], t['prior_to']), ('2026-08-01', '2026-07-01'))

    def test_part_loaded_month_is_left_out(self):
        t = revenue_trend(SERIES, year=2026, loaded_through='2026-10-30')
        self.assertEqual((t['recent_from'], t['recent_to']), ('2026-07-01', '2026-09-01'))
        self.assertEqual(t['recent'], 152323 + 150364 + 134329)

    def test_missing_month_counts_as_zero_and_marks_a_fall(self):
        series = [p for p in SERIES if p['period'] != '2026-10-01']
        t = revenue_trend(series, year=2026, loaded_through='2026-10-31')
        self.assertEqual(t['direction'], 'down')

    def test_too_early_in_the_year_or_no_load_date(self):
        self.assertIsNone(revenue_trend(SERIES[:5], year=2026, loaded_through='2026-05-31'))
        self.assertIsNone(revenue_trend(SERIES, year=2026, loaded_through=None))

    def test_revenue_carries_the_trend(self):
        rows = [{'month': i + 1, 'category': 'Interest_Income', 'value': v} for i, v in enumerate(NET)]
        out = revenue(rows, year=2026, loaded_through='2026-10-31')
        self.assertEqual(out['trend']['recent'], 441030)
        self.assertIsNone(revenue(rows, year=2026)['trend'])
