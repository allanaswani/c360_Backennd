"""Turn recorded recommendation outcomes into supervised training rows.

This is the bridge from the feedback loop to the model: each labelled feedback row
(``accepted`` → 1, ``declined``/``not_relevant`` → 0) is joined to that customer's
live feature vector, producing genuine conversion-labelled examples. When enough
exist, ``train_recommender --use-feedback`` blends these into the per-product training
sets so the model learns from what actually happened, not just ownership look-alikes.
"""
from __future__ import annotations

import logging

from ..models import RecommendationFeedback
from . import features as F

logger = logging.getLogger(__name__)


def labelled_rows(gateway) -> dict[str, list[tuple[dict, int]]]:
    """Per-product ``[(feature_row, label), …]`` from recorded outcomes.

    Groups labelled feedback by product, builds each customer's current feature row
    once, and pairs it with the outcome label. Customers whose features can't be built
    (e.g. no longer in the warehouse) are skipped. Returns ``{product: [(row, y), …]}``.
    """
    labelled = RecommendationFeedback.objects.filter(
        outcome__in=list(RecommendationFeedback.POSITIVE_OUTCOMES
                         | RecommendationFeedback.NEGATIVE_OUTCOMES))
    # Only products the model actually ranks.
    labelled = [f for f in labelled if f.product in F.TARGET_PRODUCTS]
    if not labelled:
        return {}

    # Build each customer's feature row once (feedback often repeats a customer).
    row_cache: dict[str, dict | None] = {}
    out: dict[str, list[tuple[dict, int]]] = {}
    for fb in labelled:
        if fb.cust_id not in row_cache:
            try:
                row_cache[fb.cust_id] = F.customer_feature_row(gateway, fb.cust_id)
            except Exception:
                logger.exception('feature build failed for %s', fb.cust_id)
                row_cache[fb.cust_id] = None
        row = row_cache[fb.cust_id]
        if row is None:
            continue
        label = fb.label
        if label is None:
            continue
        # Drop the scoring-only helper key before it reaches training.
        clean = {k: v for k, v in row.items() if k != '_held'}
        out.setdefault(fb.product, []).append((clean, label))
    return out
