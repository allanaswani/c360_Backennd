"""Cash flow, loan details, timeline, peers, salary timing, statement (2026-10-01).

Each case comes from live data checked while building: customer 1218821's mortgage
instalment matching her standing order, 121669's register reaching back past the
customer master's migration date, a construction mortgage's first drawdown being a
fraction of the facility.
"""
from datetime import date

from django.test import SimpleTestCase

from c360 import activity as A
from c360 import cashflow as CF
from c360 import peers as P
from c360 import timeline as TL
from c360.warehouse.trino.trino_gateway import TrinoWarehouse


class CashFlowTests(SimpleTestCase):
    def test_months_sources_and_uses(self):
        months = CF.month_starts(date(2026, 8, 1), date(2026, 10, 1))
        out = CF.shape([
            {'month': '2026-08-01', 'grp': 'Credits into a salary account', 'inflow': 275970, 'outflow': 0, 'n_in': 1, 'n_out': 0},
            {'month': '2026-08-01', 'grp': 'Standing orders', 'inflow': 0, 'outflow': 108087, 'n_in': 0, 'n_out': 1},
            {'month': '2026-09-01', 'grp': 'M-Pesa & Whizz', 'inflow': 0, 'outflow': 70076, 'n_in': 0, 'n_out': 1},
        ], months)
        self.assertEqual([m['period'] for m in out['months']], ['2026-08-01', '2026-09-01', '2026-10-01'])
        self.assertEqual(out['months'][0], {'period': '2026-08-01', 'in': 275970, 'out': -108087, 'net': 167883})
        self.assertEqual(out['months'][2]['net'], 0)                 # a quiet month is a zero, not missing
        self.assertEqual(out['total_in'], 275970)
        self.assertEqual(out['total_out'], 178163)
        self.assertEqual(out['uses'][0]['group'], 'Standing orders')
        self.assertEqual(out['sources'][0]['share'], 1.0)

    def test_nothing_moved_is_none(self):
        self.assertIsNone(CF.shape([], ['2026-09-01']))

    def test_group_sql_has_no_overlap_gaps_for_known_purposes(self):
        for needle in ("'Salary'", "'Credits into a salary account'", "'M-Pesa & Whizz'", "'PesaLink'",
                       "'Standing orders'", "'Charges & fees'", "'Other'"):
            self.assertIn(needle, CF.GROUP_SQL)


class TimelineTests(SimpleTestCase):
    def test_relationship_starts_at_the_earliest_record(self):
        out = TL.build(joined='2016-01-01',
                       deposits=[{'product': 'TAKE ON ACCOUNT', 'opened': '2014-07-28', 'status': '1'}],
                       loans=[])
        self.assertEqual(out['since'], '2014-07-28')
        self.assertEqual(out['events'][-1]['title'], 'Became a customer')

    def test_repeats_are_folded(self):
        deps = [{'product': 'TARGET SAVINGS ACCOUNT', 'opened': f'2019-0{m}-01', 'status': '3'} for m in range(1, 6)]
        loans = [{'product': 'MOBILE LOAN', 'opened': f'2023-0{m}-10', 'status': '3', 'amount': 5000} for m in range(1, 4)]
        loans.append({'product': 'PURCHASE OWNER OCCUPIER', 'opened': '2026-01-31', 'matures': '2038-01-31',
                      'status': '1', 'amount': 9_000_000})
        loans.append({'product': 'OVERDRAFT-FCY ACCOUNT', 'opened': '2024-01-31', 'matures': '2107-04-30',
                      'status': '1', 'amount': 0})
        out = TL.build(joined=None, deposits=deps, loans=loans)
        titles = [e['title'] for e in out['events']]
        self.assertIn('First Target Savings Account', titles)
        self.assertIn('3 mobile loans in 2023', titles)
        mort = next(e for e in out['events'] if e['title'] == 'Took Purchase Owner Occupier')
        self.assertEqual(mort['detail'], 'KES 9,000,000 facility, matures 2038-01-31, active')
        od = next(e for e in out['events'] if 'Overdraft' in e['title'])
        self.assertNotIn('2107', od['detail'])                       # placeholder maturity hidden
        self.assertEqual(out['counts']['mobile_loans'], 3)
        dates = [e['date'] for e in out['events']]
        self.assertEqual(dates, sorted(dates, reverse=True))         # newest first

    def test_empty_is_none(self):
        self.assertIsNone(TL.build(joined=None, deposits=[], loans=[]))


