"""ML pipeline unit tests — feature/label derivation, leak-free columns, train+score,
and the engine's rules fallback when no model is present.

These don't touch the warehouse: features are pure functions and training runs on
synthetic rows, so the suite stays fast and offline.
"""
import os
import tempfile

import numpy as np
from django.test import TestCase

from c360.ml import features as F


class FeatureTests(TestCase):
    def test_flags_from_products(self):
        flags = F.flags_from_products(['CURRENT ACCOUNT', 'FANAKA SAVINGS'], ['OWNER OCCUPIER MORTGAGE'])
        self.assertEqual(flags['current'], 1)
        self.assertEqual(flags['savings'], 1)
        self.assertEqual(flags['mortgage'], 1)
        self.assertEqual(flags['overdraft'], 0)

    def test_target_flag_excluded_from_its_own_features(self):
        # The label must never appear among its own features (that would leak).
        for target in F.TARGET_PRODUCTS:
            cols = F.feature_columns(target)
            self.assertNotIn(target, cols)
            # …but other product flags do remain as signal.
            others = [f for f in F.ALL_FLAGS if f != target]
            for o in others:
                self.assertIn(o, cols)

    def test_make_row_has_expected_shape(self):
        flags = {k: 0 for k in F.ALL_FLAGS}
        flags['current'] = 1
        row = F.make_row(dep_bal=100_000, loan_bal=0, dep_n=2, loan_n=0,
                         flags=flags, segment='SME', tenure_years=3.0)
        self.assertIn('log_dep_bal', row)
        self.assertEqual(row['segment'], 'SME')
        self.assertEqual(row['current'], 1)
        # log compression applied
        self.assertAlmostEqual(row['log_dep_bal'], float(np.log1p(100_000)), places=2)


class TrainScoreTests(TestCase):
    def setUp(self):
        # Train into a throwaway dir so the production models on disk are never touched.
        self._tmp = tempfile.mkdtemp(prefix='c360ml_')
        self._prev = os.environ.get('C360_ML_MODELS_DIR')
        os.environ['C360_ML_MODELS_DIR'] = self._tmp
        from c360.ml import model as M
        M.reset_model_cache()

    def tearDown(self):
        import shutil
        from c360.ml import model as M
        if self._prev is None:
            os.environ.pop('C360_ML_MODELS_DIR', None)
        else:
            os.environ['C360_ML_MODELS_DIR'] = self._prev
        M.reset_model_cache()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_train_and_score_roundtrip(self):
        # Synthetic book where savings clearly tracks deposits + a current account.
        rng = np.random.default_rng(0)
        rows = []
        for _ in range(3000):
            dep = float(rng.lognormal(11, 2))
            cur = int(rng.random() < 0.5)
            savings = int(rng.random() < min(0.95, 0.1 + 0.45 * (dep > 1e5) + 0.25 * cur))
            flags = {k: 0 for k in F.ALL_FLAGS}
            flags['current'] = cur
            flags['savings'] = savings
            rows.append(F.make_row(dep_bal=dep, loan_bal=0, dep_n=1 + cur + savings, loan_n=0,
                                   flags=flags, segment=str(rng.choice(['Retail', 'SME'])), tenure_years=5.0))

        from c360.ml import model as M
        from c360.ml import train as T
        manifest = T.train_all(rows, seed=1)
        self.assertGreaterEqual(manifest['n_products_trained'], 1)
        # savings should be learnable (AUC well above chance).
        self.assertGreater(manifest['products']['savings']['auc'], 0.7)

        M.reset_model_cache()
        mdl = M.load_model()
        self.assertIsNotNone(mdl)
        # A high-deposit customer with a current account but no savings → savings ranks,
        # with a real probability and a reason that cites only products they hold.
        flags = {k: 0 for k in F.ALL_FLAGS}
        flags['current'] = 1
        row = F.make_row(dep_bal=5e5, loan_bal=0, dep_n=2, loan_n=0, flags=flags,
                         segment='SME', tenure_years=8.0)
        row['_held'] = {'current'}
        recs = mdl.recommend(row, limit=3)
        self.assertTrue(recs)
        self.assertNotIn('savings', row['_held'])
        savings_rec = next((r for r in recs if r['product'] == 'savings'), None)
        self.assertIsNotNone(savings_rec)
        self.assertGreater(savings_rec['score'], 0.5)          # sane probability, not ~0
        self.assertNotIn('mortgage', savings_rec['reason'])    # never cite an un-held product


class EngineFallbackTests(TestCase):
    def test_engine_uses_rules_when_no_model(self):
        # With no trained model on disk, the engine must fall back to the rule engine
        # (mock gateway) and still return a valid result — never error.
        from django.conf import settings
        from c360.recommendations.engine import recommend_for_customer
        from c360.warehouse.factory import get_gateway
        settings.C360['DATA_MODE'] = 'mock'
        get_gateway.cache_clear()
        from c360.ml import model as M
        M.reset_model_cache()
        res = recommend_for_customer(get_gateway(), 'HF-100238', limit=3)
        self.assertIn(res.status, ('ok', 'eligibility_hold', 'eligibility_pending'))
        self.assertEqual(res.engine_version, 'rules-v1')   # not the ML engine
