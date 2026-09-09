"""Pure shaping for the lending-health panel — delinquency standing (npl_accounts /
pre_npl_accounts) and collateral held (collateral). No warehouse access, so the same
logic runs live and mock and is unit-testable.

Two honest notes encoded here:
* **Delinquency severity is ranked, not alphabetical** — LOSS is worse than DOUBTFUL is
  worse than SUBSTD, and a customer's standing is their *worst* classified account. A
  customer only on the pre-NPL WATCH list is a lighter early-warning, shown distinctly.
* **Collateral counts are noisy** — the source repeats collateral rows (one customer had
  104 'VEHICLE' rows), so we report the *distinct types held* and a raw count clearly
  labelled as records, never as a trustworthy asset value (values are blank upstream).
"""
from __future__ import annotations

from typing import Any

# Worse first. Rank drives the customer's overall standing (their worst account).
_NPL_RANK = {'LOSS': 3, 'DOUBTFUL': 2, 'SUBSTD': 1, 'SUBSTANDARD': 1}
_NPL_LABEL = {'LOSS': 'Loss', 'DOUBTFUL': 'Doubtful', 'SUBSTD': 'Substandard', 'SUBSTANDARD': 'Substandard'}


def shape_delinquency(npl_rows: list[dict[str, Any]], on_watch: bool,
                      month: str | None) -> dict[str, Any] | None:
    """npl_rows: [{classification, impairment}]. Returns the delinquency block, or None
    when the customer is neither classified NPL nor on the watch list."""
    scored = [(r, _NPL_RANK.get(str(r.get('classification') or '').strip().upper(), 0)) for r in npl_rows]
    scored = [(r, rank) for r, rank in scored if rank > 0]
    if scored:
        worst_row, worst_rank = max(scored, key=lambda x: x[1])
        key = str(worst_row.get('classification') or '').strip().upper()
        total_imp = sum(float(r.get('impairment') or 0) for r in npl_rows)
        return {
            'status': 'npl',
            'classification': _NPL_LABEL.get(key, key.title()),
            'severity': worst_rank,                    # 1..3 (higher = worse)
            'accounts': len(scored),
            'impairment': round(total_imp),
            'month': month,
        }
    if on_watch:
        return {'status': 'watch', 'classification': 'Watch', 'severity': 0,
                'accounts': 0, 'impairment': 0, 'month': month}
    return None


def shape_collateral(type_counts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """type_counts: [{type, count}]. Returns distinct collateral types held (Title-cased),
    ordered by frequency, or None when there is no collateral."""
    types: list[dict[str, Any]] = []
    for tc in type_counts:
        label = _title(tc.get('type'))
        if not label:
            continue
        types.append({'type': label, 'count': int(tc.get('count') or 0)})
    if not types:
        return None
    types.sort(key=lambda x: x['count'], reverse=True)
    return {'types': types, 'distinct': len(types)}


def _title(v: Any) -> str | None:
    if not isinstance(v, str):
        return None
    v = ' '.join(v.split())
    if not v:
        return None
    # Keep short all-caps tokens readable (e.g. 'TERM DEPOSIT COLLATERAL' -> 'Term Deposit Collateral')
    return v.title()
