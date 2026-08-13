"""Train the next-best-product propensity models.

One binary LightGBM classifier per target product: "does a customer like this hold
product P?" trained on the whole-book ownership pattern (look-alike labels). At score
time we ask each model P(holds P) for the products a customer does NOT yet hold, and
recommend the highest — so every customer gets a full ranked list, with SHAP reasons.

Artifacts land in ``c360/ml/models/``: one ``<product>.txt`` booster each plus a
``manifest.json`` (feature order, categoricals, per-product metrics, as-of, row count)
that the scorer reads. Training is CPU-only and offline (a management command / nightly
job), never in the request path.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from . import features as F

logger = logging.getLogger(__name__)

_DEFAULT_MODELS_DIR = Path(__file__).resolve().parent / 'models'


def models_dir() -> Path:
    """Where boosters + manifest live. Overridable via C360_ML_MODELS_DIR so tests
    train into a temp dir and never touch the production models."""
    return Path(os.environ.get('C360_ML_MODELS_DIR') or _DEFAULT_MODELS_DIR)


def manifest_path() -> Path:
    return models_dir() / 'manifest.json'

# A product needs enough positive AND negative examples to learn anything. The
# positive-count floor is the real guard; the rate floor only screens out products so
# rare the sample can't represent them (a few hundred positives trains a tree fine).
_MIN_POSITIVES = 120
_MIN_RATE = 0.0012   # skip near-universal or vanishingly-rare products


def _prep_frame(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    df['segment'] = df['segment'].astype('category')
    return df


def _precision_at_k(y_true: np.ndarray, y_score: np.ndarray, k_frac: float = 0.1) -> float:
    """Precision among the top-k% highest-scored — the metric that matches how the
    worklist is used (RMs work the top of a ranked list)."""
    n = len(y_true)
    k = max(1, int(n * k_frac))
    top = np.argsort(y_score)[::-1][:k]
    return float(y_true[top].mean())


# Real recorded outcomes are worth more than ownership look-alike proxies, so each
# feedback example counts as several proxy examples during fitting.
_FEEDBACK_WEIGHT = 6.0


def train_all(rows: list[dict], *, seed: int = 42,
              feedback_labels: dict[str, list[tuple[dict, int]]] | None = None) -> dict:
    """Train one model per target product from feature rows. Returns the manifest.

    ``feedback_labels`` (from the outcome-logging loop) maps product → [(row, label)].
    When present for a product, those real conversion examples are appended to that
    product's training set and upweighted, so the model learns from what actually
    happened, not just ownership look-alikes."""
    if not rows:
        raise ValueError('No training rows — is the warehouse reachable and in live mode?')
    feedback_labels = feedback_labels or {}
    mdir = models_dir()
    mdir.mkdir(parents=True, exist_ok=True)
    # Clear any stale boosters so a retrain never leaves an orphaned product model.
    for old in mdir.glob('*.txt'):
        old.unlink()

    df = _prep_frame(rows)
    n = len(df)
    rng = np.random.default_rng(seed)
    test_mask = rng.random(n) < 0.2   # 80/20 holdout

    products_meta: dict[str, dict] = {}
    for target in F.TARGET_PRODUCTS:
        y = df[target].to_numpy()
        pos, rate = int(y.sum()), float(y.mean())
        if pos < _MIN_POSITIVES or rate < _MIN_RATE or rate > 1 - _MIN_RATE:
            logger.info('skip %s (positives=%d rate=%.4f)', target, pos, rate)
            products_meta[target] = {'trained': False, 'positives': pos, 'rate': round(rate, 5)}
            continue

        cols = F.feature_columns(target)
        cat = [c for c in F.CATEGORICAL_FEATURES if c in cols]
        X = df[cols]
        Xtr, ytr = X[~test_mask], y[~test_mask]
        Xte, yte = X[test_mask], y[test_mask]
        w_tr = np.ones(len(ytr), dtype=float)

        # Blend in real recorded outcomes (the feedback loop), upweighted.
        fb = feedback_labels.get(target) or []
        n_feedback = 0
        if fb:
            fb_df = pd.DataFrame([r for r, _ in fb]).reindex(columns=cols)
            for c in cat:
                fb_df[c] = fb_df[c].astype('category')
            fb_y = np.array([lab for _, lab in fb], dtype=int)
            Xtr = pd.concat([Xtr, fb_df], ignore_index=True)
            ytr = np.concatenate([ytr, fb_y])
            w_tr = np.concatenate([w_tr, np.full(len(fb_y), _FEEDBACK_WEIGHT)])
            n_feedback = len(fb_y)

        # class_weight balances the many non-holders against the few holders.
        scale = max(1.0, (len(ytr) - ytr.sum()) / max(1, ytr.sum()))
        booster = lgb.LGBMClassifier(
            n_estimators=300, learning_rate=0.05, num_leaves=31,
            min_child_samples=40, subsample=0.8, colsample_bytree=0.8,
            scale_pos_weight=scale, random_state=seed, n_jobs=-1, verbosity=-1)
        booster.fit(Xtr, ytr, sample_weight=w_tr, categorical_feature=cat)

        proba = booster.predict_proba(Xte)[:, 1]
        auc = float(roc_auc_score(yte, proba)) if len(np.unique(yte)) > 1 else float('nan')
        p_at_10 = _precision_at_k(yte, proba, 0.1)

        booster.booster_.save_model(str(mdir / f'{target}.txt'))
        products_meta[target] = {
            'trained': True, 'positives': pos, 'rate': round(rate, 5),
            'auc': round(auc, 4), 'precision_at_10pct': round(p_at_10, 4),
            'feedback_examples': n_feedback,
            'features': cols, 'categorical': cat,
        }
        logger.info('trained %s: AUC=%.4f P@10%%=%.4f (pos=%d, feedback=%d)',
                    target, auc, p_at_10, pos, n_feedback)

    trained = {k: v for k, v in products_meta.items() if v.get('trained')}
    manifest = {
        'version': 'lgbm-v1',
        'rows': n,
        'n_products_trained': len(trained),
        'products': products_meta,
        'product_labels': F.PRODUCT_LABELS,
        'numeric_features': F.NUMERIC_FEATURES,
        'categorical_features': F.CATEGORICAL_FEATURES,
        'mean_auc': round(float(np.nanmean([v['auc'] for v in trained.values()])), 4) if trained else None,
    }
    manifest_path().write_text(json.dumps(manifest, indent=2))
    return manifest
