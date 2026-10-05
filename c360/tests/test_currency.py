"""Foreign-currency accounts are valued in KES (2026-10-05).

Found on customer 72336: a USD 1,045,245 call deposit was added to their KES accounts
as if it were KES 1,045,245, understating the relationship by about KES 134M. Core
banking keeps every amount in the account's own currency; the KES value is in
eom_deposits.euro_book_bal / eom_loans.lc_gross_total, or is the amount times the
row's fixing_rate.
"""
import pathlib
import re
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from c360.warehouse.trino import trino_gateway as tg
from c360.warehouse.trino.trino_gateway import TrinoWarehouse

SRC = pathlib.Path(tg.__file__).read_text(encoding='utf-8')


class _Conn:
    def __init__(self, deposits=None):
        self.sql: list[str] = []
        self.deposits = deposits or []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        s = sql.lower()
        if 'bank_parameters' in s:
            return [{'d': date(2026, 10, 2)}]
        if 'max(eom_date)' in s:
            return [{'d': date(2026, 10, 2)}]
        if 'max(fixing_rate)' in s:
            return [{'i': Decimal('1'), 'r': Decimal('129.65')}, {'i': Decimal('22'), 'r': Decimal('1')}]
        if 'select account_no, product_desc, euro_book_bal' in s:
            return self.deposits
        if 'as deposits' in s:
            return [{'deposits': Decimal('665645987.31'), 'loans': Decimal('0')}]
        return []


class CurrencyTests(SimpleTestCase):
    def test_no_balance_or_movement_is_summed_in_its_own_currency(self):
        for raw in ('SUM(book_balance', 'SUM(e.book_balance', 'SUM(gross_total', 'SUM(e.gross_total',
                    'SUM(i_amount)', 'SUM(tot_drawdown_amn)'):
            self.assertNotIn(raw, SRC, raw)
        self.assertIsNone(re.search(r'THEN -?o_final_acc_amount ELSE', SRC))

    def test_relationship_value_reads_the_kes_columns(self):
        conn = _Conn()
        v = TrinoWarehouse(conn).get_relationship_value('72336')
        sql = next(x for x in conn.sql if 'AS deposits' in x)
        self.assertIn('SUM(euro_book_bal)', sql)
        self.assertIn('SUM(lc_gross_total)', sql)
        self.assertEqual(v['deposits'], 665645987)

    def test_usd_account_shows_kes_with_its_own_amount_beside(self):
        conn = _Conn(deposits=[
            {'account_no': '9783913548-0', 'product_desc': 'CALL DEPOSIT ACCOUNT-USD',
             'euro_book_bal': Decimal('135515987'), 'book_balance': Decimal('1045244.79'),
             'currency': 'USD  ', 'entry_status': 1, 'last_trx_date': '2026-10-02'},
            {'account_no': '2000023198-0', 'product_desc': 'MORTGAGE SCHEME DEP',
             'euro_book_bal': Decimal('530031904.77'), 'book_balance': Decimal('530031904.77'),
             'currency': 'KES  ', 'entry_status': 1, 'last_trx_date': '2026-09-30'},
        ])
        usd, kes = TrinoWarehouse(conn).get_deposit_accounts('72336')
        self.assertEqual(usd['balance'], 135515987)
        self.assertEqual(usd['currency'], 'KES')
        self.assertEqual(usd['account_currency'], 'USD')
        self.assertEqual(usd['account_currency_amount'], 1045244.79)
        self.assertNotIn('account_currency', kes)

    def test_transaction_rate_uses_the_row_then_the_as_of_rate(self):
        fx = TrinoWarehouse(_Conn())._trx_fx
        self.assertIn('WHEN currency_id = 22 THEN 1', fx)
        self.assertIn('WHEN o_fixing_rate > 0 THEN o_fixing_rate', fx)
        self.assertIn('WHEN 1 THEN 129.65', fx)
        # a currency with no rate is left out (NULL), never counted as shillings
        self.assertNotIn('ELSE 1 END END', fx)

    def test_own_currency_only_for_foreign_accounts(self):
        self.assertEqual(tg._own_currency('KES', 5), {})
        self.assertEqual(tg._own_currency(None, 5), {})
        self.assertEqual(tg._own_currency('gbp ', Decimal('10.5')),
                         {'account_currency': 'GBP', 'account_currency_amount': 10.5})


class OverdrawnLeverageTests(SimpleTestCase):
    def test_overdrawn_deposits_count_as_no_cover(self):
        from c360 import risk
        # exactly -1 used to divide by zero; below zero read as "no leverage"
        for dep in (-1.0, -500_000.0):
            out = risk.derive_risk(['Normal'], dep, 5_000_000, 'Verified')
            self.assertTrue(any('high leverage' in f for f in out['factors']), out)
        self.assertIn('GREATEST(COALESCE(dp.dep,0), 0) + 1', SRC)
