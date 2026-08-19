"""Silent-attrition classifier — the maths that flags eroding deposit relationships."""
from django.test import SimpleTestCase

from c360.retention import classify_retention


class RetentionTests(SimpleTestCase):
    def test_material_drop_is_at_risk(self):
        sig = classify_retention(1_000_000, 700_000)   # -30%
        self.assertEqual(sig['flag'], 'at_risk')
        self.assertEqual(sig['trend_pct'], -0.3)
        self.assertIn('30%', sig['note'])

    def test_mild_drop_is_watch(self):
        sig = classify_retention(1_000_000, 870_000)   # -13%
        self.assertEqual(sig['flag'], 'watch')

    def test_drawn_down_to_nil_is_at_risk_without_nonsense_percent(self):
        sig = classify_retention(1_000_000, -50_000)   # overdrawn / drained
        self.assertEqual(sig['flag'], 'at_risk')
        self.assertEqual(sig['trend_pct'], -1.0)       # capped, never ">100% down"
        self.assertNotIn('%', sig['note'])

    def test_tiny_base_is_not_judged(self):
        self.assertIsNone(classify_retention(2_000, 100))       # KES 2k wallet → noise
        self.assertIsNone(classify_retention(0, 500_000))       # nothing to erode from

    def test_big_rise_is_capped_language_not_a_huge_percent(self):
        sig = classify_retention(100_000, 1_200_000)   # +1100%
        self.assertEqual(sig['flag'], 'stable')
        self.assertIn('doubled', sig['note'])

    def test_steady_is_stable(self):
        self.assertEqual(classify_retention(1_000_000, 1_020_000)['flag'], 'stable')
