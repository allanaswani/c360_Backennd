"""Repayment track record (c360/repayment.py), on loan-book rows shaped like the
live ones that motivated it: a three-instalment mobile loan paid late twice, and a
bullet mobile loan that leaves the book with a balance on its last day."""
from django.test import SimpleTestCase

from c360 import repayment


def _row(key, d, bal, ov=0, od=0, amount=100_000, acc='0600000001-01', product='MWANABIASHARA 3-MONTHS MOBILE LOAN'):
    return {'key': key, 'account_no': acc, 'product': product, 'd': d,
            'bal': bal, 'ov': ov, 'od': od, 'amount': amount}


THREE_MONTH = [
    _row('1', '2026-07-29', 100000),
    _row('1', '2026-08-29', 88664.78),
    _row('1', '2026-08-31', 82470.78, ov=16002.79),          # due, not yet counted overdue
    _row('1', '2026-09-01', 82470.78, ov=16002.79, od=3),
    _row('1', '2026-09-12', 71970.78, ov=5502.79, od=14),
    _row('1', '2026-09-14', 66467.99),
    _row('1', '2026-10-02', 45709.47, ov=12239.25, od=3),
    _row('1', '2026-10-17', 38215.47, ov=4745.25, od=18),
    _row('1', '2026-10-19', 33470.22),
    _row('1', '2026-10-29', 0),
]
BULLET = [
    _row('2', '2026-08-27', 30000, amount=30000, acc='0600000002-02', product='MOBILE LOAN'),
    _row('2', '2026-09-14', 0, amount=30000, acc='0600000002-02', product='MOBILE LOAN'),   # load glitch
    _row('2', '2026-09-15', 30000, amount=30000, acc='0600000002-02', product='MOBILE LOAN'),
    _row('2', '2026-09-24', 4500, amount=30000, acc='0600000002-02', product='MOBILE LOAN'),
]
REGISTRY = {
    '1': {'opened': '2026-07-29', 'matures': '2026-10-29', 'status': 'Closed', 'last_trx': '2026-10-29'},
    '2': {'opened': '2026-08-27', 'matures': '2026-09-25', 'status': 'Closed', 'last_trx': '2026-09-25'},
}


class RepaymentTests(SimpleTestCase):
    def setUp(self):
        self.out = repayment.build(THREE_MONTH + BULLET, REGISTRY, start='2025-11-01', end='2026-10-30')
        self.loans = {x['account_no']: x for x in self.out['loans']}

    def test_late_instalments_with_due_date_and_days(self):
        late = self.loans['0600000001-01']['late']
        self.assertEqual([(e['due'], e['days_late'], e['cleared']) for e in late],
                         [('2026-08-29', 14, '2026-09-14'), ('2026-09-29', 18, '2026-10-19')])
        self.assertEqual(late[0]['peak_arrears'], 16003)

    def test_overdue_amount_before_any_days_overdue_is_not_late(self):
        # 31 Aug carries an overdue balance at 0 days: the grace day, not an arrears run.
        self.assertEqual(len(self.loans['0600000001-01']['late']), 2)

    def test_one_day_glitch_is_not_a_payment(self):
        pays = self.loans['0600000002-02']['payments']
        self.assertNotIn('2026-09-14', [p['date'] for p in pays])
        self.assertEqual(self.loans['0600000002-02']['paid_down'], 30000)

    def test_closed_loan_balance_is_cleared_on_closing_date(self):
        b = self.loans['0600000002-02']
        self.assertEqual(b['status'], 'closed')
        self.assertEqual(b['payments'][-1], {'date': '2026-09-25', 'amount': 4500, 'final': True})
        self.assertEqual(b['balance_now'], 0)

    def test_summary(self):
        s = self.out['summary']
        self.assertEqual((s['loans'], s['loans_paid_late'], s['late_instalments'], s['worst_days_late']), (2, 1, 2, 18))
        self.assertEqual(s['in_arrears_now'], 0)
        self.assertEqual((s['max_open_together'], s['max_open_on']), (2, '2026-08-27'))

    def test_open_loan_in_arrears_today(self):
        rows = [_row('3', '2026-10-20', 5000, amount=5000), _row('3', '2026-10-30', 5200, ov=5200, od=4, amount=5000)]
        out = repayment.build(rows, {'3': {'status': 'Active'}}, start='2025-11-01', end='2026-10-30')
        loan = out['loans'][0]
        self.assertEqual((loan['status'], loan['days_overdue_now'], loan['balance_now']), ('open', 4, 5200))
        self.assertIsNone(loan['late'][0]['cleared'])
        self.assertEqual(out['summary']['in_arrears_now'], 1)

    def test_no_rows_is_none(self):
        self.assertIsNone(repayment.build([], {}, start='2025-11-01', end='2026-10-30'))
