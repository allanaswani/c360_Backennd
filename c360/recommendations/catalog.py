"""Product catalogue + analyst-defined complementary pairings.

Kept as data, not code, so an analyst can extend the rule inputs without touching
the engine. ``COMPLEMENTS`` encodes "holds X → should consider Y" pairings that
Rule A (product gap) reads. Domains beyond core banking are declared here so the
same structure extends once Whizz / Properties / Bancassurance holdings are sourced.
"""
from __future__ import annotations

# domain -> {product_key: display name}
CATALOG = {
    'HFCB': {
        'deposit': 'Deposit Account', 'current': 'Current Account', 'savings': 'Savings Account',
        'mobile': 'Mobile Banking', 'mortgage': 'Mortgage', 'asset_finance': 'Asset Finance',
        'overdraft': 'Overdraft', 'ipf': 'Insurance Premium Finance', 'cash_cover': 'Cash Cover',
        'trade': 'Trade Finance', 'unsecured': 'Unsecured Loan',
    },
    'Bancassurance': {'home_cover': 'Home Insurance', 'asset_cover': 'Asset Insurance',
                      'life_cover': 'Credit Life Cover'},
    'Whizz': {'whizz_wallet': 'Whizz Wallet', 'whizz_loan': 'Whizz Mobile Loan'},
    'Properties': {'property_advisory': 'Property Advisory'},
}

# Complementary pairings: if the customer HOLDS `held` (in any domain) but LACKS
# `suggest`, that's a gap candidate. `reason` is RM-speakable, no jargon/score.
COMPLEMENTS = [
    {
        'held': 'mortgage', 'suggest': 'ipf', 'suggest_domain': 'HFCB',
        'reason': 'Holds a mortgage but no insurance premium finance — cover the property risk on the loan.',
    },
    {
        'held': 'mortgage', 'suggest': 'home_cover', 'suggest_domain': 'Bancassurance',
        'reason': 'Has a mortgage with no home insurance cover attached.',
    },
    {
        'held': 'asset_finance', 'suggest': 'asset_cover', 'suggest_domain': 'Bancassurance',
        'reason': 'Financing an asset with no asset insurance — a natural attach.',
    },
    {
        'held': 'asset_finance', 'suggest': 'mobile', 'suggest_domain': 'HFCB',
        'reason': 'Active borrower not yet on mobile banking — push digital servicing.',
    },
    {
        'held': 'trade', 'suggest': 'overdraft', 'suggest_domain': 'HFCB',
        'reason': 'Trade-finance customer with no overdraft — likely needs working-capital headroom.',
    },
    {
        'held': 'current', 'suggest': 'savings', 'suggest_domain': 'HFCB',
        'reason': 'Transacts on a current account but holds no savings product.',
    },
]

# Products that make sense to recommend when Rule C (peer gap) fires but no
# specific complement matched — ordered by general priority for retail/SME.
GENERIC_GROWTH_PRODUCTS = [
    ('mobile', 'HFCB', 'Below the segment average on product holdings — start with mobile banking to deepen the relationship.'),
    ('savings', 'HFCB', 'Below the segment average on product holdings — a savings product is the natural next step.'),
    ('overdraft', 'HFCB', 'Below the segment average on product holdings — an overdraft facility fits this profile.'),
]
