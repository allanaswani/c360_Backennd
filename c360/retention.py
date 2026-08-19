"""Silent-attrition early warning — a pure, testable classifier.

Research basis: in retail banking the costliest and most-missed churn is *silent
attrition* — wallet share migrating out while the account stays open. A deposit
balance erosion of ~20%+ over a quarter flags at-risk customers 60–90 days before
they would show up as closed. We already hold near-daily EOM deposit snapshots, so
this is derivable with no new feed — hence provenance DERIVED, never invented.

This module holds only the maths so the trino and mock gateways share one rule and
it can be unit-tested without a warehouse.
"""
from __future__ import annotations

from typing import Any

# Thresholds on the trailing-quarter deposit change (fraction, negative = erosion).
AT_RISK_DROP = 0.20        # 20%+ erosion → retention risk
WATCH_DROP = 0.10          # 10–20% erosion → worth watching
RISE_NOTE = 0.10           # only call out growth once it's meaningful
MIN_MATERIAL_BASE = 25_000  # KES; below this a % swing isn't a meaningful wallet-share signal


def classify_retention(first_bal: float, last_bal: float) -> dict[str, Any] | None:
    """Classify a trailing-window deposit trend.

    Returns ``{'flag', 'trend_pct', 'note'}`` or ``None`` when it can't be judged:
    no base to measure against, or a base too small for the % swing to mean anything
    (a KES-few-hundred wallet halving isn't attrition — that would just be noise).
    """
    base = float(first_bal or 0)
    if base < MIN_MATERIAL_BASE:
        return None

    last = float(last_bal or 0)
    # Balance fully drawn down (now nil or overdrawn) — unambiguous erosion, and the
    # raw % would read as a nonsensical ">100% drop", so we state it plainly instead.
    if last <= 0:
        return {'flag': 'at_risk', 'trend_pct': -1.0,
                'note': 'Deposits drawn down to nil over the trailing quarter — retention risk.'}

    change = (last - base) / base
    drop = -change
    if drop >= AT_RISK_DROP:
        return {'flag': 'at_risk', 'trend_pct': round(change, 3),
                'note': f'Deposits down {round(drop * 100)}% over the trailing quarter — '
                        'retention risk (balance leaving while the account stays open).'}
    if drop >= WATCH_DROP:
        return {'flag': 'watch', 'trend_pct': round(change, 3),
                'note': f'Deposits down {round(drop * 100)}% over the trailing quarter — worth watching.'}
    if change >= 1.0:
        return {'flag': 'stable', 'trend_pct': round(change, 3),
                'note': 'Deposits more than doubled over the trailing quarter.'}
    if change >= RISE_NOTE:
        return {'flag': 'stable', 'trend_pct': round(change, 3),
                'note': f'Deposits up {round(change * 100)}% over the trailing quarter.'}
    return {'flag': 'stable', 'trend_pct': round(change, 3),
            'note': 'Deposit balance steady over the trailing quarter.'}
