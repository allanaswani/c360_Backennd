"""The insurance-client universe — the other customers the bank's master never held.

The insurance team named four customers as missing. Three were real and none were
reachable, for three different reasons:

    Rajaa Stones Ltd       a bank customer whose policies would not resolve
    Susan Wanjiku Kariuki  a bank customer, five namesakes, no linking identifier
    VTN Ventures Limited   on the insurance register, not a bank customer
    Michael Njau Kimani    NOT on the register either - 29 premium receipts, nothing else

That last one is why this universe has two tiers. About 454 of the 6,392 names in
``hfbi_receipt_data`` have no row on ``hfbi_customer_data`` at all, and he is one of
them. He has already come back "missing" twice; excluding receipt-only clients would
have made it three times.
"""
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from c360 import insurance_register as ins_reg
from c360.tests.test_sso import _pin_mock
from c360.warehouse.gateway import WarehouseGateway


class IdNamespaceTests(SimpleTestCase):
    def test_round_trip_for_a_register_client(self):
        self.assertEqual(ins_reg.format_id('RA386'), 'INS-RA386')
        self.assertEqual(ins_reg.parse_id('INS-RA386'), 'RA386')
        self.assertEqual(ins_reg.parse_id('ins-ra386'), 'RA386')

    def test_round_trip_for_a_receipt_only_client(self):
        """No client number exists, so the normalised name is the key."""
        self.assertEqual(ins_reg.parse_id('INS-MICHAELNJAUKIMANI'), 'MICHAELNJAUKIMANI')

    def test_other_namespaces_are_never_mistaken_for_this_one(self):
        for other in ('PROP-415', 'HF-102010', '415', '', None, 'INS-', 'INSX-1'):
            self.assertIsNone(ins_reg.parse_id(other), other)

    def test_bank_queries_reject_an_insurance_id(self):
        """The namespace is the safety mechanism: no bank figure can attach to a
        client who has no bank relationship."""
        from c360.warehouse.trino.trino_gateway import TrinoWarehouse
        self.assertIsNone(TrinoWarehouse._cid('INS-RA386'))

    def test_the_prefix_carries_no_brand_name(self):
        for word in ('HFCB', 'HFDI', 'HFBI', 'HF'):
            self.assertNotIn(word, ins_reg.PREFIX)

    def test_coverage_note_reads_as_a_sentence(self):
        note = ins_reg.coverage_note(16952, 4490, 454)
        self.assertIn('16,952', note)
        self.assertIn('454 more appear only', note)
        self.assertIn('appears only', ins_reg.coverage_note(10, 2, 1))
        self.assertEqual(ins_reg.coverage_note(0, 0, 0),
                         'No clients found on the insurance register.')

    def test_a_receipt_only_client_is_marked(self):
        c = ins_reg.shape_client({'name': 'Michael Njau Kimani'},
                                 receipts={'receipts': 29}, name_key='MICHAELNJAUKIMANI')
        self.assertTrue(c['insurance']['receipts_only'])
        self.assertIsNone(c['insurance']['client_no'])
        self.assertEqual(c['insurance']['receipts'], 29)

    def test_base_gateway_has_no_insurance_universe(self):
        self.assertEqual(WarehouseGateway.search_insurance_clients(object(), ''), [])
        self.assertIsNone(WarehouseGateway.get_insurance_client(object(), 'RA386'))
        self.assertIsNone(WarehouseGateway.insurance_client_coverage(object()))


class InsuranceClientApiTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def _list(self, qs='', **h):
        r = self.c.get(f'/api/insurance-clients/{qs}', **h)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_the_register_lists(self):
        body = self._list(**{'HTTP_X_C360_ADMIN': '1'})
        self.assertEqual(body['basis'], 'Insurance client register')
        self.assertTrue(body['results'])

    def test_a_client_who_also_banks_with_us_is_bridged(self):
        by_id = {c['cust_id']: c for c in self._list(**{'HTTP_X_C360_ADMIN': '1'})['results']}
        self.assertEqual(by_id['INS-ZA118']['insurance']['bank_cust_id'], 'HF-102010')

    def test_the_reported_non_bank_customer_is_present(self):
        by_id = {c['cust_id']: c for c in self._list(**{'HTTP_X_C360_ADMIN': '1'})['results']}
        self.assertIn('INS-VT001', by_id)                    # VTN Ventures
        self.assertIsNone(by_id['INS-VT001']['insurance']['bank_cust_id'])

    def test_michael_njau_kimani_is_findable(self):
        """He has no client record at all, only 29 premium receipts, and he has come
        back 'missing' twice already."""
        hits = self._list('?q=michael', **{'HTTP_X_C360_ADMIN': '1'})['results']
        by_id = {c['cust_id']: c for c in hits}
        self.assertIn('INS-MICHAELNJAUKIMANI', by_id)
        row = by_id['INS-MICHAELNJAUKIMANI']
        self.assertTrue(row['insurance']['receipts_only'])
        self.assertEqual(row['insurance']['receipts'], 29)

    def test_receipt_only_clients_are_searchable_not_listable(self):
        """Without a client number there is no stable key to page them by, and the
        page must say so rather than appearing to show everything."""
        self.assertTrue(self._list(**{'HTTP_X_C360_ADMIN': '1'})['receipts_only_searchable'])
        self.assertFalse(
            self._list('?q=michael', **{'HTTP_X_C360_ADMIN': '1'})['receipts_only_searchable'])

    def test_unbanked_filter(self):
        for c in self._list('?unbanked=1', **{'HTTP_X_C360_ADMIN': '1'})['results']:
            self.assertIsNone(c['insurance']['bank_cust_id'])

    def test_internal_keys_never_reach_the_client(self):
        for c in self._list(**{'HTTP_X_C360_ADMIN': '1'})['results']:
            for key in ('sales_code', 'is_staff', 'staff', 'staff_evaluated'):
                self.assertNotIn(key, c)

    def test_the_register_is_not_handed_to_a_scoped_rm(self):
        r = self.c.get('/api/insurance-clients/', HTTP_X_C360_ROLE='rm',
                       HTTP_X_C360_SALES_CODES='SC-1077')
        self.assertEqual(r.status_code, 403)


class InsuranceClientPageTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def _header(self, cust_id):
        r = self.c.get(f'/api/customers/{cust_id}/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        return r.json()['header']

    def test_the_customer_page_loads_for_an_insurance_client(self):
        self.assertEqual(self._header('INS-VT001')['identity']['name']['value'],
                         'Vtn Ventures Limited')

    def test_no_bank_figure_is_attached_to_a_non_customer(self):
        body = self.c.get('/api/customers/INS-VT001/', HTTP_X_C360_ADMIN='1').json()
        self.assertEqual(body['value_summary']['headline']['relationship_value']['value'], 0)

    def test_a_receipt_only_client_says_exactly_what_is_known(self):
        summary = self._header('INS-MICHAELNJAUKIMANI')['summary']
        self.assertIn('29 premium receipts', summary)
        self.assertIn('No client record on the register', summary)

    def test_a_client_with_policies_says_so(self):
        self.assertIn('policies', self._header('INS-RA386')['summary'])

    def test_a_client_with_receipts_but_no_policy_says_that(self):
        summary = self._header('INS-VT001')['summary']
        self.assertIn('No policy on file', summary)
        self.assertIn('premium receipts', summary)

    def test_a_bridged_client_is_described_as_both(self):
        self.assertIn('bank customer in their own right', self._header('INS-ZA118')['summary'])

    def test_a_scoped_rm_gets_an_accurate_refusal(self):
        r = self.c.get('/api/customers/INS-VT001/', HTTP_X_C360_ROLE='rm',
                       HTTP_X_C360_SALES_CODES='SC-1077')
        self.assertEqual(r.status_code, 403)
        self.assertIn('Not allocated to a book', r.json()['error']['detail'])

    def test_an_unknown_insurance_client_is_a_404(self):
        self.assertEqual(
            self.c.get('/api/customers/INS-NOSUCH/', HTTP_X_C360_ADMIN='1').status_code, 404)

    def test_bank_customers_are_unaffected(self):
        self.assertNotIn('insurance_client', self._header('HF-102010'))
