"""The book page must agree with the RM Portfolio tool, or say why it does not.

For Robin Magerer (MR4087) the two screens showed:

                       C360          RM Portfolio
    Deposits           47.2M         55.77M
    Loans             138.6M        250.82M
    Customers            191           220

None of those were arithmetic bugs. They were different definitions, and the
portfolio tool's are the ones the business quotes. Its own source
(hf_group_backend/services/portfolio_service.py) documents having already hit this:
a customer-master aggregate on its own refresh cycle is not an RM's live position,
and the figure the business quotes comes from the balance-movement tables keyed on
the ACCOUNT's rm_code.

These tests pin the three rules that are easy to get wrong and expensive when wrong.
"""
from datetime import date

from django.test import SimpleTestCase

from c360.warehouse import rm_balances as rm
from c360.warehouse.trino.trino_gateway import TrinoWarehouse

SALES_CODE = 'MR4087'


class ClosedMonthTests(SimpleTestCase):
    """The running month is never a rung."""

    def test_the_current_month_is_excluded(self):
        # Mid-September: sep_26_bal exists and holds a part-month total, which is not
        # a month-end balance. Their first version treating it as one is how an RM
        # sitting on 296 million was shown 0.00.
        months = rm.closed_months(date(2026, 9, 18))
        self.assertNotIn('sep_26_bal', [c for c, _ in months])
        self.assertEqual(months[0][0], 'aug_26_bal')

    def test_most_recent_first(self):
        months = rm.closed_months(date(2026, 9, 18))
        self.assertEqual([c for c, _ in months][:3],
                         ['aug_26_bal', 'jul_26_bal', 'jun_26_bal'])

    def test_it_walks_back_across_a_year_boundary(self):
        months = [c for c, _ in rm.closed_months(date(2026, 2, 3))]
        self.assertEqual(months[0], 'jan_26_bal')
        self.assertEqual(months[1], 'dec_25_bal')

    def test_february_in_a_leap_year(self):
        labels = dict((c, l) for c, l in rm.closed_months(date(2024, 4, 1)))
        self.assertEqual(labels['feb_24_bal'], '29 February 2024')

    def test_february_in_a_common_year(self):
        labels = dict((c, l) for c, l in rm.closed_months(date(2026, 4, 1)))
        self.assertEqual(labels['feb_26_bal'], '28 February 2026')


class LadderTests(SimpleTestCase):
    """The columns are asked of the catalogue, never assumed."""

    def test_only_columns_that_exist_become_rungs(self):
        # Older periods are QUARTERLY: sep_25_bal exists, oct_25_bal does not. The
        # date is pinned because the ladder walks back a bounded number of months, so
        # naming a real column is otherwise only true for part of a year.
        cols = {'yester_1_bal', 'sep_25_bal', 'rm_code'}
        rungs = [c for c, _ in rm.ladder(cols, date(2026, 9, 18))]
        self.assertIn('yester_1_bal', rungs)
        self.assertIn('sep_25_bal', rungs)
        self.assertNotIn('oct_25_bal', rungs)

    def test_yesterday_outranks_any_month_end(self):
        cols = {'yester_1_bal', 'yester_2_bal', 'aug_26_bal'}
        self.assertEqual([c for c, _ in rm.ladder(cols, date(2026, 9, 18))][:2],
                         ['yester_1_bal', 'yester_2_bal'])

    def test_a_table_with_no_known_columns_yields_no_rungs(self):
        self.assertEqual(rm.ladder({'id', 'rm_code'}), [])


class PickTests(SimpleTestCase):
    """The overnight load does not always land."""

    def test_the_freshest_positive_rung_wins(self):
        rungs = rm.ladder({'yester_1_bal', 'yester_2_bal', 'aug_26_bal'}, date(2026, 9, 18))
        value, label = rm.pick({'yester_1_bal': 55_770_000, 'yester_2_bal': 1,
                                'aug_26_bal': 2}, rungs)
        self.assertEqual(value, 55_770_000)
        self.assertEqual(label, 'yesterday')

    def test_it_falls_through_when_last_night_did_not_post(self):
        """On the day they investigated, the loan file had not posted at all and
        yester_1_bal was empty across all 233 of that RM's loan accounts."""
        rungs = rm.ladder({'yester_1_bal', 'yester_2_bal', 'aug_26_bal'}, date(2026, 9, 18))
        value, label = rm.pick({'yester_1_bal': None, 'yester_2_bal': 0,
                                'aug_26_bal': 250_820_000}, rungs)
        self.assertEqual(value, 250_820_000)
        self.assertIn('August', label)

    def test_nothing_anywhere_is_unknown_not_zero(self):
        """An RM with no rows has an unknown position, not an empty one, and the
        caller must render that as 'not available'."""
        rungs = rm.ladder({'yester_1_bal', 'aug_26_bal'}, date(2026, 9, 18))
        self.assertEqual(rm.pick({'yester_1_bal': None, 'aug_26_bal': None}, rungs),
                         (0.0, None))

    def test_a_non_numeric_value_is_skipped_not_crashed_on(self):
        rungs = rm.ladder({'yester_1_bal', 'aug_26_bal'}, date(2026, 9, 18))
        value, _ = rm.pick({'yester_1_bal': 'n/a', 'aug_26_bal': 7}, rungs)
        self.assertEqual(value, 7)


