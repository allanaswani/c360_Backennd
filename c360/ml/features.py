"""Feature & label engineering for the next-best-product model.

One place defines what the model sees, so training and scoring can never disagree.
Features come only from data we already query (product holdings, balances, segment,
tenure) — no new pipeline. Labels are **product ownership**: we train the model to
recognise "customers like this hold product X", then recommend X to look-alikes who
don't hold it yet (the standard cold-start for next-best-product when there is no
logged recommendation→outcome history).

Feature/label leakage is handled per product at train time: when modelling product P
we drop P's own ownership flag from the feature set.
"""
from __future__ import annotations

from datetime import date
from typing import Any

# Canonical product flags (mirrors CANON_LABELS in the gateway). 'deposit' is the
# entry product (nearly everyone has it → no signal) and 'mobile' needs a per-customer
# channel scan we don't do in bulk, so neither is a model target in v1.
PRODUCT_LABELS = {
    'current': 'Current Account', 'savings': 'Savings Account',
    'mortgage': 'Mortgage', 'asset_finance': 'Asset Finance', 'overdraft': 'Overdraft',
    'ipf': 'Insurance Premium Finance', 'cash_cover': 'Cash Cover',
    'trade': 'Trade Finance', 'unsecured': 'Unsecured Loan',
}
# Every product flag we compute (features may use all of these; the target for a given
# model is excluded from its own features).
ALL_FLAGS = ['current', 'savings', 'mortgage', 'asset_finance', 'overdraft',
             'ipf', 'cash_cover', 'trade', 'unsecured']

# Products the model ranks (must have PRODUCT_LABELS + appear as ALL_FLAGS).
TARGET_PRODUCTS = list(PRODUCT_LABELS.keys())

# Only leak-free features here. Aggregate counts (total_products / dep_n / loan_n)
# and has_loan were removed: they include the target product, so a holder looks
# "deeper" at train time than an identical non-holder does at score time — a label
# leak that collapsed predictions to ~0. The individual product flags (target
# excluded, see feature_columns) carry the cross-sell signal cleanly.
NUMERIC_FEATURES = ['log_dep_bal', 'log_loan_bal', 'tenure_years']
CATEGORICAL_FEATURES = ['segment']

# product_desc keyword → canonical flag (kept in sync with the gateway's maps).
_DEPOSIT_KEYWORDS = (
    ('current', 'current'), ('transactional', 'current'),
    ('saving', 'savings'), ('fanaka', 'savings'), ('target', 'savings'),
    ('nyumba', 'savings'), ('bond', 'savings'), ('take on', 'savings'),
)
_LOAN_KEYWORDS = (
    ('mortgage', 'mortgage'), ('owner occupier', 'mortgage'), ('purchase', 'mortgage'),
    ('housing', 'mortgage'), ('plot', 'mortgage'), ('construction', 'mortgage'),
    ('overdraft', 'overdraft'), ('asset', 'asset_finance'), ('motor', 'asset_finance'),
    ('vehicle', 'asset_finance'), ('lpo', 'trade'), ('trade', 'trade'),
    ('guarantee', 'trade'), ('insurance premium', 'ipf'), ('ipf', 'ipf'),
    ('personal', 'unsecured'), ('salary', 'unsecured'), ('unsecured', 'unsecured'),
)


def _match(desc: Any, keywords) -> str | None:
    if not desc:
        return None
    low = str(desc).lower()
    for needle, key in keywords:
        if needle in low:
            return key
    return None


def flags_from_products(deposit_products, loan_products) -> dict[str, int]:
    """Derive the canonical product-ownership flags from product_desc lists."""
    flags = {k: 0 for k in ALL_FLAGS}
    for p in (deposit_products or []):
        k = _match(p, _DEPOSIT_KEYWORDS)
        if k:
            flags[k] = 1
    for p in (loan_products or []):
        k = _match(p, _LOAN_KEYWORDS)
        if k:
            flags[k] = 1
    return flags