class PeerTests(SimpleTestCase):
    def test_position_and_zero_values(self):
        dist = {'customers': 1000, 'deposits': [float(i) for i in range(1, 100)],
                'loans': [0.0] * 99, 'products': [1.0] * 99, 'revenue': [0.0] * 90 + [10.0] * 9}
        out = P.position({'deposits': 95, 'loans': 0, 'products': 3, 'revenue': 50}, dist)
        m = {r['key']: r for r in out['measures']}
        self.assertEqual(m['deposits']['standing'], 'Top 5%')
        self.assertEqual(m['loans']['standing'], 'None held')        # no rank for nothing
        self.assertEqual(m['products']['standing'], 'Top 1%')
        self.assertEqual(m['revenue']['standing'], 'Top 1%')

    def test_low_values_say_bottom(self):
        dist = {'customers': 50, 'deposits': [float(i) for i in range(1, 100)]}
        m = P.position({'deposits': 10}, dist)['measures'][0]
        self.assertTrue(m['standing'].startswith('Bottom'))


class SalaryTimingTests(SimpleTestCase):
    def test_tight_days_give_a_window(self):
        self.assertEqual(A.salary_timing([28, 29, 31]), 'Salary usually lands on the 28th to 31st of the month.')
        self.assertEqual(A.salary_timing([25, 25]), 'Salary usually lands on the 25th of the month.')

    def test_spread_or_single_days_give_nothing(self):
        self.assertIsNone(A.salary_timing([2, 15, 28]))
        self.assertIsNone(A.salary_timing([28]))


class _Conn:
    def __init__(self, loans, sos):
        self.loans, self.sos = loans, sos

    def execute(self, sql, params=None):
        s = sql.lower()
        if 'bank_parameters' in s:
            return [{'d': date(2026, 9, 30)}]
        if 'max(eom_date)' in s:
            return [{'d': date(2026, 9, 30)}]
        if 'max(transaction_date)' in s:
            return [{'d': date(2026, 10, 1)}]
        if 'installment_amount' in s:
            return self.loans
        if "standing order payment" in s:
            return self.sos
        return []


class LoanDetailTests(SimpleTestCase):
    def test_standing_order_matching_the_instalment_is_found(self):
        loan = {'l2': 'PURCHASE MORTGAGES', 'product': 'PURCHASE OWNER OCCUPIER', 'acc': 'X', 'bal': 8439127.37,
                'inst': 108087.36, 'inst_own': 108087.36, 'next_dt': '2026-10-31', 'od': 0, 'ov': 5, 'matures': '2038-01-31',
                'rem': 136, 'tot': 144, 'rate': 9.5, 'st': 'Normal'}
        sos = [{'d': '2026-09-30', 'a': -108087.36, 'prod': 'MALIPO SALARY ACCOUNT'},
               {'d': '2026-08-31', 'a': -108087.36, 'prod': 'MALIPO SALARY ACCOUNT'},
               {'d': '2026-08-15', 'a': -5000, 'prod': 'MALIPO SALARY ACCOUNT'}]
        out = TrinoWarehouse(_Conn([loan], sos)).get_loan_details('1218821')['loans'][0]
        self.assertEqual(out['paid_by_standing_order'],
                         {'account': 'Malipo Salary Account', 'days': [30, 31], 'last': '2026-09-30'})
        self.assertEqual(out['type'], 'Purchase mortgage')
        self.assertEqual(out['arrears'], 0)                          # ov_balance ignored when not overdue
        self.assertEqual(out['instalment'], 108087)

    def test_placeholder_dates_are_blank(self):
        loan = {'l2': 'MOBILE LOANS', 'product': 'MOBILE LOAN', 'acc': 'Y', 'bal': 6000, 'inst': 0,
                'next_dt': '0000-12-30', 'od': 3, 'ov': 6000, 'matures': '2026-10-17', 'rem': 1, 'tot': 1,
                'rate': None, 'st': 'Overdue'}
        out = TrinoWarehouse(_Conn([loan], [])).get_loan_details('895')['loans'][0]
        self.assertIsNone(out['next_due'])
        self.assertIsNone(out['instalment'])
        self.assertIsNone(out['paid_by_standing_order'])
        self.assertEqual(out['arrears'], 6000)
