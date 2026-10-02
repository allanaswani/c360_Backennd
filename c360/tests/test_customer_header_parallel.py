"""The customer header's reads run side by side (2026-10-02: one after another they
took long enough that the customer page timed out). Running them in parallel must not
change what the page shows: a failed read still blanks only its own panel, a failed
headline value still fails the page, and the customer is not read twice."""
from unittest import mock

from django.test import SimpleTestCase

from c360.services.customer import build_customer_header, build_value_summary
from c360.warehouse.mock import seed
from c360.warehouse.mock.mock_gateway import MockWarehouse


class ParallelHeaderTests(SimpleTestCase):
    def setUp(self):
        self.gw = MockWarehouse()
        self.cid = next(iter(seed.CUSTOMER_INDEX))

    def test_same_header_as_before(self):
        header = build_customer_header(self.gw, self.cid)
        self.assertEqual(header['cust_id'], self.cid)
        self.assertIn('risk_class', header['risk'])

    def test_a_failed_read_blanks_only_its_own_panel(self):
        with mock.patch.object(self.gw, 'get_lending_health', side_effect=RuntimeError('down')):
            header = build_customer_header(self.gw, self.cid)
        self.assertIsNone(header['lending'])
        self.assertEqual(header['identity']['name']['value'], self.gw.get_customer(self.cid)['name'])

    def test_a_failed_bureau_read_is_unsourced_not_no_record(self):
        with mock.patch.object(self.gw, 'get_credit_bureau', side_effect=RuntimeError('down')):
            header = build_customer_header(self.gw, self.cid)
        self.assertIsNone(header['credit_bureau'])
        self.assertIn('unavailable', header['risk']['crb_status']['note'])

    def test_no_bureau_record_still_says_so(self):
        with mock.patch.object(self.gw, 'get_credit_bureau', return_value=None):
            header = build_customer_header(self.gw, self.cid)
        self.assertEqual(header['risk']['crb_status']['value'], 'No bureau record')

    def test_the_customer_already_read_is_reused(self):
        raw = self.gw.get_customer(self.cid)
        with mock.patch.object(self.gw, 'get_customer') as again:
            build_customer_header(self.gw, self.cid, raw)
        again.assert_not_called()

    def test_a_failed_headline_value_still_fails(self):
        with mock.patch.object(self.gw, 'get_relationship_value', side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):
                build_value_summary(self.gw, self.cid)

    def test_overview_a_failed_domain_drops_only_that_domain(self):
        from c360.services.overview import build_customer_overview
        from c360.warehouse.periods import resolve_period
        period = resolve_period('30D', as_of=self.gw.as_of_date())
        with mock.patch.object(self.gw, 'get_whizz', side_effect=RuntimeError('down')):
            out = build_customer_overview(self.gw, self.cid, period)
        whizz = next(s for s in out['domain_snapshots'] if s['tab'] == 'whizz')
        self.assertEqual(whizz['status'], 'to_source')
        self.assertTrue(out['relationship_trend']['series'] is not None)

    def test_tabs_reuse_the_customer_and_still_fail_on_a_required_read(self):
        from c360.services.hfcb import build_hfcb_domain
        from c360.services.overview import build_customer_overview
        from c360.warehouse.periods import resolve_period
        period = resolve_period('30D', as_of=self.gw.as_of_date())
        raw = self.gw.get_customer(self.cid)
        with mock.patch.object(self.gw, 'get_customer') as again:
            self.assertIsNotNone(build_hfcb_domain(self.gw, self.cid, period, raw))
            self.assertIsNotNone(build_customer_overview(self.gw, self.cid, period, raw))
        again.assert_not_called()
        with mock.patch.object(self.gw, 'channel_usage', side_effect=RuntimeError('down')):
            with self.assertRaises(RuntimeError):
                build_hfcb_domain(self.gw, self.cid, period, raw)
        with mock.patch.object(self.gw, 'get_profitability', side_effect=RuntimeError('down')):
            out = build_hfcb_domain(self.gw, self.cid, period, raw)
        self.assertEqual(out['metrics']['aum']['status'], 'to_source')

    def test_a_failed_insurance_read_only_drops_insurance(self):
        with mock.patch.object(self.gw, 'get_bancassurance', side_effect=RuntimeError('down')):
            v = build_value_summary(self.gw, self.cid)
        ins = v['by_domain'][3]
        self.assertIsNone(ins['value'])
        self.assertIsNotNone(v['headline']['relationship_value'])
