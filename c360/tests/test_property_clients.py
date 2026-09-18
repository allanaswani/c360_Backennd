"""the property register property clients — the universe Customer 360 could not see.

The bank's customer master is ``dim_customer``. the property register's property buyers mostly are
not in it (3,830 of 4,843 at the last live measurement), so they had no page, no
search result and no existence in this app at all — which is how a company with six
units and KES 50M of property came to be "not on Customer 360".

These tests pin the three things that make the new universe safe to expose: bank
figures can never leak onto a client with no bank relationship, the clients who DO
also bank with us are bridged and offered their real profile, and the register is
not handed to RMs whose book contains none of it.
"""
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from c360 import brand
from c360 import property_register as prop_reg
from c360.tests.test_sso import _pin_mock
from c360.warehouse.gateway import WarehouseGateway


class IdNamespaceTests(SimpleTestCase):
    """The prefix is a safety mechanism, not a label — see c360/property_register.py."""

    def test_round_trip(self):
        self.assertEqual(prop_reg.format_id(415), 'PROP-415')
        self.assertEqual(prop_reg.parse_id('PROP-415'), 415)
        self.assertEqual(prop_reg.parse_id('prop-415'), 415)
        self.assertEqual(prop_reg.format_id(415.0), 'PROP-415')

    def test_a_bank_id_is_never_mistaken_for_an_hfdi_one(self):
        for bank_id in ('415', 'HF-102010', '', None, 'PROP-', 'PROPX-1', 'PROP-4a'):
            self.assertIsNone(prop_reg.parse_id(bank_id), bank_id)
            self.assertFalse(prop_reg.is_hfdi_id(bank_id))

    def test_bank_only_methods_reject_an_hfdi_id(self):
        """The whole namespace argument rests on this: every bank query parses the id
        with TrinoWarehouse._cid, which is an int() cast, so an the property register id cannot reach
        eom_deposits or eom_loans and come back with somebody else's money."""
        from c360.warehouse.trino.trino_gateway import TrinoWarehouse
        self.assertIsNone(TrinoWarehouse._cid('PROP-415'))

    def test_idno_normalisation_is_punctuation_blind(self):
        self.assertEqual(prop_reg.normalise_idno('C.102844'), 'C102844')
        self.assertEqual(prop_reg.normalise_idno('c/102844'), 'C102844')
        self.assertEqual(prop_reg.normalise_idno('CPR/2009/6011'), 'CPR20096011')
        self.assertEqual(prop_reg.normalise_idno(None), '')

    def test_coverage_note_reads_as_a_sentence(self):
        note = prop_reg.coverage_note(4843, 1013)
        self.assertIn('4,843', note)
        self.assertIn('1,013 (21%)', note)
        self.assertIn('3,830', note)
        # Derived, not pinned: the sentence shape is what matters, and a test that
        # hardcodes the entity name just makes the next rebrand bigger.
        self.assertIn(brand.PROPERTY, note)
        self.assertEqual(prop_reg.coverage_note(0, 0),
                         f'No property clients found in the {brand.PROPERTY} register.')

    def test_unbridged_client_does_not_claim_the_staff_rule_ran(self):
        """the property register's register has no employer, segment or employee id, so for a client
        with no bank record there is nothing to evaluate the staff rule against.
        Saying 'not staff' would be an assertion we cannot support."""
        row = {'client_id': 9.0, 'client_name': 'A', 'client_idno': '123456'}
        unbridged = prop_reg.shape_client(row)
        self.assertFalse(unbridged['staff_evaluated'])
        bridged = prop_reg.shape_client(row, bank={'cust_id': '1', 'is_staff': True})
        self.assertTrue(bridged['staff_evaluated'])
        self.assertTrue(bridged['is_staff'])

    def test_base_gateway_has_no_property_universe(self):
        self.assertEqual(WarehouseGateway.search_property_clients(object(), ''), [])
        self.assertIsNone(WarehouseGateway.get_property_client(object(), 415))
        self.assertIsNone(WarehouseGateway.property_client_coverage(object()))


class PropertyClientApiTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def _list(self, qs='', **headers):
        r = self.c.get(f'/api/property-clients/{qs}', **headers)
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_register_lists_with_the_coverage_that_explains_it(self):
        body = self._list(**{'HTTP_X_C360_ADMIN': '1'})
        self.assertEqual(body['basis'], f'{brand.PROPERTY} client register')
        cov = body['coverage']
        # The counts have to reconcile or the sentence on the page is a lie.
        self.assertEqual(cov['total'], cov['banked'] + cov['unbanked'])
        self.assertLessEqual(cov['owners'], cov['total'])
        self.assertIn(f"{cov['total']:,}", cov['note'])

    def test_a_client_who_also_banks_with_us_is_bridged(self):
        body = self._list(**{'HTTP_X_C360_ADMIN': '1'})
        by_id = {c['cust_id']: c for c in body['results']}
        self.assertEqual(by_id['PROP-415']['property_client']['bank_cust_id'], 'HF-102010')

    def test_the_bridge_ignores_punctuation(self):
        """The two systems punctuate a registration number differently ('C.088310'
        vs 'C/088310'). Live, normalising gains only two clients out of 3,429 — the
        gap really is people with no bank record — but a false 'not a customer' on a
        client we do hold is still wrong."""
        body = self._list(**{'HTTP_X_C360_ADMIN': '1'})
        by_id = {c['cust_id']: c for c in body['results']}
        self.assertEqual(by_id['PROP-1503']['property_client']['bank_cust_id'], 'HF-101120')

    def test_unbanked_filter_returns_only_the_acquisition_list(self):
        body = self._list('?unbanked=1', **{'HTTP_X_C360_ADMIN': '1'})
        self.assertTrue(body['results'])
        for c in body['results']:
            self.assertIsNone(c['property_client']['bank_cust_id'])

    def test_a_client_with_no_unit_yet_is_still_on_the_register(self):
        """Filtering them out would make the count on screen disagree with the
        register itself."""
        body = self._list(**{'HTTP_X_C360_ADMIN': '1'})
        by_id = {c['cust_id']: c for c in body['results']}
        self.assertIn('PROP-1688', by_id)
        self.assertEqual(by_id['PROP-1688']['property_client']['units'], 0)

    def test_search_matches_name_and_identity_document(self):
        self.assertTrue(self._list('?q=tumaini', **{'HTTP_X_C360_ADMIN': '1'})['results'])
        hit = self._list('?q=C-088310', **{'HTTP_X_C360_ADMIN': '1'})['results']
        self.assertEqual([c['cust_id'] for c in hit], ['PROP-1503'])

    def test_internal_scope_and_staff_keys_never_reach_the_client(self):
        for c in self._list(**{'HTTP_X_C360_ADMIN': '1'})['results']:
            for key in ('sales_code', 'is_staff', 'staff', 'staff_evaluated', 'employer'):
                self.assertNotIn(key, c)

    def test_the_register_is_not_handed_to_a_scoped_rm(self):
        """Not because it is more sensitive than a customer record, but because none
        of it is theirs: these clients are allocated to no book at all."""
        r = self.c.get('/api/property-clients/', HTTP_X_C360_ROLE='rm',
                       HTTP_X_C360_SALES_CODES='SC-1077')
        self.assertEqual(r.status_code, 403)


