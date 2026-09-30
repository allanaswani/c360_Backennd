"""What a customer holds, in the bank's own product language.

The recommendation engine classifies products by keyword on ``product_desc`` (see
``CANON_LABELS`` in the trino gateway). That is good enough for a model feature but
not for telling an RM whether somebody has a current account: "MALIPO SALARY
ACCOUNT" and "MICRO BIZ ACCOUNT" are current accounts and contain neither word.

The core-banking system already answers the question. ``w_dim_product`` carries the
official product tree, and every live deposit and loan account joins to it through
``id_product = product_code`` (verified 2026-09-30: every account with a balance
matched, deposits and loans alike). This module turns that tree into the handful of
categories an RM talks in, and nothing else. It does no SQL.

Places where the tree says something an RM would read wrongly, each checked against
the products actually filed there:

* ``CURRENT ACCOUNT`` holds ``VIRTUAL ACCOUNT MOBILE`` (133k accounts) - the mobile
  wallet every VIRTUAL-segment customer opens. Counting it as a current account would
  tell an RM that 800k wallet holders already have one, so it is split out.
* Deposit ``OVERDRAFT`` holds only CURRENT USD / EURO / GBP - foreign-currency current
  accounts, not overdrafts.
* Deposit ``DUMMY`` holds MEMO PRODUCT-* and float accounts - internal postings, never
  a customer product. Kept out of what a customer "holds".
* Loan ``HIDDEN ACCOUNT`` holds OVERDRAFT-* products - overdrafts.
"""
from __future__ import annotations

from typing import Any

# The wallet product filed under CURRENT ACCOUNT, matched on the trimmed, upper-cased
# product description.
VIRTUAL_WALLET_PRODUCTS = frozenset({'VIRTUAL ACCOUNT MOBILE'})

# Deposit tree level 2 -> (key, label).
DEPOSIT_CATEGORIES: dict[str, tuple[str, str]] = {
    'CURRENT ACCOUNT': ('current', 'Current account'),
    'OVERDRAFT': ('current_fcy', 'Foreign-currency current account'),
    'SAVINGS ACCOUNT': ('savings', 'Savings account'),
    'NOTICE ACCOUNT': ('notice', 'Notice account'),
    'TERM DEPOSIT ACCOUNT': ('term_deposit', 'Fixed / term deposit'),
    'CALL ACCOUNT': ('call_deposit', 'Call deposit'),
}
WALLET = ('virtual_wallet', 'Virtual mobile account')
INTERNAL_DEPOSIT_LEVELS = frozenset({'DUMMY', 'NOSTRO FCY', 'VOSTRO A/CS', 'HIDDEN ACCOUNT'})

# Loan tree level 2 -> (key, label).
LOAN_CATEGORIES: dict[str, tuple[str, str]] = {
    'PURCHASE MORTGAGES': ('purchase_mortgage', 'Purchase mortgage'),
    'CONSTRUCTION MORTGAGES': ('construction_mortgage', 'Construction mortgage'),
    'BUY AND BUILD': ('buy_and_build', 'Buy and build'),
    'PLOT PURCHASE': ('plot_purchase', 'Plot purchase'),
    'VUNA HELA': ('vuna_hela', 'Vuna Hela'),
    'CONSUMER LOANS': ('consumer', 'Consumer loan'),
    'WORKING CAPITAL': ('working_capital', 'Working capital'),
    'HIDDEN ACCOUNT': ('overdraft', 'Overdraft'),
    'MOBILE LOANS': ('mobile_loan', 'Mobile loan'),
    'IPF LOAN PRODUCTS': ('ipf', 'Insurance premium finance'),
    'CASH COVER': ('cash_cover', 'Cash cover'),
    'PROJECTS': ('project_finance', 'Project finance'),
}

# The categories the "does this customer hold X" strip always shows, held or not, so
# an RM sees the gaps as clearly as the holdings.
HEADLINE = (
    ('current', 'Current account'),
    ('savings', 'Savings account'),
    ('term_deposit', 'Fixed / term deposit'),
    ('call_deposit', 'Call deposit'),
    ('notice', 'Notice account'),
    ('virtual_wallet', 'Virtual mobile account'),
)

MORTGAGE_KEYS = frozenset({'purchase_mortgage', 'construction_mortgage',
                           'buy_and_build', 'plot_purchase'})
DEPOSIT_KEYS = frozenset(k for k, _ in DEPOSIT_CATEGORIES.values()) | {WALLET[0]}
LOAN_KEYS = frozenset(k for k, _ in LOAN_CATEGORIES.values())


def _norm(value: Any) -> str:
    return ' '.join(str(value or '').split()).upper()


def deposit_category(level2: Any, product: Any) -> tuple[str, str] | None:
    """(key, label) for a deposit account, or None for an internal/unknown product."""
    l2 = _norm(level2)
    if l2 in INTERNAL_DEPOSIT_LEVELS:
        return None
    if l2 == 'CURRENT ACCOUNT' and _norm(product) in VIRTUAL_WALLET_PRODUCTS:
        return WALLET
    return DEPOSIT_CATEGORIES.get(l2)


def loan_category(level2: Any) -> tuple[str, str] | None:
    return LOAN_CATEGORIES.get(_norm(level2))


def summarise(deposits: list[dict], loans: list[dict]) -> dict[str, Any]:
    """Group per-account rows into the RM-facing product mix.

    ``deposits`` rows: {level2, product, balance}.
    ``loans`` rows:    {level2, product, balance}.
    Rows whose category is internal/unknown are dropped, never shown as a holding.
    """
    groups: dict[str, dict[str, Any]] = {}

    def add(side: str, cat: tuple[str, str], row: dict) -> None:
        key, label = cat
        g = groups.setdefault(key, {'key': key, 'label': label, 'side': side,
                                    'accounts': 0, 'balance': 0, 'products': []})
        g['accounts'] += 1
        g['balance'] += round(float(row.get('balance') or 0))
        name = ' '.join(str(row.get('product') or '').split()).title()
        if name and name not in g['products']:
            g['products'].append(name)

    for r in deposits:
        cat = deposit_category(r.get('level2'), r.get('product'))
        if cat:
            add('deposit', cat, r)
    for r in loans:
        cat = loan_category(r.get('level2'))
        if cat:
            add('loan', cat, r)

    held = set(groups)
    headline = []
    for key, label in HEADLINE:
        g = groups.get(key)
        # A foreign-currency current account answers "has a current account" too.
        if key == 'current' and g is None and 'current_fcy' in groups:
            g = groups['current_fcy']
        headline.append({'key': key, 'label': label, 'held': g is not None,
                         'accounts': g['accounts'] if g else 0,
                         'balance': g['balance'] if g else 0})

    dep = sorted((g for g in groups.values() if g['side'] == 'deposit'),
                 key=lambda g: g['balance'], reverse=True)
    lns = sorted((g for g in groups.values() if g['side'] == 'loan'),
                 key=lambda g: g['balance'], reverse=True)
    return {
        'headline': headline,
        'deposits': dep,
        'loans': lns,
        'loan_types': [g['label'] for g in lns],
        'holds_mortgage': bool(held & MORTGAGE_KEYS),
        'held_keys': sorted(held),
    }
