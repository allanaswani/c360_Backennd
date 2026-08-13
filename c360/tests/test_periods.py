"""Period resolution + the year-stamped movement-column name generation."""
from datetime import date

from django.test import SimpleTestCase

from c360.warehouse.periods import month_column_name, resolve_period


class PeriodTests(SimpleTestCase):
    AS_OF = date(2026, 7, 22)

    def test_presets_resolve(self):
        self.assertEqual(resolve_period('7D', as_of=self.AS_OF).start, date(2026, 7, 15))
        self.assertEqual(resolve_period('QTD', as_of=self.AS_OF).start, date(2026, 7, 1))
        self.assertEqual(resolve_period('YTD', as_of=self.AS_OF).start, date(2026, 1, 1))

    def test_unknown_token_defaults_to_30d(self):
        self.assertEqual(resolve_period('bogus', as_of=self.AS_OF).token, '30D')

    def test_custom_range_orders_endpoints(self):
        p = resolve_period('CUSTOM', as_of=self.AS_OF, start='2026-03-10', end='2026-01-01')
        self.assertEqual(p.start, date(2026, 1, 1))
        self.assertEqual(p.end, date(2026, 3, 10))

    def test_month_anchors_span_range(self):
        p = resolve_period('YTD', as_of=self.AS_OF)
        anchors = p.month_anchors()
        self.assertEqual(anchors[0], date(2026, 1, 1))
        self.assertEqual(anchors[-1], date(2026, 7, 1))

    def test_month_column_name_is_year_stamped(self):
        # The whole point: names are generated, never hardcoded, so the Jan
        # schema rollover can't break queries.
        self.assertEqual(month_column_name(date(2026, 1, 1)), 'jan_26_bal')
        self.assertEqual(month_column_name(date(2025, 12, 1)), 'dec_25_bal')