class PropertyClientDetailTests(TestCase):
    """An the property register id has to survive the whole customer page, not just a list row."""

    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def test_the_customer_page_loads_for_a_property_client(self):
        r = self.c.get('/api/customers/PROP-902/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        header = r.json()['header']
        self.assertEqual(header['identity']['name']['value'], 'Halima Yusuf Abdi')

    def test_no_bank_figure_is_ever_attached_to_a_non_customer(self):
        """The whole point of the separate namespace. A client with no bank record
        must show zero relationship value, not another customer's."""
        body = self.c.get('/api/customers/PROP-902/', HTTP_X_C360_ADMIN='1').json()
        head = body['value_summary']['headline']
        self.assertEqual(head['relationship_value']['value'], 0)
        self.assertEqual(head['deposits']['value'], 0)
        self.assertEqual(head['loans']['value'], 0)
        by_domain = {d['domain']: d for d in body['value_summary']['by_domain']}
        # HFCB is zero and SAYS why, rather than looking like a loading failure.
        self.assertEqual(by_domain['HFCB']['value'], 0)
        self.assertIn('No bank relationship', by_domain['HFCB']['note'])
        # Their property holding is known exactly, so it is reported as live rather
        # than left on the generic 'pending the property register CRM integration' placeholder.
        self.assertEqual(by_domain['Properties']['status'], 'live')
        self.assertEqual(by_domain['Properties']['value'], 6_400_000)

    def test_properties_come_from_the_client_id_not_the_national_id(self):
        """The bank side has to bridge on the national ID because that is all it has.
        A property client owns their units directly, so this path is exact — and it
        works for the 3,830 who have no national ID match at all."""
        r = self.c.get('/api/customers/PROP-1177/domains/properties/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        metrics = {m['label']: m['value'] for m in r.json()['metrics']}
        self.assertEqual(metrics['Properties'], 2)
        self.assertEqual(metrics['Property value'], 43_000_000)
        self.assertEqual(metrics['Under mortgage'], 2)

    def test_a_scoped_rm_gets_an_accurate_refusal(self):
        """'Outside your book' would imply another RM holds this client. Nobody does."""
        r = self.c.get('/api/customers/PROP-415/', HTTP_X_C360_ROLE='rm',
                       HTTP_X_C360_SALES_CODES='SC-1077')
        self.assertEqual(r.status_code, 403)
        self.assertIn('Not allocated to a book', r.json()['error']['detail'])

    def test_an_unknown_property_client_is_a_404(self):
        r = self.c.get('/api/customers/PROP-999999/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 404)

    def test_bank_customers_are_unaffected(self):
        r = self.c.get('/api/customers/HF-102010/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('property_client', r.json()['header'])


class PropertyClientRecommendationTests(TestCase):
    """A non-customer is an acquisition lead, not a cross-sell one.

    The cross-sell ranker reasons from the gap between what a customer holds and what
    their peer segment holds. Run on somebody who holds nothing and belongs to no peer
    group it invented both — the panel read "holds 0, segment average 3, gap 3.0" and
    advised "start with mobile banking to deepen the relationship" for a person with
    no relationship to deepen. The peer average was the preview gateway's 3.0 default.
    """

    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def _recs(self, cust_id):
        r = self.c.get(f'/api/customers/{cust_id}/recommendations/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_unbanked_client_gets_acquisition_not_cross_sell(self):
        body = self._recs('PROP-902')
        self.assertEqual(body['engine_version'], 'acquisition-v1')
        self.assertTrue(body['items'])
        for item in body['items']:
            self.assertTrue(item['rule_id'].startswith('acq.'))
            # No fabricated peer comparison anywhere in the RM-facing sentence.
            self.assertNotIn('segment average', item['reason'])
            self.assertNotIn('deepen', item['reason'].lower())

    def test_the_first_product_is_an_account_and_says_why(self):
        item = self._recs('PROP-902')['items'][0]
        self.assertEqual(item['product'], 'transaction_account')
        self.assertIn('no account with us', item['reason'])
        self.assertIn(brand.PROPERTY, item['reason'])
        self.assertIn('KES 6,400,000', item['reason'])

    def test_part_paid_holding_adds_the_financing_conversation(self):
        """62% paid means a live payment stream going to somebody else."""
        products = [i['product'] for i in self._recs('PROP-902')['items']]
        self.assertIn('mortgage', products)

    def test_a_fully_unpaid_holding_does_not_claim_a_balance_is_financed(self):
        # Zawadi is 0% paid — but they also bank with us, so take Tumaini (35%)
        # and the zero-unit client, which must produce nothing at all.
        body = self._recs('PROP-1688')
        self.assertEqual(body['items'], [])
        self.assertIn('no unit yet', body['eligibility']['note'])

    def test_no_eligibility_gate_is_claimed_for_a_non_customer(self):
        """Risk and KYC are derived from banking history. There isn't any."""
        body = self._recs('PROP-902')
        self.assertFalse(body['eligibility']['gate_evaluable'])
        self.assertIn('Acquisition lead', body['eligibility']['note'])

    def test_a_property_client_who_banks_with_us_uses_the_normal_engine(self):
        body = self._recs('PROP-415')
        self.assertNotEqual(body['engine_version'], 'acquisition-v1')

    def test_no_peer_average_is_invented_for_an_unknown_segment(self):
        """The mock returned 3.0 for any segment it did not know, which is how a
        measured-looking 'segment average 3' reached the screen."""
        from c360.warehouse.factory import get_gateway
        gw = get_gateway()
        self.assertIsNone(gw.segment_product_benchmark('property client'))
        self.assertIsNone(gw.segment_product_benchmark('Nonexistent segment'))
        self.assertEqual(gw.segment_product_benchmark('Retail'), 2.7)


class PropertyClientHeaderTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def _header(self, cust_id):
        r = self.c.get(f'/api/customers/{cust_id}/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        return r.json()['header']

    def test_summary_describes_the_property_not_a_bank_relationship(self):
        """The generic template produced 'property client customer.' — a
        sentence true of nobody."""
        summary = self._header('PROP-902')['summary']
        self.assertIn('no account here', summary)
        self.assertIn('Komarock Heights', summary)
        self.assertIn('KES 6.4M', summary)
        self.assertNotIn('property client customer', summary)

    def test_a_bridged_client_is_described_as_both(self):
        summary = self._header('PROP-415')['summary']
        self.assertIn('bank customer in their own right', summary)

    def test_a_client_with_no_unit_says_so(self):
        self.assertIn('No unit on the register yet', self._header('PROP-1688')['summary'])

    def test_branch_is_null_rather_than_the_string_null(self):
        """The header printed the literal text 'null branch' because the sub-line
        rendered the branch unconditionally. The API must send a real null."""
        self.assertIsNone(self._header('PROP-902')['identity']['branch']['value'])
