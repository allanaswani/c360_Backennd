"""Where a customer stands among the others in their segment.

The segment distribution is computed once for the whole book (99 percentile points
per measure per segment) and cached; placing one customer is then arithmetic. The
population is every customer in the segment holding at least one account or loan
with a balance at the latest close - the same customers the book totals count.

A customer whose value is zero is not given a rank: "top 60%" for an empty figure
reads as praise for nothing. The panel says "none" instead.

Pure - no SQL.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from typing import Any

MEASURES = (
    ('deposits', 'Deposits', 'kes'),
    ('loans', 'Loans', 'kes'),
    ('products', 'Product types held', 'count'),
    ('revenue', 'Revenue this year', 'kes'),
)


def percentile_of(value: float, points: list[float]) -> float | None:
    """Share of the segment below ``value`` (0..1), from 99 percentile points; ties
    count half, so a value equal to the median reads as about 0.5."""
    if not points:
        return None
    pts = sorted(float(p) for p in points if p is not None)
    if not pts:
        return None
    lo, hi = bisect_left(pts, value), bisect_right(pts, value)
    return round((lo + (hi - lo) / 2) / len(pts), 4)


def position(customer: dict[str, float], dist: dict[str, Any]) -> dict[str, Any]:
    """``customer``: {measure: value}. ``dist``: {'customers': n, measure: [99 points]}."""
    rows = []
    for key, label, fmt in MEASURES:
        v = float(customer.get(key) or 0)
        pts = dist.get(key) or []
        median = sorted(pts)[len(pts) // 2] if pts else None
        pct = percentile_of(v, pts) if v > 0 else None
        if pct is None:
            standing = 'None held' if key != 'revenue' else 'None this year'
        else:
            top = max(1, round((1 - pct) * 100))
            standing = (f'Top {top}%' if top <= 50 else f'Bottom {max(1, round(pct * 100))}%')
        rows.append({'key': key, 'label': label, 'fmt': fmt, 'value': round(v),
                     'median': round(median) if median is not None else None,
                     'percentile': pct, 'standing': standing})
    return {'customers': int(dist.get('customers') or 0), 'measures': rows}
