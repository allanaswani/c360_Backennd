"""An insurance client's own page must agree with the list beside it.

JACCA CONSULTING GROUP LIMITED, on one screen: the list said 1 policy, KES 85,422 of
premium, 1 receipt. Their own page said "Bancassurance - NOT SOURCED - No policies".

get_bancassurance opens with an int() cast of the customer id. That cast is the
namespace guard working as designed - it is what stops a bank query resolving an
insurance id and attaching somebody else's balances - but it also blocked the one
domain that SHOULD answer for these clients.
"""
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from c360 import insurance_register as ins_reg
from c360.recommendations.engine import _insurance_acquisition_result
from c360.tests.test_sso import _pin_mock


class PersonNameTests(SimpleTestCase):
    """The register stores its risk manager as an email address."""

    def test_an_email_becomes_a_name(self):
        self.assertEqual(ins_reg._person_name('Brian.Gumba@hfgroup.co.ke'), 'Brian Gumba')
        self.assertEqual(ins_reg._person_name('mary_wanjiru@hfgroup.co.ke'), 'Mary Wanjiru')

    def test_something_already_a_name_is_left_alone(self):
        """Guessing twice is how a real name gets mangled."""
        self.assertEqual(ins_reg._person_name('Brian Gumba'), 'Brian Gumba')
        self.assertEqual(ins_reg._person_name("O'Brien Mwangi"), "O'Brien Mwangi")

    def test_nothing_stays_nothing(self):
        for empty in (None, '', '   '):
            self.assertIsNone(ins_reg._person_name(empty))


class InsuranceAcquisitionTests(SimpleTestCase):
    """The ranker reasons from a product gap against a peer segment. An insurance
    client has neither, and left alone it recommended a Current Account at "51% fit"
    because they have "a long-standing relationship" - with a bank they have never
    used."""

    def test_a_client_with_policies_gets_an_acquisition_pitch(self):
        out = _insurance_acquisition_result(
            {'policies': 1, 'active_policies': 1, 'premium': 85_422, 'receipts': 1})
        self.assertEqual(out.engine_version, 'acquisition-v1')
        self.assertTrue(out.items)
        self.assertEqual(out.items[0]['product'], 'transaction_account')
        self.assertIn('Insures with the group', out.items[0]['reason'])
        self.assertIn('holds no account with us', out.items[0]['reason'])

    def test_no_peer_comparison_is_invented(self):
        out = _insurance_acquisition_result(
            {'policies': 1, 'active_policies': 1, 'premium': 85_422, 'receipts': 1})
        for item in out.items:
            self.assertTrue(item['rule_id'].startswith('acq.'))
            self.assertNotIn('segment average', item['reason'])
            self.assertNotIn('long-standing', item['reason'])

    def test_an_active_policy_adds_the_renewal_conversation(self):
        out = _insurance_acquisition_result(
            {'policies': 3, 'active_policies': 2, 'premium': 300_000, 'receipts': 9})
        self.assertIn('mobile', [i['product'] for i in out.items])
        self.assertIn('renewal conversation', ' '.join(i['reason'] for i in out.items))

    def test_a_lapsed_book_gets_no_renewal_pitch(self):
        out = _insurance_acquisition_result(
            {'policies': 4, 'active_policies': 0, 'premium': 100_000, 'receipts': 4})
        self.assertNotIn('mobile', [i['product'] for i in out.items])
        self.assertIn('none currently active', out.items[0]['reason'])

    def test_a_receipt_only_client_still_gets_a_pitch(self):
        out = _insurance_acquisition_result(
            {'policies': 0, 'active_policies': 0, 'premium': 0, 'receipts': 29})
        self.assertTrue(out.items)
        self.assertIn('29 premium receipts', out.items[0]['reason'])

    def test_nothing_on_file_means_no_pitch(self):
        out = _insurance_acquisition_result(
            {'policies': 0, 'active_policies': 0, 'premium': 0, 'receipts': 0})
        self.assertEqual(out.items, [])
        self.assertIn('nothing to base a recommendation on', out.eligibility['note'])

    def test_no_eligibility_gate_is_claimed(self):
        """Risk and KYC are derived from banking history. There isn't any."""
        out = _insurance_acquisition_result(
            {'policies': 1, 'active_policies': 1, 'premium': 85_422, 'receipts': 1})
        self.assertFalse(out.eligibility['gate_evaluable'])


class PageAgreesWithListTests(TestCase):
    def setUp(self):
        _pin_mock()
        self.c = APIClient()

    def test_the_page_and_the_list_report_the_same_policies(self):
        listed = self.c.get('/api/insurance-clients/?q=zawadi',
                            HTTP_X_C360_ADMIN='1').json()['results']
        row = [c for c in listed if c['cust_id'] == 'INS-ZA118'][0]
        domain = self.c.get('/api/customers/INS-ZA118/domains/bancassurance/',
                            HTTP_X_C360_ADMIN='1')
        self.assertEqual(domain.status_code, 200)
        metrics = {m['label']: m['value'] for m in domain.json()['metrics']}
        self.assertEqual(metrics['Policies'], row['insurance']['policies'])

    def test_the_overview_carries_the_premium_rather_than_a_zero(self):
        body = self.c.get('/api/customers/INS-ZA118/', HTTP_X_C360_ADMIN='1').json()
        by_domain = {d['domain']: d for d in body['value_summary']['by_domain']}
        self.assertEqual(by_domain['Bancassurance']['status'], 'live')
        self.assertGreater(by_domain['Bancassurance']['value'], 0)

    def test_an_insurance_client_gets_the_acquisition_engine(self):
        body = self.c.get('/api/customers/INS-VT001/recommendations/',
                          HTTP_X_C360_ADMIN='1').json()
        self.assertEqual(body['engine_version'], 'acquisition-v1')
