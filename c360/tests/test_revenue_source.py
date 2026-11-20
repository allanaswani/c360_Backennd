"""Customer revenue comes from the reporting Postgres ``revenue`` table, not gold
rpt_ceo_all_revenue_trend (they disagreed by 11.6M-33.8M a month on 2026-11-20).
Gold is only the fallback when Postgres fails."""
from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from c360.warehouse.trino.trino_gateway import TrinoWarehouse


class _FakePG:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.sql = rows or [], fail, []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.fail:
            raise RuntimeError('pg down')
        if 'max(trx_date)' in sql:
            return [{'d': '2026-11-19'}]
        return self.rows


class _FakeTrino:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if 'rpt_ceo_all_revenue_trend WHERE cust_cif' in sql:
            return [{'m': 10, 'y': 2026, 'c': 'NFI', 'v': 999}]
        return [{'d': '2026-11-16'}]


def _gw(pg, t=None):
    gw = TrinoWarehouse(t or _FakeTrino(), postgres=pg)
    return gw


class RevenueSourceTests(SimpleTestCase):
    def setUp(self):
        p = mock.patch.object(TrinoWarehouse, 'as_of_date', return_value=date(2026, 11, 19))
        p.start()
        self.addCleanup(p.stop)

    def test_reads_postgres_and_turns_expense_positive(self):
        pg = _FakePG([{'m': 10, 'c': 'interest_income', 'v': 1000},
                      {'m': 10, 'c': 'nfi', 'v': 200},
                      {'m': 10, 'c': 'interest_expenses', 'v': -300}])
        t = _FakeTrino()
        out = _gw(pg, t).get_revenue('223739')
        oct_ = out['months'][0]
        self.assertEqual((oct_['interest_income'], oct_['nfi'], oct_['interest_expense'], oct_['net']),
                         (1000, 200, 300, 900))
        self.assertFalse(any('rpt_ceo_all_revenue_trend' in s for s in t.sql))
        sql, params = pg.sql[0]
        self.assertEqual(params, ('223739', date(2026, 1, 1), date(2027, 1, 1)))

    def test_no_rows_in_postgres_is_no_revenue(self):
        self.assertIsNone(_gw(_FakePG([])).get_revenue('223739'))

    def test_postgres_failure_falls_back_to_gold(self):
        out = _gw(_FakePG(fail=True)).get_revenue('223739')
        self.assertEqual(out['totals']['nfi'], 999)