def _tenure_years(opened: str | None, as_of: date) -> float:
    if not opened:
        return 0.0
    try:
        y, m, d = int(opened[:4]), int(opened[5:7]), int(opened[8:10])
        days = (as_of - date(y, m, d)).days
        return max(0.0, round(days / 365.25, 2))
    except Exception:
        return 0.0


def _flags_from_gateway(holding_flags: dict) -> dict[str, int]:
    """Map a gateway holdings ``flags`` dict (booleans) onto the model's flag set."""
    return {k: int(bool(holding_flags.get(k))) for k in ALL_FLAGS}


def make_row(*, dep_bal, loan_bal, dep_n, loan_n, flags: dict[str, int],
             segment: str, tenure_years: float) -> dict[str, Any]:
    """Assemble one model-ready feature row from raw parts (used by train + score).
    Numeric balances are log1p-compressed so a few billion-shilling customers don't
    dominate the trees."""
    from math import log1p
    row = {
        'log_dep_bal': round(log1p(max(0.0, dep_bal or 0.0)), 4),
        'log_loan_bal': round(log1p(max(0.0, loan_bal or 0.0)), 4),
        'dep_n': int(dep_n or 0),
        'loan_n': int(loan_n or 0),
        'total_products': int(sum(flags.values())),
        'tenure_years': float(tenure_years or 0.0),
        'has_loan': int((loan_bal or 0) > 0),
        'segment': segment or 'Unsegmented',
    }
    row.update({f: int(flags.get(f, 0)) for f in ALL_FLAGS})
    return row


def feature_columns(target: str) -> list[str]:
    """The feature columns for a given target product — every base feature plus all
    OTHER product flags (never the target's own flag: that would leak the label)."""
    flag_feats = [f for f in ALL_FLAGS if f != target]
    return NUMERIC_FEATURES + CATEGORICAL_FEATURES + flag_feats


# ---------------------------------------------------------------------------
# Bulk training-set extraction (live warehouse only).
# ---------------------------------------------------------------------------
def extract_training_rows(gateway, *, sample: int = 80_000, id_chunk: int = 2000) -> list[dict]:
    """Pull a training sample of customers with their features + ownership flags.

    Two-step, avoiding the heavy deposits⋈dim_customer join: (1) sample N depositing
    customers with balance + product list (one bounded as-of scan, like the whole-book
    aggregates); (2) attach loan aggregates (only ~26k customers hold loans — cheap);
    (3) attach segment/tenure from dim_customer in chunked IN lookups.
    """
    t = gateway._t
    d, p = gateway._as_of_lit(), gateway._asof_part()
    not_internal = gateway._not_internal_cid()

    dep_rows = t.execute(
        f"SELECT cust_id cid, SUM(book_balance) bal, array_agg(DISTINCT product_desc) prods "
        f"FROM delta.gold_db.eom_deposits WHERE eom_date={d} {p} {not_internal} "
        f"GROUP BY cust_id LIMIT {int(sample)}")
    dep = {int(r['cid']): (float(r['bal'] or 0), list(r['prods'] or [])) for r in dep_rows if r.get('cid') is not None}
    if not dep:
        return []

    # ALL loan-holders (only ~26k) with their loan products — the labels for the
    # lending products. A random deposit sample is Whizz-heavy and catches almost no
    # borrowers, so lending models starved; pulling every borrower fixes that.
    loan_rows = t.execute(
        f"SELECT cust_id cid, SUM(gross_total) bal, array_agg(DISTINCT product_desc) prods "
        f"FROM delta.gold_db.eom_loans WHERE eom_date={d} {p} GROUP BY cust_id")
    loan = {int(r['cid']): (float(r['bal'] or 0), list(r['prods'] or [])) for r in loan_rows if r.get('cid') is not None}

    # Make sure every borrower is in the training set (with their real deposit balance),
    # not just those the deposit sample happened to catch.
    missing = [i for i in loan if i not in dep]
    for j in range(0, len(missing), id_chunk):
        chunk = missing[j:j + id_chunk]
        inlist = ','.join(str(x) for x in chunk)
        for r in t.execute(
            f"SELECT cust_id cid, SUM(book_balance) bal, array_agg(DISTINCT product_desc) prods "
            f"FROM delta.gold_db.eom_deposits WHERE eom_date={d} {p} AND cust_id IN ({inlist}) "
            f"GROUP BY cust_id"):
            if r.get('cid') is not None:
                dep[int(r['cid'])] = (float(r['bal'] or 0), list(r['prods'] or []))
    # Borrowers with no deposit row still belong in the set (deposit balance 0).
    for i in missing:
        dep.setdefault(i, (0.0, []))

    ids = list(dep.keys())
    ident: dict[int, dict] = {}
    for i in range(0, len(ids), id_chunk):
        chunk = ids[i:i + id_chunk]
        inlist = ','.join(str(x) for x in chunk)
        for r in t.execute(
            f"SELECT CAST(customer_id AS BIGINT) id, customer_segment seg, "
            f"CAST(account_opening_date AS varchar) opened FROM delta.gold_db.dim_customer "
            f"WHERE customer_id IN ({inlist})"):
            ident[int(r['id'])] = r

    as_of = gateway.as_of_date()
    rows = []
    for cid, (dep_bal, dep_prods) in dep.items():
        loan_bal, loan_prods = loan.get(cid, (0.0, []))
        flags = flags_from_products(dep_prods, loan_prods)
        info = ident.get(cid, {})
        rows.append(make_row(
            dep_bal=dep_bal, loan_bal=loan_bal,
            dep_n=len(dep_prods), loan_n=len(loan_prods), flags=flags,
            segment=gateway._seg_label(info.get('seg')),
            tenure_years=_tenure_years(gateway._safe_date(info.get('opened')), as_of)))
    return rows


