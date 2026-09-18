"""Product catalogue + analyst-defined complementary pairings.

Kept as data, not code, so an analyst can extend the rule inputs without touching
the engine. ``COMPLEMENTS`` encodes "holds X → should consider Y" pairings that
Rule A (product gap) reads. Domains beyond core banking are declared here so the
same structure extends once Whizz / Properties / Bancassurance holdings are sourced.
"""
from __future__ import annotations

from .. import brand

# domain -> {product_key: display name}
# The domain keys are DISPLAY labels (the panel renders them as a chip next to the
# product), so they come from the brand module - see c360/brand.py.
CATALOG = {
    brand.DOMAIN_LABELS['bank']: {
        'deposit': 'Deposit Account', 'current': 'Current Account', 'savings': 'Savings Account',
        'mobile': 'Mobile Banking', 'mortgage': 'Mortgage', 'asset_finance': 'Asset Finance',
        'overdraft': 'Overdraft', 'ipf': 'Insurance Premium Finance', 'cash_cover': 'Cash Cover',
        'trade': 'Trade Finance', 'unsecured': 'Unsecured Loan',
    },
    brand.DOMAIN_LABELS['insurance']: {'home_cover': 'Home Insurance', 'asset_cover': 'Asset Insurance',
                      'life_cover': 'Credit Life Cover'},
    brand.DOMAIN_LABELS['digital']: {'whizz_wallet': 'Whizz Wallet', 'whizz_loan': 'Whizz Mobile Loan'},
    brand.DOMAIN_LABELS['property']: {'property_advisory': 'Property Advisory'},
}

# Complementary pairings: if the customer HOLDS `held` (in any domain) but LACKS
# `suggest`, that's a gap candidate. `reason` is RM-speakable, no jargon/score.
COMPLEMENTS = [
    {
        'held': 'mortgage', 'suggest': 'ipf', 'suggest_domain': brand.DOMAIN_LABELS['bank'],
        'reason': 'Holds a mortgage but no insurance premium finance, so the property risk on the loan is uncovered.',
    },
    {
        'held': 'mortgage', 'suggest': 'home_cover', 'suggest_domain': 'Bancassurance',
        'reason': 'Has a mortgage with no home insurance cover attached.',
    },
    {
        'held': 'asset_finance', 'suggest': 'asset_cover', 'suggest_domain': 'Bancassurance',
        'reason': 'Financing an asset with no asset insurance, which is a natural attach.',
    },
    {
        'held': 'asset_finance', 'suggest': 'mobile', 'suggest_domain': brand.DOMAIN_LABELS['bank'],
        'reason': 'Active borrower not yet on mobile banking, so digital servicing is the obvious next step.',
    },
    {
        'held': 'trade', 'suggest': 'overdraft', 'suggest_domain': brand.DOMAIN_LABELS['bank'],
        'reason': 'Trade-finance customer with no overdraft, so they likely need working-capital headroom.',
    },
    {
        'held': 'current', 'suggest': 'savings', 'suggest_domain': brand.DOMAIN_LABELS['bank'],
        'reason': 'Transacts on a current account but holds no savings product.',
    },
]

# Products that make sense to recommend when Rule C (peer gap) fires but no
# specific complement matched — ordered by general priority for retail/SME.
GENERIC_GROWTH_PRODUCTS = [
    ('mobile', brand.DOMAIN_LABELS['bank'], 'Below the segment average on product holdings. Mobile banking is the place to start.'),
    ('savings', brand.DOMAIN_LABELS['bank'], 'Below the segment average on product holdings. A savings product is the natural next step.'),
    ('overdraft', brand.DOMAIN_LABELS['bank'], 'Below the segment average on product holdings. An overdraft facility fits this profile.'),
]
