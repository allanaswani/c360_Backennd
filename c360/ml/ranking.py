"""Post-scoring selection for the propensity recommender.

Kept deliberately free of any LightGBM/pandas import so the selection policy — the
part that decides what an RM actually sees — is pure and unit-testable on its own.

The trained model emits a raw score per un-held product. Those raw scores are NOT
comparable across products: each product model is fitted with its own
``scale_pos_weight`` (≈7 for savings, ≈300 for overdraft), which inflates the rare
products' output. Ranking raw scores across products therefore lets the *worst*,
rarest models win the top slot — exactly the "recommends overdraft/asset-finance to
everyone" symptom. This module fixes that with three guards, applied in order:

1. **Model-quality gate** — a product whose model can't discriminate (low precision in
   its own top decile at train time, ``precision_at_10pct``) never headlines, however
   high its raw score. This is what stops the ≈2-3%-precision overdraft / asset-finance
   models being sprayed across the book.
2. **Calibration** — when the manifest carries an isotonic calibration curve for a
   product (added at train time), the raw score is mapped to a true probability, which
   IS comparable across products. Without a curve the raw score is used unchanged.
3. **Confidence floor** — a candidate must clear an absolute score to surface at all.
   Below it there is no real signal, so we return nothing for that product rather than
   a generic pick.

Returning fewer items — or an empty list — is a valid, deliberate answer. The engine
then falls back to the transparent rules, and if those are silent too, shows an honest
"no strong cross-sell signal" instead of inventing one. Every threshold is env-tunable
so the policy can be tightened against the live book without a code change.
"""
from __future__ import annotations

import bisect
import os


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def min_precision() -> float:
    """A model must be right at least this often within its own top decile to be
    allowed to headline. 0.15 clears current/savings/mortgage/unsecured and drops the
    near-useless overdraft/asset-finance models (≈0.024 / ≈0.028)."""
    return _float_env('C360_ML_MIN_PRECISION', 0.15)


def min_score() -> float:
    """Absolute (calibrated where available) confidence a candidate must clear to
    surface. Below this the model has no real signal for this customer."""
    return _float_env('C360_ML_MIN_SCORE', 0.15)


def apply_calibration(score: float, calib: dict | None) -> float:
    """Map a raw model score to a calibrated probability using isotonic breakpoints
    (``{'x': [...], 'y': [...]}``) stored in the manifest. Piecewise-linear, clipped to
    the fitted range — reproduces sklearn's IsotonicRegression.predict without needing
    sklearn at score time. No/!malformed curve → the score is returned unchanged."""
    if not calib:
        return score
    xs, ys = calib.get('x'), calib.get('y')
    if not xs or not ys or len(xs) != len(ys):
        return score
    if score <= xs[0]:
        return float(ys[0])
    if score >= xs[-1]:
        return float(ys[-1])
    i = bisect.bisect_right(xs, score)
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    if x1 == x0:
        return float(y0)
    return float(y0 + (y1 - y0) * (score - x0) / (x1 - x0))


def select(scored: list[dict], products_meta: dict[str, dict], *, limit: int,
           min_prec: float | None = None, min_sc: float | None = None) -> list[dict]:
    """Filter + rank scored ML candidates into the confident, quality-gated top ``limit``.

    ``scored``       : items ``{product, product_name, domain, score, reason, rule_id}``
                       where ``score`` is the model's RAW output.
    ``products_meta``: the manifest ``products`` map; per product may carry
                       ``precision_at_10pct``, ``rate`` (base rate) and ``calibration``.

    The returned items carry the (calibrated) ``score`` plus ``model_precision`` and
    ``lift`` (calibrated score ÷ base rate) for transparency. Ranked by score, high → low.
    """
    min_prec = min_precision() if min_prec is None else min_prec
    min_sc = min_score() if min_sc is None else min_sc
    out: list[dict] = []
    for item in scored:
        meta = products_meta.get(item['product'], {}) or {}
        prec = meta.get('precision_at_10pct')
        # Quality gate: a tracked model below the precision floor is dropped. A product
        # with no precision recorded is left in (score floor still guards it) so a schema
        # gap can never silently blank the whole panel.
        if prec is not None and prec < min_prec:
            continue
        cal = apply_calibration(float(item['score']), meta.get('calibration'))
        if cal < min_sc:
            continue
        rate = meta.get('rate')
        out.append({
            **item,
            'score': round(cal, 4),
            'model_precision': prec,
            'lift': round(cal / rate, 2) if rate else None,
        })
    out.sort(key=lambda r: r['score'], reverse=True)
    return out[:limit]
