"""Unit tests for the derived risk/KYC logic (c360.risk) — pure, no gateway."""
from django.test import TestCase

from c360 import risk


_FULL_IDENTITY = {
    'id_no': '22841097', 'kra_pin_status': '1', 'mobile': '+254 722 431 908',
    'email': 'a@b.co.ke', 'date_of_birth': '1980-01-01', 'address': 'P O BOX 1 NAIROBI',
}


class KycTests(TestCase):
    def test_full_identity_is_verified(self):
        out = risk.derive_kyc(_FULL_IDENTITY)
        self.assertEqual(out['status'], 'Verified')
        self.assertTrue(all(c['ok'] for c in out['checks']))

    def test_missing_national_id_cannot_be_verified(self):
        ident = {**_FULL_IDENTITY, 'id_no': 'N.A.'}
        out = risk.derive_kyc(ident)
        self.assertNotEqual(out['status'], 'Verified')

    def test_sparse_identity_is_incomplete(self):
        out = risk.derive_kyc({'id_no': None, 'mobile': None, 'email': None})
        self.assertEqual(out['status'], 'Incomplete')

    def test_placeholder_id_rejected(self):
        for bad in ('', 'N.A.', 'NULL', '0', '00000'):
            self.assertFalse(risk._valid_national_id(bad), bad)


class RiskTests(TestCase):
    def test_performing_verified_is_low(self):
        out = risk.derive_risk(['Performing'], deposits=500_000, loans=200_000, kyc_status='Verified')
        self.assertEqual(out['class'], 'Low')

    def test_non_performing_is_high(self):
        out = risk.derive_risk(['Non-Performing'], deposits=0, loans=5_000_000, kyc_status='Verified')
        self.assertEqual(out['class'], 'High')

    def test_watch_is_at_least_medium(self):
        out = risk.derive_risk(['Watch'], deposits=100_000, loans=1_000_000, kyc_status='Verified')
        self.assertIn(out['class'], ('Medium', 'High'))

    def test_high_leverage_nudges_up(self):
        out = risk.derive_risk(['Performing'], deposits=1_000, loans=50_000_000, kyc_status='Verified')
        self.assertIn(out['class'], ('Medium', 'High'))

    def test_incomplete_kyc_never_low(self):
        out = risk.derive_risk(['Performing'], deposits=500_000, loans=100_000, kyc_status='Incomplete')
        self.assertNotEqual(out['class'], 'Low')

    def test_no_exposure_is_low(self):
        out = risk.derive_risk([], deposits=300_000, loans=0, kyc_status='Verified')
        self.assertEqual(out['class'], 'Low')
