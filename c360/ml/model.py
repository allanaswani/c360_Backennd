"""Score & explain next-best-product with the trained LightGBM models.

Loaded once (process-cached), then used in the request path to rank the products a
customer does NOT hold, each with a human-readable reason built from the model's own
feature attributions (LightGBM ``pred_contrib`` — exact per-prediction SHAP values, no
extra dependency). If no models are on disk (fresh checkout / mock mode) the caller
falls back to the rule engine, so the app always works.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pandas as pd

from . import features as F
from .train import manifest_path, models_dir

logger = logging.getLogger(__name__)

# Noun-phrase drivers that read naturally after "this customer has …".
_NUM_PHRASE = {
    'log_dep_bal': 'strong deposit balances', 'log_loan_bal': 'an existing loan balance',
    'tenure_years': 'a long-standing relationship', 'segment': 'a profile typical of their segment',
}
# Phrasing for product flags — only cited when the customer actually holds the product.
_FLAG_HELD_PHRASE = {
    'current': 'a current account', 'savings': 'a savings account',
    'mortgage': 'a mortgage', 'asset_finance': 'asset finance', 'overdraft': 'an overdraft',
    'ipf': 'insurance premium finance', 'cash_cover': 'cash cover', 'trade': 'trade finance',
    'unsecured': 'an unsecured loan',
}


class PropensityModel:
    def __init__(self, manifest: dict, boosters: dict[str, lgb.Booster]):
        self.manifest = manifest
        self.boosters = boosters
        self.labels = manifest.get('product_labels', F.PRODUCT_LABELS)
        self.products_meta = manifest.get('products', {})

    @property
    def is_ready(self) -> bool:
        return bool(self.boosters)

    def _score_one(self, target: str, row: dict) -> tuple[float, str]:
        booster = self.boosters[target]
        cols = self.products_meta[target]['features']
        cat = self.products_meta[target].get('categorical', [])
        X = pd.DataFrame([{c: row.get(c) for c in cols}])
        for c in cat:
            X[c] = X[c].astype('category')
        # A booster saved from an LGBMClassifier with the binary objective returns the
        # positive-class probability directly.
        prob = float(booster.predict(X)[0])
        prob = min(1.0, max(0.0, prob))
        reason = self._explain(target, X, cols)
        return prob, reason

    def _explain(self, target: str, X: pd.DataFrame, cols: list[str]) -> str:
        """Top positive contributors for THIS prediction → one RM-facing sentence.
        Product-flag drivers are only named when the customer actually holds them, so
        the reason never cites a product they lack."""
        label = self.labels.get(target, target)
        try:
            contrib = self.boosters[target].predict(X, pred_contrib=True)[0]  # len = n_feat + 1
        except Exception:
            return f'Model flags {label} as a strong fit for this customer.'
        row = X.iloc[0]
        pairs = sorted(zip(cols, contrib[:-1]), key=lambda kv: kv[1], reverse=True)
        drivers: list[str] = []
        for col, val in pairs:
            if val <= 0:
                continue
            if col in _NUM_PHRASE:
                drivers.append(_NUM_PHRASE[col])
            elif col in _FLAG_HELD_PHRASE and int(row.get(col, 0)) == 1:
                drivers.append(_FLAG_HELD_PHRASE[col])
            if len(drivers) == 2:
                break
        if not drivers:
            return f'{label} fits this customer’s overall profile versus similar customers.'
        return f'Recommended because this customer has {" and ".join(drivers)} — customers with this profile typically hold {label.lower()}.'

    def recommend(self, row: dict, *, limit: int = 3) -> list[dict]:
        """Rank the customer's UN-held target products by propensity, with reasons."""
        held = row.get('_held', set())
        scored = []
        for target, booster in self.boosters.items():
            if target in held:
                continue   # already has it — not a recommendation
            try:
                prob, reason = self._score_one(target, row)
            except Exception:
                logger.exception('scoring failed for %s', target)
                continue
            scored.append({
                'product': target,
                'product_name': self.labels.get(target, target),
                'domain': 'HFCB',
                'score': round(prob, 4),
                'reason': reason,
                'rule_id': 'ml.lgbm-v1',
            })
        scored.sort(key=lambda r: r['score'], reverse=True)
        return scored[:limit]


@lru_cache(maxsize=1)
def load_model() -> PropensityModel | None:
    """Load the manifest + boosters once. Returns None when nothing is trained yet."""
    mpath, mdir = manifest_path(), models_dir()
    if not mpath.exists():
        return None
    try:
        manifest = json.loads(mpath.read_text())
    except Exception:
        logger.exception('could not read model manifest')
        return None
    boosters: dict[str, lgb.Booster] = {}
    for target, meta in manifest.get('products', {}).items():
        if not meta.get('trained'):
            continue
        path = mdir / f'{target}.txt'
        if path.exists():
            try:
                boosters[target] = lgb.Booster(model_file=str(path))
            except Exception:
                logger.exception('could not load booster %s', target)
    if not boosters:
        return None
    return PropensityModel(manifest, boosters)


def reset_model_cache() -> None:
    load_model.cache_clear()
