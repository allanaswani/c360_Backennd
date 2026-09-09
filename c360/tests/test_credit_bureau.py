"""Credit-bureau (TransUnion CRB) shaping + header-chip logic.

Covers the two correctness traps in the source data: the no-hit sentinel
(score 0 / PD 999.99 / grade YY must never read as a real zero) and the PD banding,
plus the service helper that turns a record into the header 'CRB status' chip and
distinguishes 'no bureau record' from a genuine load failure."""
from __future__ import annotations

from django.test import SimpleTestCase

from c360 import credit_bureau as cb
from c360.services.customer import _credit_bureau


class ShapeBureauTests(SimpleTestCase):
    def test_scored_record(self):
        out = cb.shape_bureau(
            {'score': 628, 'score_grade': 'HH', 'probability': 14.48,
             'non_performing': 0, 'arrears_90_days': 0, 'number_of_enquiries': 3,
             'enquiries_90_days': 1}, as_of='2026-03-20')
        self.assertFalse(out['no_hit'])
        self.assertEqual(out['score'], 628)
        self.assertEqual(out['grade'], 'HH')
        self.assertEqual(out['pd'], 14.48)
        self.assertEqual(out['pd_band'], 'Low')       # 5 <= 14.48 < 15 -> Low
        self.assertEqual(out['enquiries'], 3)
        self.assertEqual(out['as_of'], '2026-03-20')

    def test_no_hit_sentinel(self):
        # score 0 + PD 999.99 + grade YY = on the bureau but no scoreable history.
        out = cb.shape_bureau(
            {'score': 0, 'score_grade': 'YY', 'probability': 999.99,
             'non_performing': 0, 'number_of_enquiries': 0}, as_of='2026-03-01')
        self.assertTrue(out['no_hit'])
        self.assertIsNone(out['score'])        # never a real zero
        self.assertIsNone(out['pd'])
        self.assertIsNone(out['grade'])

    def test_grade_yy_alone_is_no_hit(self):
        out = cb.shape_bureau({'score': 0, 'score_grade': 'YY'}, as_of=None)
        self.assertTrue(out['no_hit'])

    def test_high_pd_bands_high(self):
        out = cb.shape_bureau({'score': 387, 'score_grade': 'JJ', 'probability': 82.79}, as_of=None)
        self.assertFalse(out['no_hit'])
        self.assertEqual(out['pd_band'], 'High')

    def test_pd_band_thresholds(self):
        self.assertIsNone(cb.pd_band(None))
        self.assertEqual(cb.pd_band(4.9), 'Very low')
        self.assertEqual(cb.pd_band(5), 'Low')
        self.assertEqual(cb.pd_band(29.9), 'Moderate')
        self.assertEqual(cb.pd_band(30), 'Elevated')
        self.assertEqual(cb.pd_band(60), 'High')

    def test_missing_counts_default_zero(self):
        out = cb.shape_bureau({'score': 700, 'score_grade': 'CC', 'probability': 6.0}, as_of=None)
        self.assertEqual(out['non_performing'], 0)
        self.assertEqual(out['arrears_90_days'], 0)


class _FakeGateway:
    """Minimal gateway exposing only get_credit_bureau, for the service helper."""
    def __init__(self, result=None, raise_exc=False):
        self._result = result
        self._raise = raise_exc

    def get_credit_bureau(self, cust_id):
        if self._raise:
            raise RuntimeError('bureau down')
        return self._result


class CreditBureauChipTests(SimpleTestCase):
    def test_scored_chip_shows_score_and_grade(self):
        rec = cb.shape_bureau({'score': 628, 'score_grade': 'HH', 'probability': 14.48}, as_of='2026-03-20')
        panel, chip = _credit_bureau(_FakeGateway(rec), '123')
        self.assertIsNotNone(panel)
        self.assertEqual(chip['status'], 'live')
        self.assertIn('628', chip['value'])
        self.assertIn('HH', chip['value'])
        self.assertIn('2026-03-20', chip['note'])

    def test_no_hit_chip(self):
        rec = cb.shape_bureau({'score': 0, 'score_grade': 'YY', 'probability': 999.99}, as_of='2026-03-01')
        panel, chip = _credit_bureau(_FakeGateway(rec), '123')
        self.assertIsNotNone(panel)
        self.assertIn('thin file', chip['value'])
        self.assertEqual(chip['status'], 'live')

    def test_no_record_is_honest_not_to_source(self):
        panel, chip = _credit_bureau(_FakeGateway(None), '123')
        self.assertIsNone(panel)
        self.assertEqual(chip['status'], 'live')
        self.assertEqual(chip['value'], 'No bureau record')

    def test_probe_failure_stays_to_source(self):
        panel, chip = _credit_bureau(_FakeGateway(raise_exc=True), '123')
        self.assertIsNone(panel)
        self.assertEqual(chip['status'], 'to_source')
