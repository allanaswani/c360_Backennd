"""Next Best Product engine — v1 rules, one engine, two consumers.

Orchestration only: gather the customer's facts, run the three pure rules, apply
the *hard eligibility gate*, dedupe, rank, and emit the output contract. The same
function feeds the Level 2 per-customer panel (top 1-3) and the Level 1 cross-sell
worklist (ranked across all customers).

Output contract per item (stable from day one):
    {product, product_name, domain, reason, rule_id, score: float|null, eligible: bool|null}

The ``score`` field exists now and stays ``null`` in v1 so a v2 propensity model
slots in without a contract change. ``eligible`` is ``null`` — not ``true`` —
whenever the gate cannot be evaluated, because pretending eligibility is exactly
the failure the gate exists to prevent.

Eligibility gate (hard rule): risk classification + KYC status must both pass. These
are now **derived** from live data (``c360.risk`` — KYC from identity completeness,
operational risk from loan performance + leverage), so the gate is evaluable:

* pass  → candidates surface as actionable ``items``.
* fail  → candidates are computed but returned as ``withheld`` (status
  ``eligibility_hold``) with the blocking reason, so the RM sees the opportunity and
  why it's blocked, never a faked pass.
* profile unavailable (unknown customer) → ``eligibility_pending``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..warehouse.gateway import WarehouseGateway
from . import rules
from .rules import Candidate

ENGINE_VERSION = 'rules-v1'

# Risk classes that pass the gate. High operational risk is blocked.
_ELIGIBLE_RISK = {'low', 'medium'}
# KYC must be Verified (Partial/Incomplete customers are held for verification).
_ELIGIBLE_KYC = {'verified', 'complete'}


@dataclass
class RecommendationResult:
    status: str                       # 'ok' | 'eligibility_hold' | 'eligibility_pending'
    items: list[dict[str, Any]]       # eligible, ranked, RM-facing
    withheld: list[dict[str, Any]]    # computed but gate-blocked (preview)
    eligibility: dict[str, Any]
    engine_version: str = ENGINE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            'status': self.status,
            'engine_version': self.engine_version,
            'eligibility': self.eligibility,
            'items': self.items,
            'withheld': self.withheld,
        }


def _evaluate_gate(profile: dict | None) -> dict[str, Any]:
    if not profile:
        return {
            'gate_evaluable': False,
            'risk_class': None,
            'kyc_status': None,
            'note': 'Eligibility gate cannot run — no risk/KYC profile for this customer.',
        }
    risk = profile['risk']['class']
    kyc = profile['kyc']['status']
    risk_ok = str(risk).lower() in _ELIGIBLE_RISK
    kyc_ok = str(kyc).lower() in _ELIGIBLE_KYC
    passed = risk_ok and kyc_ok
    reasons = []
    if not risk_ok:
        reasons.append(f'{risk} operational risk')
    if not kyc_ok:
        reasons.append(f'KYC {kyc.lower()}')
    return {
        'gate_evaluable': True,
        'risk_class': risk,
        'kyc_status': kyc,
        'passed': passed,
        'basis': 'derived',   # risk/KYC are computed from live data, not a source feed
        'risk_factors': profile['risk'].get('factors', []),
        'kyc_checks': profile['kyc'].get('checks', []),
        'note': None if passed else 'Held — ' + ' and '.join(reasons) + '.',
    }


def _dedupe_rank(candidates: list[Candidate], value: dict) -> list[Candidate]:
    # Prefer the highest base_score per (product, domain); nudge by relationship
    # value so more valuable customers' gaps rank higher in the worklist.
    best: dict[tuple[str, str], Candidate] = {}
    for c in candidates:
        key = (c.product, c.domain)
        if key not in best or c.base_score > best[key].base_score:
            best[key] = c
    rv = value.get('relationship_value') or 0
    value_boost = min(rv / 100_000_000, 0.15)  # small, capped
    return sorted(best.values(), key=lambda c: c.base_score + value_boost, reverse=True)


def _to_item(c: Candidate, *, eligible: bool | None) -> dict[str, Any]:
    # ML candidates carry a real propensity in base_score (rule_id 'ml.*'); rule
    # candidates use base_score only as an internal ranking weight, so their score
    # stays null (no propensity to report).
    is_ml = str(c.rule_id).startswith('ml.')
    return {
        'product': c.product,
        'product_name': c.product_name,
        'domain': c.domain,
        'reason': c.reason,
        'rule_id': c.rule_id,
        'score': round(float(c.base_score), 4) if is_ml else None,
        'eligible': eligible,
    }


def _ml_candidates(gateway: WarehouseGateway, cust_id: str, *, limit: int) -> list[Candidate] | None:
    """Rank next-best products with the trained propensity model, if available.

    Returns rule-shaped ``Candidate``s (so the eligibility gate + output contract are
    unchanged) carrying the model's probability as ``base_score``. Returns None when no
    model is trained (fresh checkout / mock mode) so the caller falls back to rules."""
    if not _ml_enabled():
        return None
    try:
        from ..ml.features import customer_feature_row
        from ..ml.model import load_model
    except Exception:
        return None
    model = load_model()
    if model is None or not model.is_ready:
        return None
    row = customer_feature_row(gateway, cust_id)
    if row is None:
        return None
    picks = model.recommend(row, limit=limit)
    if not picks:
        return []
    return [Candidate(product=p['product'], product_name=p['product_name'], domain=p['domain'],
                      reason=p['reason'], rule_id=p['rule_id'], base_score=p['score']) for p in picks]


def recommend_for_customer(
    gateway: WarehouseGateway,
    cust_id: str,
    *,
    limit: int = 3,
) -> RecommendationResult:
    customer = gateway.get_customer(cust_id)
    if not customer:
        return RecommendationResult('ok', [], [], {'gate_evaluable': False, 'note': 'Unknown customer.'})

    # Prefer the trained ML model; fall back to the rule engine when it isn't available.
    ml = _ml_candidates(gateway, cust_id, limit=limit)
    if ml is not None:
        try:
            profile = gateway.get_risk_profile(cust_id)
        except Exception:
            profile = None
        return _result_from(ml[:limit], profile, engine='ml.lgbm-v1')

    holdings = gateway.get_product_holdings(cust_id)
    value = gateway.get_relationship_value(cust_id)
    # The segment benchmark is a whole-segment aggregate; in live mode it may not be
    # sourced yet. Treat that as "no benchmark" so rule C simply abstains — never a
    # fabricated peer average.
    try:
        benchmark = gateway.segment_product_benchmark(customer.get('segment') or '')
    except Exception:
        benchmark = None

    # Behaviour rule needs a recent activity level; a light snapshot is enough.
    try:
        # Light activity probe only — a narrow lookback keeps the worklist (which runs
        # this per customer) fast; the full feed uses the default wide lookback.
        txns = gateway.recent_transactions(cust_id, limit=30, lookback_months=2)
        txn_count = len(txns) * 8  # scale sample to a period-level proxy
    except Exception:
        txn_count = 0

    facts = {
        'holdings': holdings, 'value': value,
        'segment_benchmark': benchmark, 'txn_count': txn_count,
    }
    candidates: list[Candidate] = []
    for rule in rules.ALL_RULES:
        candidates.extend(rule(**facts))

    ranked = _dedupe_rank(candidates, value)[:limit]
    try:
        profile = gateway.get_risk_profile(cust_id)
    except Exception:
        profile = None
    return _result_from(ranked, profile)


def _result_from(ranked: list[Candidate], profile: dict | None,
                 *, engine: str = ENGINE_VERSION) -> RecommendationResult:
    """Apply the eligibility gate to ranked candidates → the output contract.
    Shared by the single-customer path and the batched worklist path. ``engine`` tags
    which producer ranked these (the rule engine or the ML model)."""
    gate = _evaluate_gate(profile)
    if not gate['gate_evaluable']:
        return RecommendationResult('eligibility_pending', [], [_to_item(c, eligible=None) for c in ranked], gate, engine)
    if not gate.get('passed'):
        # Gate ran and blocked — surface the candidates as held (with the reason)
        # so the RM sees the opportunity and why it can't be pitched yet.
        return RecommendationResult('eligibility_hold', [], [_to_item(c, eligible=False) for c in ranked], gate, engine)
    return RecommendationResult('ok', [_to_item(c, eligible=True) for c in ranked], [], gate, engine)


def worklist_across_customers(
    gateway: WarehouseGateway,
    *,
    sales_codes: list[str] | None,
    limit_per_customer: int = 1,
    max_customers: int | None = None,
    include_staff: bool = True,
) -> list[dict[str, Any]]:
    """Level 1 cross-sell worklist — same engine, ranked across the visible book.

    One row per customer (their single strongest next-best-product), so a customer
    never appears twice — the worklist is a de-duplicated call list, not a list of
    every candidate. In production this is a strong candidate for the nightly Celery
    precompute (§6). Here it runs directly; ``max_customers`` caps the per-customer
    engine passes so live mode (which re-queries per customer) stays responsive — the
    highest-value customers, where cross-sell matters most, are processed first.
    """
    # Prefer the cross-sell *prospect* sample (customers with real product gaps) when
    # the gateway offers it; fall back to the general roster otherwise.
    prospects = getattr(gateway, 'list_prospects', None)
    roster = None
    if prospects is not None and sales_codes is None:
        try:
            roster = prospects()
        except Exception:
            roster = None
    if not roster:
        roster = gateway.list_customers(sales_codes=sales_codes)
    # Staff customers (HF employees) are admin-only — strip them from a non-admin's
    # cross-sell call list. Every gateway stamps ``is_staff`` on its roster rows.
    if not include_staff:
        roster = [c for c in roster if not c.get('is_staff')]
    roster.sort(key=lambda c: (c.get('value') or 0), reverse=True)
    if max_customers is not None:
        roster = roster[:max_customers]
    # De-dupe the roster to one entry per customer up front.
    seen: set[str] = set()
    roster = [c for c in roster if not (c['cust_id'] in seen or seen.add(c['cust_id']))]
    ids = [c['cust_id'] for c in roster]

    # Fast path: batch holdings + risk in a handful of queries (not ~7 per customer),
    # so the worklist scales to the whole roster without per-customer round-trips.
    batch_flags = getattr(gateway, 'batch_product_flags', None)
    batch_risk = getattr(gateway, 'batch_risk_profiles', None)
    holdings_map = profiles_map = None
    if batch_flags and batch_risk and ids:
        try:
            value_map = {c['cust_id']: (c.get('deposits') or 0, c.get('loans') or 0) for c in roster}
            holdings_map = batch_flags(ids)
            profiles_map = batch_risk(ids, value_map)
        except Exception:
            holdings_map = profiles_map = None

    rows: list[dict[str, Any]] = []
    for summary in roster:
        cid = summary['cust_id']
        if holdings_map is not None:
            result = _recommend_batched(gateway, summary, holdings_map.get(cid),
                                        profiles_map.get(cid), limit=limit_per_customer)
        else:
            result = recommend_for_customer(gateway, cid, limit=limit_per_customer)
        # Worklist surfaces withheld candidates too (clearly flagged), so RMs see
        # the pipeline of opportunities even while a customer is gate-held.
        picks = (result.items or result.withheld)[:limit_per_customer]
        for item in picks:
            rows.append({**summary, 'recommendation': item, 'recommendation_status': result.status})
    rows.sort(key=lambda r: (r.get('value') or 0), reverse=True)
    return rows


def _recommend_batched(gateway, summary, holdings, profile, *, limit=1) -> RecommendationResult:
    """Run the rules + gate from pre-fetched batch inputs (no per-customer queries).
    Rule B (behaviour) is skipped here — its transaction probe would defeat batching;
    Rules A (product gap) and C (peer benchmark) carry the worklist."""
    if not holdings:
        return RecommendationResult('ok', [], [], {'gate_evaluable': False, 'note': 'No holdings.'})

    # Prefer the ML model here too — scored from the batch inputs (no extra queries).
    ml = _ml_candidates_batched(gateway, summary, holdings, limit=limit)
    if ml is not None:
        return _result_from(ml[:limit], profile, engine='ml.lgbm-v1')

    value = {'relationship_value': summary.get('value') or 0,
             'deposits': summary.get('deposits') or 0, 'loans': summary.get('loans') or 0}
    try:
        benchmark = gateway.segment_product_benchmark(summary.get('segment') or '')
    except Exception:
        benchmark = None
    facts = {'holdings': holdings, 'value': value, 'segment_benchmark': benchmark, 'txn_count': 0}
    candidates: list[Candidate] = []
    for rule in rules.ALL_RULES:
        candidates.extend(rule(**facts))
    ranked = _dedupe_rank(candidates, value)[:limit]
    return _result_from(ranked, profile)


def _ml_enabled() -> bool:
    """The propensity model is trained on the LIVE warehouse distribution, so it is
    only meaningful in live mode. In mock/seeded mode we stay on the rule engine (the
    demo's deterministic behaviour), and stale live models never leak into it."""
    try:
        from ..warehouse.factory import data_mode
        return data_mode() == 'live'
    except Exception:
        return False


def _ml_candidates_batched(gateway, summary, holdings, *, limit) -> list[Candidate] | None:
    """ML ranking for the worklist from pre-fetched batch inputs. None → fall back."""
    if not _ml_enabled():
        return None
    try:
        from ..ml.features import row_from_batch
        from ..ml.model import load_model
    except Exception:
        return None
    _ = gateway  # signature symmetry with the rule path; no queries needed here
    model = load_model()
    if model is None or not model.is_ready:
        return None
    row = row_from_batch(summary=summary, holding_flags=holdings.get('flags', {}))
    picks = model.recommend(row, limit=limit)
    return [Candidate(product=p['product'], product_name=p['product_name'], domain=p['domain'],
                      reason=p['reason'], rule_id=p['rule_id'], base_score=p['score']) for p in picks]
