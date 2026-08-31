"""Recommendation selection policy — the guardrails that stopped the model spraying
low-quality products (overdraft/asset-finance) across the whole book, and that let the
panel show nothing rather than a generic pick. Pure logic, no LightGBM required."""
from django.test import SimpleTestCase

from c360.ml import ranking


def _item(product, score, **extra):
    return {'product': product, 'product_name': product.title(), 'domain': 'HFCB',
            'score': score, 'reason': 'because', 'rule_id': 'ml.lgbm-v1', **extra}


# A manifest-shaped products map. current/savings are strong; overdraft is the
# near-useless model that used to dominate the ranking.
META = {
    'savings':  {'precision_at_10pct': 0.84, 'rate': 0.156},
    'current':  {'precision_at_10pct': 0.75, 'rate': 0.127},
    'mortgage': {'precision_at_10pct': 0.34, 'rate': 0.033},
    'overdraft': {'precision_at_10pct': 0.024, 'rate': 0.0033},
    'asset_finance': {'precision_at_10pct': 0.028, 'rate': 0.0034},
}


class RankingPolicyTests(SimpleTestCase):
    def test_low_quality_models_are_gated_out(self):
        # Even with a sky-high raw score, a model below the precision floor never headlines.
        scored = [_item('overdraft', 0.99), _item('asset_finance', 0.98), _item('current', 0.40)]
        out = ranking.select(scored, META, limit=3, min_prec=0.15, min_sc=0.10)
        products = [r['product'] for r in out]
        self.assertNotIn('overdraft', products)
        self.assertNotIn('asset_finance', products)
        self.assertEqual(products, ['current'])

    def test_confidence_floor_drops_weak_scores(self):
        scored = [_item('savings', 0.08), _item('current', 0.60)]
        out = ranking.select(scored, META, limit=3, min_prec=0.15, min_sc=0.15)
        self.assertEqual([r['product'] for r in out], ['current'])

    def test_empty_when_nothing_clears(self):
        # Thin/ambiguous customer: no product clears the floor → empty (caller uses rules).
        scored = [_item('savings', 0.05), _item('current', 0.09)]
        self.assertEqual(ranking.select(scored, META, limit=3, min_prec=0.15, min_sc=0.15), [])

    def test_ranked_by_score_and_capped(self):
        scored = [_item('savings', 0.55), _item('current', 0.80), _item('mortgage', 0.30)]
        out = ranking.select(scored, META, limit=2, min_prec=0.15, min_sc=0.10)
        self.assertEqual([r['product'] for r in out], ['current', 'savings'])

    def test_unknown_precision_is_not_gated(self):
        # A product with no precision recorded must not be silently blanked — the score
        # floor still guards it (schema-gap safety).
        out = ranking.select([_item('new_prod', 0.50)], {'new_prod': {'rate': 0.1}},
                             limit=3, min_prec=0.15, min_sc=0.15)
        self.assertEqual([r['product'] for r in out], ['new_prod'])

    def test_lift_and_precision_attached(self):
        out = ranking.select([_item('current', 0.60)], META, limit=1, min_prec=0.15, min_sc=0.10)
        self.assertAlmostEqual(out[0]['lift'], round(0.60 / 0.127, 2))
        self.assertEqual(out[0]['model_precision'], 0.75)


class CalibrationTests(SimpleTestCase):
    CURVE = {'x': [0.0, 0.5, 1.0], 'y': [0.0, 0.2, 0.9]}

    def test_interpolates_within_range(self):
        self.assertAlmostEqual(ranking.apply_calibration(0.25, self.CURVE), 0.1)
        self.assertAlmostEqual(ranking.apply_calibration(0.75, self.CURVE), 0.55)

    def test_clips_outside_range(self):
        self.assertEqual(ranking.apply_calibration(-1.0, self.CURVE), 0.0)
        self.assertEqual(ranking.apply_calibration(2.0, self.CURVE), 0.9)

    def test_no_curve_is_identity(self):
        self.assertEqual(ranking.apply_calibration(0.42, None), 0.42)
        self.assertEqual(ranking.apply_calibration(0.42, {'x': [], 'y': []}), 0.42)

    def test_calibration_changes_selection(self):
        # A raw 0.90 that calibrates to 0.18 (real probability) still clears a 0.15 floor;
        # calibrated ranking uses the honest probability, not the inflated raw score.
        meta = {'p': {'precision_at_10pct': 0.5, 'rate': 0.1,
                      'calibration': {'x': [0.0, 1.0], 'y': [0.0, 0.2]}}}
        out = ranking.select([_item('p', 0.90)], meta, limit=1, min_prec=0.15, min_sc=0.15)
        self.assertEqual(len(out), 1)
        self.assertAlmostEqual(out[0]['score'], 0.18)
