"""Freshness and the transaction feed (2026-10-01).

Found on customer 1218821: her salary (a JOURNAL CREDIT) and standing order were
posted on the ledger's BATCH channel and the feed dropped them, and transactions were
held at the deposit snapshot date although the ledger had two more days. These tests
pin the fixes against a fake connector that records the SQL it is sent.
"""
from datetime import date, timedelta

from django.test import SimpleTestCase

from c360 import activity as A
from c360.warehouse.trino.trino_gateway import TrinoWarehouse

BIZ = date(2026, 9, 30)
DEP = date(2026, 9, 29)
LOAN = date(2026, 9, 30)
LEDGER = date(2026, 10, 1)


class _Conn:
    def __init__(self, feed_rows=None, recent_rows=None):
        self.sql: list[str] = []
        self.feed_rows = feed_rows or []
        self.recent_rows = recent_rows

    def execute(self, sql, params=None):
        self.sql.append(sql)
        s = sql.lower()
        if 'bank_parameters' in s:
            return [{'d': BIZ}]
        if 'max(eom_date)' in s and 'eom_deposits' in s:
            return [{'d': DEP}]
        if 'max(eom_date)' in s and 'eom_loans' in s:
            return [{'d': LOAN}]
        if 'max(transaction_date)' in s:
            return [{'d': LEDGER}]
        if 'o_final_acc_amount amt' in s:
            # The staged feed: answer the first (recent) read with recent_rows when
            # given, every other read with feed_rows.
            if self.recent_rows is not None and len([x for x in self.sql if 'o_final_acc_amount amt' in x.lower()]) == 1:
                return list(self.recent_rows)
            return list(self.feed_rows)
        return []


def _row(d, j, ch, amt, prod='MALIPO SALARY ACCOUNT'):
    return {'d': d, 'j': j, 'ch': ch, 'amt': amt, 'prod': prod}


class SourceDateTests(SimpleTestCase):
    def test_each_source_reports_its_own_date(self):
        gw = TrinoWarehouse(_Conn())
        self.assertEqual(gw.as_of_date(), DEP)          # whole-book stays consistent
        self.assertEqual(gw.loan_as_of(), LOAN)          # loans are not held back a day
        self.assertEqual(gw.ledger_as_of(), LEDGER)      # transactions run to the newest posting
        self.assertEqual(gw.source_freshness(), {
            'business_date': '2026-09-30', 'deposits': '2026-09-29',
            'loans': '2026-09-30', 'transactions': '2026-10-01'})

    def test_ledger_never_earlier_than_balances(self):
        class Lagging(_Conn):
            def execute(self, sql, params=None):
                if 'max(transaction_date)' in sql.lower():
                    return [{'d': DEP - timedelta(days=3)}]
                return super().execute(sql, params)
        self.assertEqual(TrinoWarehouse(Lagging()).ledger_as_of(), DEP)

    def test_a_to_date_period_runs_to_the_newest_posting(self):
        gw = TrinoWarehouse(_Conn())
        self.assertEqual(gw._txn_end(DEP), LEDGER)
        self.assertEqual(gw._txn_end(date(2026, 6, 30)), date(2026, 6, 30))   # explicit end kept

    def test_loan_reads_use_the_loan_date(self):
        conn = _Conn()
        gw = TrinoWarehouse(conn)
        gw.get_loan_accounts('1218821')
        loan_sql = [x for x in conn.sql if 'from delta.gold_db.eom_loans' in x.lower() and 'max(' not in x.lower()]
        self.assertTrue(loan_sql)
        self.assertIn("DATE '2026-09-30'", loan_sql[-1])


