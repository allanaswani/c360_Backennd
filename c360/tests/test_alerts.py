"""RM alerts (c360/alerts.py) and the morning email built from them."""
from django.test import SimpleTestCase

from c360 import alerts
from c360.reports import rm_alerts

WHO = {'name': 'A CUSTOMER', 'segment': 'PRIVATE', 'branch': 'REHANI BRANCH'}


def _build(**kw):
    args = {'arrears': [], 'drops': [], 'maturities': [], 'loan_as_of': '2026-10-30', 'deposit_as_of': '2026-10-30'}
    args.update(kw)
    return alerts.build(**args)


class AlertRuleTests(SimpleTestCase):
    def test_arrears_due_date_and_text(self):
        out = _build(arrears=[{**WHO, 'cust_id': 1, 'account_no': 'A1', 'product': 'SECURED BUSINESS LOAN',
                               'od': 3, 'overdue': 45000.4, 'balance': 900000}])
        self.assertEqual(len(out), 1)
        a = out[0]
        self.assertEqual((a['kind'], a['date'], a['amount'], a['cust_id']), ('arrears', '2026-10-27', 45000, '1'))
        self.assertEqual(a['title'], 'Secured Business Loan: 3 days in arrears')
        self.assertIn('KES 45,000 fell due on 2026-10-27', a['detail'])

    def test_mobile_loans_and_older_arrears_are_left_out(self):
        out = _build(arrears=[
            {**WHO, 'cust_id': 1, 'product': 'MWANABIASHARA 3-MONTHS MOBILE LOAN', 'od': 2, 'overdue': 1, 'balance': 1},
            {**WHO, 'cust_id': 2, 'product': 'SECURED BUSINESS LOAN', 'od': 8, 'overdue': 1, 'balance': 1},
            {**WHO, 'cust_id': 3, 'product': 'SECURED BUSINESS LOAN', 'od': 0, 'overdue': 1, 'balance': 1}])
        self.assertEqual(out, [])

    def test_deposit_drop_thresholds(self):
        out = _build(drops=[
            {**WHO, 'cust_id': 1, 'before': 2_000_000, 'after': 400_000, 'before_on': '2026-09-30'},
            {**WHO, 'cust_id': 2, 'before': 2_000_000, 'after': 1_100_000, 'before_on': '2026-09-30'},   # under half
            {**WHO, 'cust_id': 3, 'before': 900_000, 'after': 0, 'before_on': '2026-09-30'}])          # too small
        self.assertEqual([a['cust_id'] for a in out], ['1'])
        self.assertEqual(out[0]['title'], 'Deposits down 80% in 30 days')
        self.assertEqual(out[0]['amount'], 1_600_000)

    def test_maturity_window_and_order(self):
        out = _build(
            maturities=[{**WHO, 'cust_id': 5, 'product': 'Fixed Deposit', 'account_no': 'F1', 'balance': 5e6,
                         'matures': '2026-11-03', 'days_left': 4},
                        {**WHO, 'cust_id': 6, 'product': 'Fixed Deposit', 'balance': 9e6,
                         'matures': '2026-11-20', 'days_left': 21}],
            arrears=[{**WHO, 'cust_id': 1, 'product': 'PLOT PURCHASE', 'od': 1, 'overdue': 10, 'balance': 10}])
        self.assertEqual([a['kind'] for a in out], ['arrears', 'maturity'])
        self.assertEqual(out[1]['title'], 'Fixed Deposit matures in 4 days')

    def test_book_filter(self):
        items = [{'kind': 'arrears', 'amount': 1, 'rm_code': 'X1'},
                 {'kind': 'maturity', 'amount': 2, 'rm_code': 'X2'},
                 {'kind': 'maturity', 'amount': 3, 'rm_code': None}]
        self.assertEqual(len(alerts.for_codes(items, None)), 3)
        self.assertEqual([a['rm_code'] for a in alerts.for_codes(items, ['X2'])], ['X2'])
        s = {x['kind']: x['count'] for x in alerts.summary(alerts.for_codes(items, None))}
        self.assertEqual(s, {'arrears': 1, 'deposit_drop': 0, 'maturity': 2})


class AlertEmailTests(SimpleTestCase):
    def test_email_lists_each_alert(self):
        items = _build(arrears=[{**WHO, 'cust_id': 1, 'account_no': 'A1', 'product': 'PLOT PURCHASE',
                                 'od': 2, 'overdue': 12000, 'balance': 300000}])
        msg = rm_alerts.build('X1', items, as_of='2026-10-30')
        self.assertEqual(msg['subject'], 'Customer 360: 1 alert in your book')
        self.assertIn('Plot Purchase: 2 days in arrears', msg['html'])
        self.assertIn('/book', msg['html'])
        self.assertIn('A CUSTOMER (1)', msg['text'])


class RecipientListTests(SimpleTestCase):
    def _write(self, text):
        import tempfile
        fh = tempfile.NamedTemporaryFile('w', suffix='.csv', delete=False, encoding='utf-8')
        fh.write(text)
        fh.close()
        self.addCleanup(__import__('os').unlink, fh.name)
        return fh.name

    def test_reads_codes_and_skips_bad_rows(self):
        path = self._write('sales_code,rm_name,email\nHM3729,A RM,a.rm@hfcb.co.ke\n,NO CODE,x@hfcb.co.ke\n'
                           'BN3883,NO EMAIL,\n 4279 ,B RM, b.rm@hfcb.co.ke \n')
        self.assertEqual(dict(rm_alerts.listed_recipients(path)),
                         {'HM3729': ['a.rm@hfcb.co.ke'], '4279': ['b.rm@hfcb.co.ke']})

    def test_missing_file_is_empty(self):
        self.assertEqual(dict(rm_alerts.listed_recipients('no/such/file.csv')), {})

    def test_bundled_list_loads(self):
        listed = rm_alerts.listed_recipients()
        self.assertGreaterEqual(len(listed), 100)
        self.assertTrue(all('@' in e for es in listed.values() for e in es))


class RecipientMergeTests(__import__('django.test', fromlist=['TestCase']).TestCase):
    def test_list_and_users_merge_without_duplicates(self):
        from django.contrib.auth.models import User
        for name, email, code, active in [('rm.a', 'A.RM@hfcb.co.ke', 'HM3729', True),
                                          ('rm.c', 'c.rm@hfcb.co.ke', 'CM3010', True),
                                          ('rm.gone', 'gone@hfcb.co.ke', 'CM3010', False)]:
            u = User.objects.create_user(username=name, email=email, is_active=active)
            u.profile.sales_code = code
            u.profile.save()
        path = RecipientListTests._write(self, 'sales_code,rm_name,email\nHM3729,A RM,a.rm@hfcb.co.ke\n')
        out = rm_alerts.recipients_by_code(path)
        self.assertEqual(out['HM3729'], ['a.rm@hfcb.co.ke'])
        self.assertEqual(out['CM3010'], ['c.rm@hfcb.co.ke'])
