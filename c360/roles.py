"""Canonical role registry — mirrors the HF Group backend (`core.roles`).

Roles are Django ``auth.Group`` rows (the authoritative source, carried over from
the legacy system), plus a coarser forward-looking *tier* (admin/manager/officer)
derived from the group. Customer 360 uses the tier to resolve book scope:

* ``admin`` / ``manager`` → whole-book (management / analytics)
* ``officer``             → own book (relationship manager), scoped by sales_code

Keeping the exact group names and tier mapping means an account provisioned here is
consistent with the rest of the estate. Change the mapping only in this file.
"""
from __future__ import annotations

# --- legacy roles: exact auth_group.name values from the old project ---------
LEGACY_ROLES = (
    'ceo', 'exco', 'portfolio_mgt', 'tl_portfolio', 'branch_portfolio',
    'collection_mgt', 'tl_collection', 'rights_issue', 'TLrights_issue', 'hfdi_admin',
)

LEGACY_ROLE_DESCRIPTIONS = {
    'ceo': 'Group CEO — full read access across all segments and branches.',
    'exco': 'Executive committee — full read access across all segments.',
    'portfolio_mgt': 'Portfolio management — manages the portfolio reallocation book.',
    'tl_portfolio': 'Team leader, portfolio — segment-level portfolio oversight.',
    'branch_portfolio': 'Branch portfolio — branch-level relationship managers.',
    'collection_mgt': 'Collections management — manages the collections book.',
    'tl_collection': 'Team leader, collections — segment-level collections oversight.',
    'rights_issue': 'Rights issue — front-line rights issue users.',
    'TLrights_issue': 'Team leader, rights issue — rights issue oversight.',
    'hfdi_admin': 'HFDI administrator — HFDI module administration.',
}

# --- forward-looking role tiers ---------------------------------------------
ROLE_ADMIN = 'admin'
ROLE_MANAGER = 'manager'
ROLE_OFFICER = 'officer'
ROLE_CUSTOMER = 'customer'

ROLE_TIERS = (ROLE_ADMIN, ROLE_MANAGER, ROLE_OFFICER, ROLE_CUSTOMER)

# Higher rank == more privilege; used to pick the strongest tier across groups.
_TIER_RANK = {ROLE_CUSTOMER: 0, ROLE_OFFICER: 1, ROLE_MANAGER: 2, ROLE_ADMIN: 3}

# Customer 360's own roles — the two audiences the product is built for. They sit
# alongside the legacy names so an operator can provision C360 access explicitly.
C360_ROLES = ('c360_management', 'c360_rm')
C360_ROLE_DESCRIPTIONS = {
    'c360_management': 'Customer 360 — management / analytics (whole-book Level 1).',
    'c360_rm': 'Customer 360 — relationship manager (own book).',
}

ALL_ROLES = LEGACY_ROLES + C360_ROLES
ALL_ROLE_DESCRIPTIONS = {**LEGACY_ROLE_DESCRIPTIONS, **C360_ROLE_DESCRIPTIONS}

# Group name -> tier. Executives/admins → admin; *_mgt and team leaders → manager;
# front-line groups → officer. Conservative and documented — tune only here.
ROLE_TO_TIER = {
    'ceo': ROLE_ADMIN,
    'exco': ROLE_ADMIN,
    'hfdi_admin': ROLE_ADMIN,
    'portfolio_mgt': ROLE_MANAGER,
    'collection_mgt': ROLE_MANAGER,
    'tl_portfolio': ROLE_MANAGER,
    'tl_collection': ROLE_MANAGER,
    'TLrights_issue': ROLE_MANAGER,
    'branch_portfolio': ROLE_OFFICER,
    'rights_issue': ROLE_OFFICER,
    # Customer 360
    'c360_management': ROLE_MANAGER,
    'c360_rm': ROLE_OFFICER,
}


def tier_for_groups(group_names) -> str:
    """Strongest tier implied by a set of group names; defaults to ``officer``
    (the old default for every authenticated RM) when none are recognised."""
    best, best_rank = None, -1
    for name in group_names:
        tier = ROLE_TO_TIER.get(name)
        if tier is None:
            continue
        rank = _TIER_RANK[tier]
        if rank > best_rank:
            best, best_rank = tier, rank
    return best or ROLE_OFFICER
