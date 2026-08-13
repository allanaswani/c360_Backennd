"""The three v1 recommendation rules.

Each rule is a pure function: it takes a customer's facts and returns a list of
``Candidate`` tuples. No SQL, no eligibility logic, no ranking here — those live in
``engine.py``. Keeping the rules isolated and pure is what makes them auditable:
an analyst can read one function and know exactly when it fires.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import catalog


@dataclass(frozen=True)
class Candidate:
    product: str          # product key
    product_name: str
    domain: str
    reason: str           # plain-language, RM-speakable
    rule_id: str          # 'A' | 'B' | 'C' (+ suffix), auditable
    base_score: float     # internal ranking weight (NOT the propensity score)


def _held_keys(holdings: dict) -> set[str]:
    return {k for k, v in holdings.get('flags', {}).items() if v}


def rule_a_product_gap(holdings: dict, **_) -> list[Candidate]:
    """Rule A — holds X, lacks complementary Y."""
    held = _held_keys(holdings)
    out: list[Candidate] = []
    for pair in catalog.COMPLEMENTS:
        if pair['held'] in held and pair['suggest'] not in held:
            dom = pair['suggest_domain']
            name = catalog.CATALOG.get(dom, {}).get(pair['suggest'], pair['suggest'])
            out.append(Candidate(
                product=pair['suggest'], product_name=name, domain=dom,
                reason=pair['reason'], rule_id='A', base_score=0.72,
            ))
    return out


def rule_b_behaviour(holdings: dict, *, value: dict, txn_count: int, **_) -> list[Candidate]:
    """Rule B — behaviour suggests unmet need.

    Signals: high transaction volume relative to deposit balance, or strong
    channel engagement with no lending relationship.
    """
    held = _held_keys(holdings)
    out: list[Candidate] = []
    deposits = value.get('deposits') or 0
    loans = value.get('loans') or 0

    # High activity, thin balance → likely needs credit / an overdraft buffer.
    if txn_count >= 200 and deposits < 500_000 and loans == 0:
        if 'overdraft' not in held:
            out.append(Candidate(
                product='overdraft', product_name='Overdraft', domain='HFCB',
                reason='High transaction volume against a low deposit balance — a short-term overdraft fits the cash-flow pattern.',
                rule_id='B', base_score=0.66,
            ))
        if 'unsecured' not in held:
            out.append(Candidate(
                product='unsecured', product_name='Unsecured Loan', domain='HFCB',
                reason='Consistently active with little idle balance — a pre-qualified personal loan is worth a conversation.',
                rule_id='B', base_score=0.58,
            ))
    return out


# A customer must be at least this far below the segment average before rule C fires,
# so a trivial gap (e.g. 0.01 below a 1.0 average) never triggers a noisy "grow" nudge.
_MIN_BENCHMARK_GAP = 0.5


def rule_c_peer_benchmark(holdings: dict, *, segment_benchmark: float | None, **_) -> list[Candidate]:
    """Rule C — holds meaningfully fewer products than the segment typically does."""
    if not segment_benchmark:  # benchmark unavailable → abstain, invent nothing
        return []
    held = _held_keys(holdings)
    products_held = len(held)
    if segment_benchmark - products_held < _MIN_BENCHMARK_GAP:
        return []
    for key, dom, reason in catalog.GENERIC_GROWTH_PRODUCTS:
        if key not in held:
            gap = round(segment_benchmark - products_held, 1)
            return [Candidate(
                product=key, product_name=catalog.CATALOG[dom][key], domain=dom,
                reason=f'{reason} (holds {products_held}, segment average {segment_benchmark:g}, gap {gap}).',
                rule_id='C', base_score=0.5 + min(gap, 2) * 0.06,
            )]
    return []


# Enough idle cash that moving it into a growth product is a real conversation.
_IDLE_DEPOSIT_MIN = 1_000_000


def rule_d_idle_deposits(holdings: dict, *, value: dict, **_) -> list[Candidate]:
    """Rule D — sizeable deposits sitting with no savings/investment product."""
    held = _held_keys(holdings)
    deposits = value.get('deposits') or 0
    if deposits >= _IDLE_DEPOSIT_MIN and 'savings' not in held:
        return [Candidate(
            product='savings', product_name='Savings Account', domain='HFCB',
            reason=f'Holds ~KES {deposits / 1_000_000:.1f}M in deposits with no dedicated savings product — '
                   f'move idle balance into a yield-bearing account.',
            rule_id='D', base_score=0.64,
        )]
    return []


def rule_e_lending_only(holdings: dict, *, value: dict, **_) -> list[Candidate]:
    """Rule E — borrows from us but banks the day-to-day elsewhere; capture the
    primary transactional relationship (or at least push digital servicing)."""
    held = _held_keys(holdings)
    loans = value.get('loans') or 0
    lending = held & {'mortgage', 'asset_finance', 'trade', 'overdraft', 'unsecured', 'ipf'}
    transactional = held & {'current', 'deposit', 'savings', 'mobile'}
    if lending and loans > 0 and not transactional:
        return [Candidate(
            product='current', product_name='Current Account', domain='HFCB',
            reason='Borrows from us but keeps the day-to-day banking elsewhere — win the primary transactional account.',
            rule_id='E', base_score=0.70,
        )]
    if lending and 'mobile' not in held:
        return [Candidate(
            product='mobile', product_name='Mobile Banking', domain='HFCB',
            reason='Active borrower not yet on mobile banking — deepen the relationship with digital servicing.',
            rule_id='E', base_score=0.54,
        )]
    return []


ALL_RULES = (rule_a_product_gap, rule_b_behaviour, rule_c_peer_benchmark,
             rule_d_idle_deposits, rule_e_lending_only)
