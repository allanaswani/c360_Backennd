"""Current-RM allocation from the curated Postgres (retail_allocated_portfolio).

dim_customer only carries the ACCOUNT-OPENING officer, which is frozen at onboarding
and wrong once a customer is reassigned. These tests pin the fix: when the curated
Postgres allocation is reachable the header shows the CURRENT RM; when it is absent,
unreachable, or the customer isn't allocated, it falls back to the onboarding officer
and labels it as such (never passing a stale name off as the current RM).
"""
from django.test import SimpleTestCase

from c360.warehouse.trino.trino_gateway import TrinoWarehouse


def _dim_row(**over):
    row = {
        'id': 39002, 'full_name': 'ACME LIMITED', 'customer_segment': 'STANDARD',
        'account_branch_name': 'KISUMU BRANCH', 'account_branch_number': 12,
        'fk_bankemployeeid': 'MIG_CIS', 'created_emp_id': 'JD100',
        'created_emp_name': 'JANE ONBOARDING', 'primary_mobile_no': '712345678',
        'mobile_tel2': None, 'telephone_1': None, 'e_mail': 'x@y.z', 'address': 'Nairobi',
        'customer_id_no': 'A123456', 'issue_authority': 'REGISTRAR', 'cust_type': 'C',
        'kra_pin_status': 'Y', 'sex': None, 'city_of_birth': None, 'employer': None,
        'date_of_birth': None, 'account_opening_date': '2020-01-02', 'cust_open_date': None,
        'cust_status': 'A',
    }
    row.update(over)
    return row


class _FakeTrino:
    def execute(self, sql, params=None):
        if 'dim_customer' in sql and 'customer_id = ?' in sql:
            return [_dim_row()]
        return []


class _FakePG:
    """Stands in for the reporting Postgres: answers the schema introspection and the
    allocation lookup. ``current`` maps customer-id string → the RM assigned today."""

    def __init__(self, *, current, raise_on_select=False):
        self.current = current
        self.raise_on_select = raise_on_select

    def execute(self, sql, params=None):
        s = sql.lower()
        if 'information_schema.tables' in s:
            return [{'table_schema': 'public', 'table_name': 'retail_allocated_portfolio'}]
        if 'information_schema.columns' in s:
            return [{'column_name': c} for c in
                    ('customer_id', 'rm_name', 'sales_code', 'allocated_date', 'branch')]
        if 'retail_allocated_portfolio' in s:            # the allocation SELECT
            if self.raise_on_select:
                raise RuntimeError('connection reset')
            ids = set(str(p) for p in (params or ()))
            return [{'cid': cid, 'rm_name': v['name'], 'sales_code': v['code']}
                    for cid, v in self.current.items() if cid in ids]
        return []


class CurrentRmTests(SimpleTestCase):
    def test_current_allocation_overrides_onboarding_officer(self):
        pg = _FakePG(current={'39002': {'name': 'PETER CURRENT', 'code': 'PC900'}})
        gw = TrinoWarehouse(_FakeTrino(), postgres=pg)
        c = gw.get_customer('39002')
        self.assertEqual(c['rm_name'], 'PETER CURRENT')      # not JANE ONBOARDING
        self.assertEqual(c['sales_code'], 'PC900')
        self.assertEqual(c['rm_source'], 'allocation')

    def test_batch_lookup_maps_ids(self):
        pg = _FakePG(current={'39002': {'name': 'PETER CURRENT', 'code': 'PC900'}})
        gw = TrinoWarehouse(_FakeTrino(), postgres=pg)
        got = gw.get_current_rm([39002, 111, None])
        self.assertEqual(got['39002']['name'], 'PETER CURRENT')
        self.assertNotIn('111', got)                          # unallocated → absent

    def test_falls_back_to_onboarding_when_no_postgres(self):
        gw = TrinoWarehouse(_FakeTrino(), postgres=None)
        c = gw.get_customer('39002')
        self.assertEqual(c['rm_name'], 'JANE ONBOARDING')
        self.assertEqual(c['rm_source'], 'onboarding')
        self.assertEqual(gw.get_current_rm([39002]), {})

    def test_falls_back_when_customer_not_allocated(self):
        pg = _FakePG(current={'55555': {'name': 'SOMEONE ELSE', 'code': 'SE1'}})
        gw = TrinoWarehouse(_FakeTrino(), postgres=pg)
        c = gw.get_customer('39002')
        self.assertEqual(c['rm_name'], 'JANE ONBOARDING')
        self.assertEqual(c['rm_source'], 'onboarding')

    def test_postgres_failure_degrades_never_raises(self):
        pg = _FakePG(current={'39002': {'name': 'PETER CURRENT', 'code': 'PC900'}},
                     raise_on_select=True)
        gw = TrinoWarehouse(_FakeTrino(), postgres=pg)
        c = gw.get_customer('39002')                          # must not raise
        self.assertEqual(c['rm_name'], 'JANE ONBOARDING')
        self.assertEqual(c['rm_source'], 'onboarding')


