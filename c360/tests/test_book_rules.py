"""Whole-book deposit/loan totals follow the bank's ETL definition (book_rules)."""
from datetime import date

from django.test import SimpleTestCase

from c360.warehouse import book_rules
from c360.warehouse.trino.trino_gateway import TrinoWarehouse


class BookRulesTests(SimpleTestCase):
    def test_lists_copied_from_etl(self):
        self.assertEqual(book_rules.DEP_EXCLUDED_PRODUCTS, (31572, 40060))
        self.assertEqual(len(book_rules.DEP_EXCLUDED_ACCOUNTS), 31)
        self.assertEqual(len(book_rules.DEP_CASH_MARGINS), 56)
        self.assertEqual(len(set(book_rules.LOAN_EXCLUDED_CUSTOMERS)),
                         len(book_rules.LOAN_EXCLUDED_CUSTOMERS))

    def test_cash_margins_respect_dates_and_add_up_per_account(self):
        m = book_rules.cash_margins_on(date(2026, 11, 2))
        self.assertEqual(round(sum(m.values())), 497866157)
        # Two TODDY rules plus its LG rule overlap on one account and must add.
        self.assertAlmostEqual(m['9783731325'], 15628474.95 + 8965815 + 10494079)
        self.assertEqual(book_rules.cash_margins_on(date(2020, 1, 1)), {})

    def test_deposit_filter_keeps_null_product_and_account(self):
        sql = TrinoWarehouse._dep_book_filter('e.')
        self.assertIn('COALESCE(TRY_CAST(e.id_product AS integer), -1) NOT IN (31572,40060)', sql)
        self.assertIn("COALESCE(TRIM(e.account_no), '') NOT IN ('4339973201'", sql)

    def test_loan_filter_positive_gross_and_shortfalls(self):
        sql = TrinoWarehouse._loan_book_filter('e.')
        self.assertIn('e.lc_gross_total > 0', sql)
        self.assertIn('e.cust_id NOT IN (', sql)

    def test_cash_margin_join(self):
        j = TrinoWarehouse._cash_margin_join(date(2026, 11, 2))
        self.assertIn("('9783731325', 35088368.95e0)", j)
        self.assertIn('ON cm.acc = TRIM(e.account_no)', j)
        self.assertIn('ON FALSE', TrinoWarehouse._cash_margin_join(date(2020, 1, 1)))