def row_from_batch(*, summary: dict, holding_flags: dict) -> dict:
    """Build a scoring row from already-fetched worklist inputs (no per-customer
    queries) — used by the cross-sell worklist so it can rank with the model too.
    Tenure isn't in the batch summary, so it's left at 0 (a small feature)."""
    flags = _flags_from_gateway(holding_flags)
    dep_n = sum(flags[f] for f in ('current', 'savings')) + 1
    loan_n = sum(flags[f] for f in ('mortgage', 'asset_finance', 'overdraft', 'ipf',
                                    'cash_cover', 'trade', 'unsecured'))
    row = make_row(
        dep_bal=summary.get('deposits', 0), loan_bal=summary.get('loans', 0),
        dep_n=dep_n, loan_n=loan_n, flags=flags,
        segment=summary.get('segment') or 'Unsegmented', tenure_years=0.0)
    row['_held'] = {k for k, v in flags.items() if v}
    return row


def customer_feature_row(gateway, cust_id: str) -> dict | None:
    """Build the feature row for a single customer at scoring time, from data the app
    already fetches (holdings flags, deposit/loan snapshot, segment, tenure)."""
    cust = gateway.get_customer(cust_id)
    if not cust:
        return None
    holdings = gateway.get_product_holdings(cust_id).get('flags', {})
    value = gateway.get_relationship_value(cust_id)
    flags = _flags_from_gateway(holdings)
    # product counts from the flags we have (best-effort without re-querying products)
    dep_n = sum(flags[f] for f in ('current', 'savings')) + 1  # deposit entry product
    loan_n = sum(flags[f] for f in ('mortgage', 'asset_finance', 'overdraft', 'ipf',
                                    'cash_cover', 'trade', 'unsecured'))
    as_of = gateway.as_of_date()
    row = make_row(
        dep_bal=value.get('deposits', 0), loan_bal=value.get('loans', 0),
        dep_n=dep_n, loan_n=loan_n, flags=flags,
        segment=cust.get('segment') or 'Unsegmented',
        tenure_years=_tenure_years(cust.get('relationship_since'), as_of))
    row['_held'] = {k for k, v in flags.items() if v}   # products already held (skip these)
    return row