class _ProfitPG:
    """Answers the customer_allocation_base lookup with one populated row."""

    def execute(self, sql, params=None):
        s = sql.lower()
        if 'customer_allocation_base' in s:
            return [{'aum_cust_id': 40789262.64, 'net_after_expense': 18517.53, 'npl': 0,
                     'main_segment': 'BUSINESS', 'rm_name_prev': 'OLD RM', 'rm_code_prev': 'OR1'}]
        return []


class ProfitabilityTests(SimpleTestCase):
    def test_reads_aum_profitability_npl(self):
        gw = TrinoWarehouse(_FakeTrino(), postgres=_ProfitPG())
        p = gw.get_profitability('134909')
        self.assertEqual(round(p['aum']), 40789263)
        self.assertEqual(round(p['contribution']), 18518)
        self.assertFalse(p['npl'])                            # npl 0 → performing
        self.assertEqual(p['prev_rm'], 'OLD RM')

    def test_none_without_postgres(self):
        gw = TrinoWarehouse(_FakeTrino(), postgres=None)
        self.assertIsNone(gw.get_profitability('134909'))


class _BookPG:
    def execute(self, sql, params=None):
        s = sql.lower()
        if 'group by main_segment' in s:
            return [{'segment': 'BUSINESS', 'n': 30, 'aum': 700000},
                    {'segment': 'PB', 'n': 12, 'aum': 300000}]
        if 'order by aum_cust_id desc' in s:
            return [{'cust_id': '39002', 'customer_name': 'ACME LTD', 'segment': 'BUSINESS',
                     'aum': 500000, 'contribution': 20000, 'npl': 0}]
        if 'count(*) as customers' in s:
            return [{'customers': 42, 'aum': 1000000, 'deposits': 400000, 'loans': 600000,
                     'contribution': 50000, 'npl_customers': 3, 'npl_aum': 120000}]
        return []


class BookSummaryTests(SimpleTestCase):
    def test_book_rollup(self):
        gw = TrinoWarehouse(_FakeTrino(), postgres=_BookPG())
        b = gw.get_book_summary('SC1')
        self.assertEqual(b['customers'], 42)
        self.assertEqual(b['aum'], 1000000)
        self.assertEqual(b['npl_customers'], 3)
        self.assertEqual(b['npl_aum'], 120000)
        self.assertFalse(b['whole_book'])
        self.assertEqual(len(b['segments']), 2)
        self.assertEqual(b['top_customers'][0]['name'], 'ACME LTD')

    def test_whole_book_when_no_sales_code(self):
        gw = TrinoWarehouse(_FakeTrino(), postgres=_BookPG())
        self.assertTrue(gw.get_book_summary(None)['whole_book'])

    def test_none_without_pg(self):
        self.assertIsNone(TrinoWarehouse(_FakeTrino(), postgres=None).get_book_summary('SC1'))