class SqlTests(SimpleTestCase):
    def test_balances_key_on_the_account_rm_code(self):
        """Not on who the customer is allocated to. An account this RM manages counts
        even when the customer sits in someone else's allocation, which is most of
        why the loan figures diverged."""
        sql, _ = rm.build_sql('daily_balance_movement',
                              {'yester_1_bal', 'rm_code', 'customer_segment'},
                              has_segment=True)
        self.assertIn('TRIM(rm_code) = TRIM(%s)', sql)

    def test_non_customer_money_is_excluded(self):
        sql, _ = rm.build_sql('daily_balance_movement',
                              {'yester_1_bal', 'customer_segment'}, has_segment=True)
        self.assertIn('customer_segment', sql)
        self.assertEqual(rm.EXCLUDED_SEGMENTS, ('INTERNAL ACCOUNTS', 'VIRTUAL'))

    def test_the_segment_filter_is_omitted_when_the_column_is_absent(self):
        sql, _ = rm.build_sql('loan_daily_balance_movement',
                              {'yester_1_bal'}, has_segment=False)
        self.assertNotIn('customer_segment', sql)

    def test_every_rung_is_summed_in_one_pass(self):
        sql, rungs = rm.build_sql('daily_balance_movement',
                                  {'yester_1_bal', 'yester_2_bal', 'aug_26_bal'},
                                  has_segment=False, today=date(2026, 9, 18))
        self.assertEqual(len(rungs), 3)
        self.assertEqual(sql.count('FILTER (WHERE'), 3)

    def test_a_table_with_nothing_usable_returns_no_query(self):
        self.assertEqual(rm.build_sql('x', {'id'}, has_segment=False), ('', []))


class _ReportingPG:
    """The reporting Postgres, answering as the portfolio tool's tables do."""

    def __init__(self, *, deposits=55_770_000, loans=250_820_000, customers=220, fail=False):
        self.deposits = deposits
        self.loans = loans
        self.customers = customers
        self.fail = fail

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError('reporting postgres down')
        s = sql.lower()
        if 'information_schema.columns' in s:
            return [{'column_name': c} for c in
                    ('rm_code', 'customer_segment', 'yester_1_bal', 'yester_2_bal',
                     'aug_26_bal')]
        if 'retail_allocated_portfolio' in s:
            return [{'n': self.customers}]
        if 'daily_balance_movement' in s:
            value = self.loans if s.startswith('select') and 'loan_daily' in s else self.deposits
            return [{'yester_1_bal': value, 'yester_2_bal': None, 'aug_26_bal': None}]
        return []


class GatewayTests(SimpleTestCase):
    def test_it_reports_the_portfolio_tool_figures(self):
        gw = TrinoWarehouse(object(), postgres=_ReportingPG())
        out = gw.get_rm_balances(SALES_CODE)
        self.assertEqual(out['deposits'], 55_770_000)
        self.assertEqual(out['loans'], 250_820_000)
        self.assertEqual(out['deposits_as_at'], 'yesterday')

    def test_book_size_counts_the_allocation_table_the_portfolio_tool_counts(self):
        gw = TrinoWarehouse(object(), postgres=_ReportingPG())
        self.assertEqual(gw.get_rm_book_size(SALES_CODE), 220)

    def test_absent_postgres_declines_rather_than_reporting_zero(self):
        gw = TrinoWarehouse(object(), postgres=None)
        self.assertIsNone(gw.get_rm_balances(SALES_CODE))
        self.assertIsNone(gw.get_rm_book_size(SALES_CODE))

    def test_a_failure_declines_rather_than_reporting_zero(self):
        """A balance that silently reads zero is worse than one that is absent: the
        screen shows a stale or empty figure as if it were current."""
        gw = TrinoWarehouse(object(), postgres=_ReportingPG(fail=True))
        self.assertIsNone(gw.get_rm_balances(SALES_CODE))
        self.assertIsNone(gw.get_rm_book_size(SALES_CODE))

    def test_no_sales_code_means_no_rm_view(self):
        gw = TrinoWarehouse(object(), postgres=_ReportingPG())
        self.assertIsNone(gw.get_rm_balances(None))
