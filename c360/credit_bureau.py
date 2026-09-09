"""Pure shaping of a TransUnion (CRB) scorecard row into the display record.

No warehouse access here — the same logic runs for live (Trino) and mock, and is
unit-testable in isolation. The source table
(``delta.gold_db.score_card_review_tu_accounts_summary``) repeats a national ID many
times, so the gateway is responsible for passing the LATEST row (by ``created_at``);
this module only normalises values, detects the bureau's *no-hit* sentinel, and
buckets the probability-of-default.

Two honest states matter downstream:
* a real **scored** record → score + grade + PD + adverse counts, with the pull date;
* a **no-hit** record → the customer is on the bureau but has no scoreable credit
  history. That is a genuine answer ("thin file"), NOT missing data — so it is kept
  and labelled, never rendered as a score of zero.

The no-hit sentinel observed in the data: ``score = 0`` together with
``probability = 999.99`` and/or ``score_grade = 'YY'``. Treating that 0 as a real
score would slander a good customer, so it is filtered here.
"""
from __future__ import annotations

from typing import Any

_NO_HIT_GRADE = 'YY'      # the bureau's 'no scoreable record' grade band
_PD_SENTINEL = 999.0      # probability 999.99 => no score on file


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def pd_band(pd: float | None) -> str | None:
    """Coarse default-risk descriptor from the bureau probability-of-default (%).

    These are OUR reading of the PD for a quick colour/label, not a bureau rating —
    the raw PD and grade are always shown alongside so nothing is hidden behind the
    bucket. Lower PD = better."""
    if pd is None:
        return None
    if pd < 5:
        return 'Very low'
    if pd < 15:
        return 'Low'
    if pd < 30:
        return 'Moderate'
    if pd < 50:
        return 'Elevated'
    return 'High'


def shape_bureau(raw: dict[str, Any], *, as_of: str | None) -> dict[str, Any]:
    """Normalise one bureau row (already the latest for the customer) into the panel
    record. ``as_of`` is the pull date (from ``created_at``), surfaced as provenance.

    Accepts either warehouse column names (``score_grade``, ``number_of_enquiries``)
    or the short aliases, so tests can pass a terse dict."""
    score = _int(raw.get('score'))
    grade = str(raw.get('grade') or raw.get('score_grade') or '').strip() or None
    pd = _float(raw.get('probability') if raw.get('probability') is not None else raw.get('pd'))

    # No-hit: no real score (0 / null) carrying the bureau's no-score markers.
    no_hit = ((score is None or score <= 0)
              and (pd is None or pd >= _PD_SENTINEL or (grade or '').upper() == _NO_HIT_GRADE))

    npl = _int(raw.get('non_performing')) or 0
    arr90 = _int(raw.get('arrears_90_days')) or 0
    enq = _int(raw.get('number_of_enquiries') if raw.get('number_of_enquiries') is not None
               else raw.get('enquiries')) or 0
    enq90 = _int(raw.get('enquiries_90_days')) or 0

    if no_hit:
        return {
            'no_hit': True,
            'score': None, 'grade': None, 'pd': None, 'pd_band': None,
            'non_performing': npl, 'arrears_90_days': arr90,
            'max_arrears_6m': _int(raw.get('max_arrears_last_6_months')) or 0,
            'enquiries': enq, 'enquiries_90_days': enq90,
            'as_of': as_of,
        }

    pd_clean = pd if (pd is not None and pd < _PD_SENTINEL) else None
    return {
        'no_hit': False,
        'score': score,
        'grade': grade,
        'pd': round(pd_clean, 2) if pd_clean is not None else None,
        'pd_band': pd_band(pd_clean),
        'non_performing': npl,
        'arrears_90_days': arr90,
        'max_arrears_6m': _int(raw.get('max_arrears_last_6_months')) or 0,
        'enquiries': enq,
        'enquiries_90_days': enq90,
        'as_of': as_of,
    }
