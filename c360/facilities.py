"""Credit facilities: what the bank sanctioned, and what the customer's mobile loans were.

Source is ``eom_agreement`` (the daily snapshot of loan agreements). Every live loan in
``eom_loans`` hangs off one agreement (verified 2026-09-30: all 28,607 matched on
agreement number), and the agreement carries the sanctioned limit.

Mobile loans are different, and the difference matters for what the screen claims.
Each Whizz / mobile loan is its own agreement, and its ``agr_limit`` is the amount
approved for THAT loan: customer 895 borrowed 10,000, then 3,000, then 6,000. So it
is the amount the customer asked for, not a standing eligibility limit. The Whizz
eligibility limit is not in the warehouse at all. This module therefore reports
mobile lending as a history of approved amounts, and never calls it a limit.

Also excluded: agreements with no limit, and the ``999,999,999,999`` placeholder
limits found on some DYNAMIC LIMIT agreements.

Pure - no SQL.
"""
from __future__ import annotations

from typing import Any

PLACEHOLDER_LIMIT = 1e11


def is_mobile(agreement_type: Any) -> bool:
    t = ' '.join(str(agreement_type or '').split()).upper()
    return 'MOBILE LOAN' in t or t.startswith('WHIZZ')


def _title(t: Any) -> str:
    s = ' '.join(str(t or '').split())
    s = s[:-len(' AGREEMENT')] if s.upper().endswith(' AGREEMENT') else s
    return s.title() if s else 'Facility'


def summarise(rows: list[dict]) -> dict[str, Any]:
    """``rows``: {agreement, type, limit, issued (ISO or None), outstanding}.

    Returns {'facilities': [...], 'mobile': {...} | None}.
    """
    facilities: list[dict] = []
    mobile: list[dict] = []
    for r in rows or []:
        limit = float(r.get('limit') or 0)
        if limit <= 0 or limit >= PLACEHOLDER_LIMIT:
            continue
        out = max(float(r.get('outstanding') or 0), 0.0)
        rec = {'agreement': r.get('agreement'), 'type': _title(r.get('type')),
               'limit': round(limit), 'outstanding': round(out),
               'issued': r.get('issued')}
        (mobile if is_mobile(r.get('type')) else facilities).append(rec)

    # An agreement stays in the snapshot after its loan is repaid (customer 121669 still
    # carries personal-loan agreements from 2019). Only a facility with money out is
    # "active"; the rest are history, and the totals never count them.
    for f in facilities:
        f['used_pct'] = round(f['outstanding'] / f['limit'], 4) if f['limit'] else None
        f['active'] = f['outstanding'] > 0
    facilities.sort(key=lambda f: (not f['active'], -f['limit']))
    active = [f for f in facilities if f['active']]

    mob = None
    if mobile:
        mobile.sort(key=lambda m: (m['issued'] or ''))
        dated = [m for m in mobile if m['issued']]
        latest = dated[-1] if dated else mobile[-1]
        mob = {
            'loans_taken': len(mobile),
            'first_issued': dated[0]['issued'] if dated else None,
            'latest_issued': latest['issued'],
            'latest_amount': latest['limit'],
            'highest_amount': max(m['limit'] for m in mobile),
            'total_approved': sum(m['limit'] for m in mobile),
            'outstanding': sum(m['outstanding'] for m in mobile),
            'history': [{'period': m['issued'], 'amount': m['limit'],
                         'outstanding': m['outstanding']} for m in dated],
        }
    return {
        'facilities': facilities,
        'active_count': len(active),
        'past_count': len(facilities) - len(active),
        'sanctioned_total': sum(f['limit'] for f in active),
        'outstanding_total': sum(f['outstanding'] for f in active),
        'mobile': mob,
    }