class FeedTests(SimpleTestCase):
    def test_system_postings_are_in_the_feed_signed(self):
        conn = _Conn(feed_rows=[
            _row('2026-09-30', 'STANDING ORDER PAYMENT', 'BATCH', -108087.36),
            _row('2026-09-29', 'JOURNAL CREDIT', 'BATCH', 275970.10),
            _row('2026-09-11', 'ACCOUNT TO MPESA(B2C)', 'KOCELA - SUBSCRIBER AND PAYMENT CHANNEL', -70076.25),
            _row('2026-09-10', 'CR FROM MOBILE BANKING-MPESA TO ACC', 'WSO2 - ENTERPRISE SERVICE BUS', 5000),
        ])
        gw = TrinoWarehouse(conn)
        rows = gw.recent_transactions('1218821', limit=4)
        self.assertEqual([r['channel'] for r in rows],
                         ['System', 'System', 'Whizz / M-Pesa', 'M-Pesa, till & PesaLink'])
        self.assertEqual([r['amount'] for r in rows], [-108087, 275970, -70076, 5000])
        self.assertEqual(rows[0]['account'], 'Malipo Salary Account')
        feed_sql = [x for x in conn.sql if 'o_final_acc_amount amt' in x][0]
        self.assertIn('o_final_acc_amount <> 0', feed_sql)
        self.assertNotIn("'BATCH'", feed_sql)            # system postings are not filtered out
        self.assertIn("DATE '2026-10-01'", feed_sql)      # to the newest posting

    def test_engagement_probe_still_excludes_system_postings(self):
        conn = _Conn()
        TrinoWarehouse(conn).recent_transactions('1', limit=30, lookback_months=2, engagement_only=True)
        feed_sql = [x for x in conn.sql if 'o_final_acc_amount amt' in x][0]
        self.assertIn("NOT IN ('BATCH','SAP FINANCE GL','DEFAULT')", feed_sql)

    def test_older_months_are_read_only_when_recent_ones_are_short(self):
        full = [_row('2026-09-30', 'JOURNAL CREDIT', 'BATCH', 1)] * 15
        conn = _Conn(recent_rows=full)
        TrinoWarehouse(conn).recent_transactions('1', limit=15)
        self.assertEqual(sum('o_final_acc_amount amt' in x for x in conn.sql), 1)

        conn = _Conn(recent_rows=full[:4], feed_rows=full[:11])
        rows = TrinoWarehouse(conn).recent_transactions('1', limit=15)
        self.assertEqual(sum('o_final_acc_amount amt' in x for x in conn.sql), 2)
        self.assertEqual(len(rows), 15)


class ActivityClassificationTests(SimpleTestCase):
    def test_salary_journal_credit_and_pesalink_are_classified(self):
        self.assertIn("j = 'JOURNAL CREDIT' AND p LIKE '%SALARY%' THEN 'salary_acct'", A.CATEGORY_SQL)
        self.assertIn("'ACCOUNT CREDIT KITS'", A.CATEGORY_SQL)
        self.assertIn('pesalink', A.CATEGORIES)
        # Deliberately unclassified: nothing says which kind of till it is.
        self.assertNotIn('DEPOSIT THROUGH TILL', A.CATEGORY_SQL)

    def test_pesalink_counts_as_digital_for_the_cash_only_rule(self):
        p = A.empty_profile()
        p['cash_in'] = {'count': 6, 'value': 1}
        p['pesalink'] = {'count': 1, 'value': 1}
        out = A.opportunities(p, held=set(), individual=True, segment='MASS')
        self.assertNotIn('T6', {o['rule_id'] for o in out})


class SalaryAccountCreditTests(SimpleTestCase):
    def _rules(self, n, v=900_000):
        p = A.empty_profile()
        p['salary_acct'] = {'count': n, 'value': v}
        return {o['rule_id']: o for o in A.opportunities(p, held=set(), individual=True, segment='STANDARD')}

    def test_monthly_credits_into_a_salary_account_count(self):
        # Customer 1218821: 3 journal credits into her MALIPO SALARY ACCOUNT in 90 days.
        out = self._rules(3, 827_910)
        self.assertIn('T1', out)
        self.assertIn('3 credits into a salary account', out['T1']['evidence'][0])
        self.assertNotIn('Salary credited', out['T1']['evidence'][0])

    def test_frequent_credits_are_not_a_salary(self):
        self.assertEqual(self._rules(49, 20_567_711), {})     # 49 in 90 days: money paid in
        self.assertEqual(self._rules(1), {})
