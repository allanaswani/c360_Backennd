"""Pure shaping for the subsidiary CRM panels — property sales leads (HFDI) and the
insurance CRM profile (HFBI). No warehouse access, so the same logic runs for live and
mock and is unit-testable.

Two honest caveats these shapers encode:
* **Property leads are phone-matched** (the lead export carries no customer/national id),
  so the panel is tagged 'matched by phone' by the caller and only resolves for the
  minority of leads whose phone is also a bank customer's. The source is messy —
  mixed-case states, unreliable/null timestamps and campaign names — so we summarise to
  a lead count, the *furthest* pipeline stage reached, and follow-up engagement rather
  than pretending to a clean event timeline.
* **Insurance leads don't exist** (the HFBI leads table is empty); the insurance panel is
  the HFBI *customer* record (servicing RM/agent, occupation, branch), bridged cleanly by
  national ID.
"""
from __future__ import annotations

from typing import Any, Iterable

# Raw lead states (from hfdi_lead_data, mixed case: 'SEALED' / 'prospect') mapped to a
# ranked pipeline. Value is (rank, display label, funnel_index, kind). Higher rank = more
# advanced. ``funnel_index`` is the customer's position on the 4-step sales funnel the UI
# draws (see FUNNEL); ``kind`` is 'active' (in play), 'won' (closed-won) or 'lost' (closed-
# lost). Unknown states rank 0 at the Prospect step.
_STAGE = {
    'sealed': (6, 'Sealed', 3, 'won'),
    'selling': (5, 'Selling', 2, 'active'),
    'booked': (5, 'Booked', 2, 'active'),
    'potential': (4, 'Potential', 1, 'active'),
    'meeting': (4, 'Meeting', 1, 'active'),
    'prospect': (3, 'Prospect', 0, 'active'),
    'holding': (3, 'Holding', 0, 'active'),
    'open': (2, 'Open', 0, 'active'),
    'waiting': (2, 'Waiting', 0, 'active'),
    'failed': (1, 'Not converted', 0, 'lost'),
    'blacklist': (1, 'Blacklisted', 0, 'lost'),
}

# The 4-step funnel the UI renders. A customer's furthest lead maps onto one step.
FUNNEL = ['Prospect', 'Engaged', 'Booked', 'Sealed']


def furthest_stage(states: Iterable[str | None]) -> tuple[str, int, str]:
    """The most-advanced stage across a customer's leads → (label, funnel_index, kind).
    A customer who ever progressed outranks a failed lead, so 'lost' only shows when
    every lead failed. Empty / all-unknown → the neutral Prospect step."""
    best_rank, best = -1, ('Prospect', 0, 'active')
    for s in states:
        key = (s or '').strip().lower()
        rank, label, idx, kind = _STAGE.get(key, (0, (s or '').strip().title() or 'Prospect', 0, 'active'))
        if rank > best_rank:
            best_rank, best = rank, (label, idx, kind)
    return best


def shape_property_leads(states: list[str | None], followup_count: int,
                         followup_ok: int) -> dict[str, Any]:
    """Compact property-sales CRM summary for the matched customer — a pipeline position
    plus follow-up engagement, drawn as a sales funnel in the UI."""
    label, idx, kind = furthest_stage(states)
    return {
        'lead_count': len(states),
        'stage': label,          # the precise furthest state (e.g. 'Holding')
        'stage_index': idx,      # 0..3 position on FUNNEL
        'stage_kind': kind,      # 'active' | 'won' | 'lost'
        'funnel': FUNNEL,        # step labels, so the UI and backend can't drift
        'followups': int(followup_count or 0),
        'followups_successful': int(followup_ok or 0),
        'bridge': 'phone',       # drives the 'matched by phone' caveat in the UI
    }


def _person_from_email(v: str | None) -> str | None:
    """Turn a work email ('robert.mugo@hfgroup.co.ke') into a display name
    ('Robert Mugo'). A value that isn't an email is returned trimmed as-is."""
    if not v:
        return None
    v = v.strip()
    if '@' not in v:
        return ' '.join(v.split()) or None
    local = v.split('@', 1)[0]
    parts = [p for p in local.replace('_', '.').split('.') if p]
    return ' '.join(p.capitalize() for p in parts) or None


def shape_insurance_crm(row: dict[str, Any]) -> dict[str, Any] | None:
    """HFBI customer/CRM record → panel dict, or None when it carries nothing worth
    showing (all servicing/profile fields blank)."""
    def clean(v: Any) -> str | None:
        if isinstance(v, str):
            v = ' '.join(v.split())
            return v or None
        return v or None

    rm = _person_from_email(clean(row.get('risk_manager')))
    agent = clean(row.get('sales_person'))
    occupation = clean(row.get('occupation'))
    branch = clean(row.get('branch'))
    location = clean(row.get('location'))
    if not any([rm, agent, occupation, branch, location]):
        return None
    return {
        'risk_manager': rm,
        'agent': agent,
        'occupation': occupation,
        'branch': branch,
        'location': location,
    }
