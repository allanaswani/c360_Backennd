"""Live warehouse gateway (Trino / delta.gold_db).

Only the ``delta`` catalog is reachable from the app tier, so *everything live*
sources from ``delta.gold_db`` (verified 2026-07-23 — see the live-warehouse notes):
identity from ``dim_customer`` (the clean 1-row-per-customer master), value from
``eom_deposits`` / ``eom_loans`` aggregated at the last closed EOM date.

DUPLICATE SAFETY IS THE CORE INVARIANT HERE. ``eom_deposits`` / ``eom_loans`` hold
one row *per account* (a single customer can have 88 deposit accounts), so joining
raw fact rows fans out and inflates every figure. Every value query therefore
pre-aggregates each fact to exactly one row per customer (GROUP BY / scalar
subquery) *before* touching identity. This was validated against a real customer:
the naive join produced 176 rows and the safe query 1 row with matching sums.

Now wired live (all dedup-safe): identity + value, per-account lists, product
holdings (canonical flags mapped from real ``product_desc``), the deposit/loan and
disbursement balance series (grouped per EOM day), transaction-value trend / channel
mix / recent transactions (from the purpose-built ``rpt_c360_*`` summary tables), and
a *bounded* live portfolio sample for Level 1.

Whizz (KOCELA transactions + customers_whizz profile) and Properties (the property register, bridged by
national ID, deduped by unit_id) are now live too. Still deferred (raise
``LiveDataNotReady`` so the service degrades to an honest empty state rather than a wrong
number): Bancassurance and the true whole-book segment benchmark.
"""
from __future__ import annotations

import logging
import re as _re
import time
from datetime import date, timedelta
from typing import Any

from ... import credit_bureau as bureau_shape
from ... import brand
from ... import crm as crm_shape
from ... import property_register as prop_reg
from ... import lending as lending_shape
from ... import retention as retention_derive
from ... import relationships as rel_shape
from .. import rm_balances as rm_bal
from ... import insurance_register as ins_reg
from ... import risk as risk_derive
from ...rbac.staff import is_staff_from_fields
from ..connector import TrinoConnector

logger = logging.getLogger('c360')



def _match_note(phone_only: int, name_only: int) -> str | None:
    """Say which policies were reached by a weaker bridge, and why that matters.

    Never a silent fuzzy match. A phone can be shared by a family or a business, and
    a name match is only made when the name is unique on both sides - both are worth
    stating so an RM can sense-check before acting on them.
    """
    parts = []
    if phone_only:
        parts.append(f'{phone_only} matched by phone number rather than ID, so a shared '
                     f'handset could put another party on this page')
    if name_only:
        parts.append(f'{name_only} matched on a name that is unique in both systems, '
                     f'there being no ID or phone on either side to match on')
    if not parts:
        return None
    return 'Worth a check: ' + '; and '.join(parts) + '.'

def _date_sort_key(value) -> float:
    """ISO date to a sortable number; missing dates sort last."""
    if not value:
        return 0.0
    try:
        return float(str(value).replace('-', ''))
    except (TypeError, ValueError):
        return 0.0
from ..gateway import WarehouseGateway
from ..periods import ResolvedPeriod

_ENTRY_STATUS = {
    '0': 'Inactive', '1': 'Active', '2': 'Locked', '3': 'Closed',
    '4': 'Closed by bank', '5': 'Blocked', '6': 'Dormant',
}

# Canonical product universe the recommendation engine reasons over (same keys the
# mock uses, so the rules are identical in both modes). Live holdings are derived by
# mapping real ``product_desc`` strings onto these keys.
CANON_LABELS = {
    'deposit': 'Deposit Account', 'current': 'Current Account', 'savings': 'Savings Account',
    'mobile': 'Mobile Banking', 'mortgage': 'Mortgage', 'asset_finance': 'Asset Finance',
    'overdraft': 'Overdraft', 'ipf': 'Insurance Premium Finance', 'cash_cover': 'Cash Cover',
    'trade': 'Trade Finance', 'unsecured': 'Unsecured Loan',
}

# product_desc keyword -> canonical flag. Ordered: first match wins per side.
_DEPOSIT_KEYWORDS = (
    ('current', 'current'), ('transactional', 'current'),
    ('saving', 'savings'), ('fanaka', 'savings'), ('target', 'savings'),
    ('nyumba', 'savings'), ('bond', 'savings'), ('take on', 'savings'),
)
# Ordered, first-match-wins per description. The specific / least-ambiguous loan
# types are listed BEFORE the mortgage bucket, because HF's names overlap: a
# "hire purchase" or "motor vehicle purchase" is asset finance, not a mortgage,
# and must be caught first. A bare "purchase" is deliberately NOT a mortgage
# trigger — it appears in hire/asset/stock purchase too; mortgages are matched on
# genuinely property-specific terms ("mortgage", "owner occupier", "plot", …).
# Getting this wrong makes the recommendation panel claim "holds a mortgage" for
# a customer who has none, so keep mortgage terms unambiguous.
_LOAN_KEYWORDS = (
    ('insurance premium', 'ipf'), ('ipf', 'ipf'),
    ('hire purchase', 'asset_finance'), ('asset', 'asset_finance'),
    ('motor', 'asset_finance'), ('vehicle', 'asset_finance'), ('equipment', 'asset_finance'),
    ('lpo', 'trade'), ('trade', 'trade'), ('guarantee', 'trade'),
    ('overdraft', 'overdraft'),
    ('mortgage', 'mortgage'), ('owner occupier', 'mortgage'), ('home loan', 'mortgage'),
    ('housing', 'mortgage'), ('residential', 'mortgage'), ('plot', 'mortgage'),
    ('construction', 'mortgage'),
    ('personal', 'unsecured'), ('salary', 'unsecured'), ('check off', 'unsecured'),
    ('check-off', 'unsecured'), ('unsecured', 'unsecured'),
)

# --- transaction feed: fact_dep_trx_recording ---------------------------------
# The real per-customer transaction ledger (covers 200/200 sampled customers, up to
# the as-of day). The rpt_c360_* summaries were near-empty (0/200) — using them was
# why the trend/channel/recent charts showed "no data". These are the technical /
# accounting channels to exclude so only customer-facing activity is counted.
_SYS_CHANNELS_SQL = (
    "'BATCH','SAP FINANCE GL','WSO2 - ENTERPRISE SERVICE BUS',"
    "'ESB - ENTERPRISE SERVICE BUS','DEFAULT'"
)
# Friendly display names for the raw channel_description values (keyed upper-cased).
_CHANNEL_DISPLAY = {
    'KOCELA - SUBSCRIBER AND PAYMENT CHANNEL': 'Whizz / M-Pesa',
    'ON-LINE CHANNEL': 'Online',
    'MOBILE BANKING CHANNEL': 'Mobile',
    'INTERNET': 'Internet',
    'ATM': 'ATM',
    'MIPS CHEQUES': 'Cheque',
    'PROFITS GATEWAY': 'Branch',
}
# Channels that indicate the customer banks digitally (drives the mobile flag).
_DIGITAL_CHANNELS_SQL = (
    "'MOBILE BANKING CHANNEL','INTERNET','ON-LINE CHANNEL',"
    "'KOCELA - SUBSCRIBER AND PAYMENT CHANNEL'"
)

# --- Whizz (KOCELA mobile-money) -----------------------------------------------
# Whizz activity is captured on the customer's bank account under the KOCELA channel
# (keyed by CBS customer_id, so no fragile id bridge). The Whizz *profile* lives in
# customers_whizz, joined via phone number to dim_customer.
_KOCELA_SQL = "UPPER(TRIM(channel_description)) = 'KOCELA - SUBSCRIBER AND PAYMENT CHANNEL'"
# Matched by substring, first hit wins — so 'PAY BILL' (which also contains
# 'ACCOUNT TO MPESA') must precede the bare 'ACCOUNT TO MPESA' send rule.
_WHIZZ_CATEGORY = {
    'PAY BILL': 'Pay Bill',
    'BUY GOOD': 'Buy Goods',
    'AIRTIME': 'Airtime',
    'CR FROM MOBILE BANKING': 'M-Pesa to account',
    'ACCOUNT TO MPESA': 'Send to M-Pesa',
}



class LiveDataNotReady(RuntimeError):
    """Raised when a live query depends on a source/view not yet built."""


class TrinoWarehouse(WarehouseGateway):
    # The as-of snapshot is refreshed once the memo is older than this. The book only
    # changes daily, so this is not about freshness — it is a self-heal ceiling so a
    # snapshot that somehow slips past the degraded-read guard can never freeze for the
    # whole process life; it re-reads within the window instead.
    _WB_TTL_SECONDS = 1800  # 30 min

    def __init__(self, trino: TrinoConnector, postgres: TrinoConnector | None = None):
        self._t = trino
        self._pg = postgres
        # Resolved current-RM allocation schema (table + column names), introspected
        # once from the curated Postgres and memoised. None = not yet probed; the
        # sentinel _ALLOC_NONE = probed and unavailable (cached on a TTL so a down PG
        # is retried, not hammered). See _alloc_schema / get_current_rm.
        # client_no -> insurance-register name, filled as clients resolve. Receipts
        # are keyed by name rather than client number, so this is the hop between them.
        self._client_names = {}
        self._alloc_meta: dict[str, str] | None = None
        self._alloc_meta_at: float = 0.0
        self._as_of: date | None = None
        self._as_of_at: float = 0.0
        # Cached "does this source table hold ANY rows" probes (table -> (present, at)).
        # Lets a domain tell "this customer has none" apart from "the whole source is
        # empty/unreachable" without a COUNT on every request.
        self._src_probe: dict[str, tuple[bool, float]] = {}
        # As-of whole-book aggregates are period-independent and expensive, so they
        # are memoised (see _WB_TTL_SECONDS). Stored with the wall-clock time it was
        # built so the memo can expire.
        self._wb_asof: dict[str, Any] | None = None
        self._wb_asof_at: float = 0.0
        self._benchmarks: dict[str, float] | None = None
        self._internal_ids_cache: list[int] | None = None

    # --- anchors ---------------------------------------------------------
    # The as-of anchor pins EVERY figure in the app to the warehouse's most recent
    # AVAILABLE snapshot date. That is NOT simply bank_parameters.prev_trx_date: the
    # eom_deposits / eom_loans fact tables are loaded daily but lag the business date
    # (deposits by up to a day; both skip weekends/holidays). Pinning to the raw
    # business date silently returns 0 whenever that exact day has not posted yet — a
    # deposit-only customer then reads 'KES 0' while the balance is one day back. So we
    # resolve to the newest date present in BOTH fact tables, on or before the business
    # date, giving a single consistent non-empty snapshot. The gateway is one lru_cached
    # instance per worker, so this MUST expire: without a TTL a worker freezes on
    # whatever date it booted on and silently drifts stale as the warehouse rolls
    # forward daily. Re-read on the same 30-min cadence as the whole-book memo.
    _ASOF_TTL_SECONDS = 1800

    def as_of_date(self) -> date:
        now = time.monotonic()
        if self._as_of is not None and (now - self._as_of_at) < self._ASOF_TTL_SECONDS:
            return self._as_of
        resolved = self._resolve_as_of()
        if resolved is not None:
            self._as_of = resolved
            self._as_of_at = now
        elif self._as_of is None:
            # Nothing readable and no prior value → last resort. (A transient empty read
            # never clobbers a good cached date back to today().)
            self._as_of = date.today()
            self._as_of_at = now
        return self._as_of

    def _resolve_as_of(self) -> date | None:
        """The latest snapshot date the value tables actually hold, on or before the
        bank's business date. Deposits and loans can each lag the business date and skip
        non-working days, so we take the newest ``eom_date`` present in BOTH tables — the
        two figures are then a single, consistent, non-empty snapshot. The probe is
        bounded to a short recent window and partition-pruned so it stays cheap on the
        2.37B-row fact tables. Returns None only when nothing at all is readable (so the
        caller keeps any previously cached date rather than snapping to today())."""
        rows = self._t.execute(
            "SELECT prev_trx_date AS d FROM delta.gold_db.bank_parameters LIMIT 1")
        biz = rows[0]['d'] if rows and rows[0].get('d') else None
        if biz is None:
            return None
        lo = biz - timedelta(days=10)   # covers a long weekend / public-holiday gap
        months = ','.join(str(m) for m in self._months_between(lo, biz))

        def _latest(table: str) -> date | None:
            r = self._t.execute(
                f"SELECT max(eom_date) AS d FROM delta.gold_db.{table} "
                f"WHERE eom_date <= DATE '{biz.isoformat()}' AND eom_date >= DATE '{lo.isoformat()}' "
                f"AND (partition_year * 100 + partition_month) IN ({months})")
            return r[0]['d'] if r and r[0].get('d') else None

        dep, loan = _latest('eom_deposits'), _latest('eom_loans')
        if dep and loan:
            return min(dep, loan)          # newest date both tables share
        # One side unreadable in the window → use whichever we have, else the biz date.
        return dep or loan or biz

    def _as_of_lit(self) -> str:
        # Our own validated date value → safe to inline (enables partition pruning).
        return f"DATE '{self.as_of_date().isoformat()}'"

    _SRC_PROBE_TTL_SECONDS = 900  # 15 min

    def _source_has_rows(self, table: str) -> bool:
        """True iff ``table`` currently holds at least one row and is reachable.
        Cheap (LIMIT 1) and memoised on a short TTL. A domain uses this to distinguish
        'this customer genuinely has none' (return None) from 'the source is empty or
        unreachable' (raise LiveDataNotReady → the UI shows an honest 'couldn't load'
        state instead of a false 'nothing linked')."""
        now = time.monotonic()
        hit = self._src_probe.get(table)
        if hit is not None and (now - hit[1]) < self._SRC_PROBE_TTL_SECONDS:
            return hit[0]
        try:
            present = bool(self._t.execute(f"SELECT 1 FROM {table} LIMIT 1"))
        except Exception:
            present = False  # unreachable / missing table == not available
        self._src_probe[table] = (present, now)
        return present

    # --- data-health report (admin panel) --------------------------------
    # Curated list of the tables the app actually depends on. Huge fact tables use a
    # cheap presence check (never COUNT a 2.37B-row table); the small report/domain
    # tables carry an exact count so an emptied source is obvious at a glance.
    _HEALTH_CHECKS = [
        ('dim_customer', 'Identity master', 'Core banking', 'delta.gold_db.dim_customer', 'count'),
        ('eom_deposits', 'Deposit snapshots', 'Core banking', 'delta.gold_db.eom_deposits', 'presence'),
        ('eom_loans', 'Loan snapshots', 'Core banking', 'delta.gold_db.eom_loans', 'presence'),
        ('fact_dep_trx', 'Transactions', 'Core banking', 'delta.gold_db.fact_dep_trx_recording', 'presence'),
        ('customers_whizz', 'Whizz (digital)', 'Domains', 'delta.gold_db.customers_whizz', 'count'),
        ('rpt_property', 'Properties source', 'Domains', 'delta.gold_db.rpt_c360_customer_property', 'count'),
        ('hfdi_mortgage', 'Mortgage flags', 'Domains', 'delta.gold_db.hfdi_mortgage_data', 'count'),
        ('rpt_policies', 'Policy summary', 'Insurance', 'delta.gold_db.rpt_c360_customer_policies_summary', 'count'),
        # Added after the insurance work: the app now depends on all of these, and a
        # health page that lists eight tables while the app reads eighteen tells ops
        # the warehouse is fine when half of what a page needs has gone.
        ('hfbi_customers', 'Insurance client register', 'Insurance', 'delta.gold_db.hfbi_customer_data', 'count'),
        ('hfbi_receipts', 'Premium receipts', 'Insurance', 'delta.gold_db.hfbi_receipt_data', 'count'),
        ('hfbi_policies', 'Policy master', 'Insurance', 'delta.gold_db.hfbi_policy_data', 'count'),
        ('hfbi_claims', 'Insurance claims', 'Insurance', 'delta.gold_db.hfbi_claim_data', 'count'),
        ('hfdi_clients', 'Property client register', 'Domains', 'delta.gold_db.hfdi_client_data', 'count'),
        ('npl_accounts', 'NPL classifications', 'Credit', 'delta.gold_db.npl_accounts', 'count'),
        ('pre_npl_accounts', 'Watch list', 'Credit', 'delta.gold_db.pre_npl_accounts', 'count'),
    ]

    # The curated reporting Postgres, checked separately because a failure there is a
    # different outage from a warehouse failure: the book page loses its allocation
    # and its balances while every Trino-backed page carries on working.
    _PG_HEALTH_CHECKS = [
        ('allocation_base', 'Allocation upload', 'Reporting Postgres', 'customer_allocation_base'),
        ('retail_allocation', 'RM allocation', 'Reporting Postgres', 'retail_allocated_portfolio'),
        ('dep_movement', 'Deposit balances', 'Reporting Postgres', 'daily_balance_movement'),
        ('loan_movement', 'Loan balances', 'Reporting Postgres', 'loan_daily_balance_movement'),
        ('relationship', 'Related-party register', 'Reporting Postgres', 'public.relationship'),
    ]

    def _run_pg_health_check(self, key, label, group, table):
        """Row count for one reporting-Postgres table.

        Reported as its own group rather than mixed in with the warehouse, because
        'Postgres is down' and 'the warehouse is down' break different pages and an
        ops person needs to tell them apart at a glance.
        """
        base = {'key': key, 'label': label, 'group': group, 'table': table.split('.')[-1]}
        if self._pg is None:
            return {**base, 'status': 'error', 'value': None, 'latency_ms': 0,
                    'detail': 'reporting Postgres is not configured'}
        t0 = time.monotonic()
        try:
            rows = self._pg.execute(f'SELECT COUNT(*) AS n FROM {table}')
            n = int(rows[0]['n']) if rows else 0
            return {**base, 'status': 'ok' if n > 0 else 'empty', 'value': n,
                    'latency_ms': round((time.monotonic() - t0) * 1000),
                    'detail': f'{n:,} rows' if n > 0 else 'table is empty - source data not loaded'}
        except Exception as e:
            return {**base, 'status': 'error', 'value': None,
                    'latency_ms': round((time.monotonic() - t0) * 1000),
                    'detail': f'{type(e).__name__}: {str(e)[:140]}'}

    def _run_health_check(self, key, label, group, table, mode):
        base = {'key': key, 'label': label, 'group': group, 'table': table.split('.')[-1]}
        t0 = time.monotonic()
        try:
            if mode == 'count':
                rows = self._t.execute(f"SELECT COUNT(*) n FROM {table}")
                n = int(rows[0]['n']) if rows else 0
                out = {**base, 'status': 'ok' if n > 0 else 'empty', 'value': n,
                       'detail': f'{n:,} rows' if n > 0 else 'table is empty — source data not loaded'}
            else:
                present = bool(self._t.execute(f"SELECT 1 FROM {table} LIMIT 1"))
                out = {**base, 'status': 'ok' if present else 'empty', 'value': 1 if present else 0,
                       'detail': 'reachable, has rows' if present else 'empty / no rows'}
            out['latency_ms'] = round((time.monotonic() - t0) * 1000)
            return out
        except Exception as e:
            return {**base, 'status': 'error', 'value': None,
                    'latency_ms': round((time.monotonic() - t0) * 1000),
                    'detail': f'{type(e).__name__}: {str(e)[:140]}'}

    def _check_connection(self) -> dict:
        """Warehouse reachability + authentication. This is checked FIRST, because when
        the connection or credentials fail every table check below would just repeat the
        same error — so we surface one clear row (and name an auth failure explicitly,
        the exact condition that took the app down when the Trino password expired)."""
        base = {'key': 'warehouse_conn', 'label': 'Warehouse connection', 'group': 'System', 'table': 'trino'}
        t0 = time.monotonic()
        try:
            self._t.execute('SELECT 1')
            return {**base, 'status': 'ok', 'value': 1, 'detail': 'connected and authenticated',
                    'latency_ms': round((time.monotonic() - t0) * 1000)}
        except Exception as e:
            msg = str(e)
            auth = '401' in msg or 'access denied' in msg.lower() or 'credential' in msg.lower()
            detail = ('authentication rejected — check the Trino service-account password (TRINO_USER / TRINO_PASSWORD)'
                      if auth else f'{type(e).__name__}: {msg[:140]}')
            return {**base, 'status': 'error', 'value': 0, 'detail': detail,
                    'latency_ms': round((time.monotonic() - t0) * 1000)}

    def _check_model(self) -> dict:
        """Recommendation-model health from the on-disk manifest (no warehouse needed):
        is a model trained, how many products, its mean quality, how many are calibrated,
        and how old it is — so silent model rot shows on the same page as data outages."""
        base = {'key': 'reco_model', 'label': 'Recommendation model', 'group': 'System', 'table': 'ml/models'}
        try:
            import json
            import os
            from pathlib import Path
            # Read the manifest path directly (mirrors ml.train.models_dir) so the check
            # never imports the LightGBM training stack just to read a JSON file.
            mdir = Path(os.environ.get('C360_ML_MODELS_DIR') or (Path(__file__).resolve().parents[2] / 'ml' / 'models'))
            mpath = mdir / 'manifest.json'
            if not mpath.exists():
                return {**base, 'status': 'empty', 'value': 0,
                        'detail': 'no model trained — recommendations use the rule engine'}
            manifest = json.loads(mpath.read_text())
            trained = {k: v for k, v in (manifest.get('products') or {}).items() if v.get('trained')}
            n = len(trained)
            precs = [v['precision_at_10pct'] for v in trained.values() if v.get('precision_at_10pct') is not None]
            mean_prec = round(sum(precs) / len(precs), 3) if precs else None
            calibrated = sum(1 for v in trained.values() if v.get('calibration'))
            age_days = round((time.time() - mpath.stat().st_mtime) / 86400)
            detail = (f'{n} products · mean top-decile precision {mean_prec} · '
                      f'{calibrated}/{n} calibrated · trained {age_days}d ago')
            status = 'ok'
            if n == 0:
                status, detail = 'empty', 'manifest present but no product model trained'
            elif age_days > 45:
                status, detail = 'stale', f'{detail} — retrain recommended'
            return {**base, 'status': status, 'value': n, 'detail': detail}
        except Exception as e:
            return {**base, 'status': 'error', 'value': None, 'detail': f'{type(e).__name__}: {str(e)[:140]}'}

    def _check_rm_allocation(self) -> dict:
        """Health of the curated Postgres (retail_allocated_portfolio) that supplies each
        customer's CURRENT RM. If it's down, the app silently falls back to the frozen
        onboarding officer — the 'shows an RM no longer managing them' report — so make
        that visible rather than silent."""
        base = {'key': 'rm_alloc', 'label': 'RM allocation (Postgres)', 'group': 'System',
                'table': 'retail_allocated_portfolio'}
        if self._pg is None:
            return {**base, 'status': 'empty', 'value': 0,
                    'detail': 'curated Postgres not configured — RMs show the onboarding officer'}
        t0 = time.monotonic()
        try:
            meta = self._alloc_schema()
            if not meta:
                return {**base, 'status': 'empty', 'value': 0,
                        'latency_ms': round((time.monotonic() - t0) * 1000),
                        'detail': 'no allocation table/columns resolved (Postgres unreachable or table absent)'}
            rows = self._pg.execute(f"SELECT COUNT(*) AS n FROM {meta['table']}")
            n = int(rows[0]['n']) if rows else 0
            base = {**base, 'table': meta['table'].split('.')[-1]}
            return {**base, 'status': 'ok' if n > 0 else 'empty', 'value': n,
                    'latency_ms': round((time.monotonic() - t0) * 1000),
                    'detail': f'{n:,} current allocations' if n > 0 else 'allocation table is empty'}
        except Exception as e:
            return {**base, 'status': 'error', 'value': None,
                    'latency_ms': round((time.monotonic() - t0) * 1000),
                    'detail': f'{type(e).__name__}: {str(e)[:140]}'}

    @staticmethod
    def _skipped_check(key, label, group, table, mode) -> dict:
        return {'key': key, 'label': label, 'group': group, 'table': table.split('.')[-1],
                'status': 'unknown', 'value': None, 'detail': 'not checked — warehouse connection is down'}

    def health_report(self):
        # System group first: connection/auth, model, RM-allocation source. The model
        # check needs no warehouse, so it reports even during an outage.
        conn = self._check_connection()
        system = [conn, self._check_model(), self._check_rm_allocation()]

        if conn['status'] == 'error':
            # Can't reach/authenticate the warehouse — running the 8 table checks would
            # just repeat this error, so mark them not-checked and point at the real cause.
            # Postgres is a separate estate: the warehouse being unreachable says
            # nothing about it, so those checks still run and still report.
            checks = (system + [self._skipped_check(*spec) for spec in self._HEALTH_CHECKS]
                      + [self._run_pg_health_check(*spec) for spec in self._PG_HEALTH_CHECKS])
            freshness = {'as_of': None, 'days_behind': None, 'status': 'error',
                         'detail': 'warehouse unreachable — see System · Warehouse connection'}
            return {'data_mode': 'live', 'freshness': freshness, 'checks': checks}

        checks = (system + [self._run_health_check(*spec) for spec in self._HEALTH_CHECKS]
                  + [self._run_pg_health_check(*spec) for spec in self._PG_HEALTH_CHECKS])
        try:
            asof = self.as_of_date()
            days = (date.today() - asof).days
            # ONE threshold, shared with the alert emails (c360.reports.alerts).
            # These were separately hard-coded and disagreed: the page called four
            # days healthy while the alert fired at three, so the dashboard showed
            # green at the same moment the mail said the data was stale.
            from django.conf import settings
            limit = int(getattr(settings, 'C360_ALERT_DATA_STALE_DAYS', 3))
            freshness = {'as_of': asof.isoformat(), 'days_behind': days,
                         'stale_after_days': limit,
                         'status': 'ok' if days <= limit else 'stale'}
        except Exception as e:
            freshness = {'as_of': None, 'days_behind': None, 'status': 'error',
                         'detail': f'{type(e).__name__}: {str(e)[:140]}'}
        return {'data_mode': 'live', 'freshness': freshness, 'checks': checks}

    @staticmethod
    def _date_lit(d: date) -> str:
        # Period bounds come from our own resolver, never user text → safe to inline.
        return f"DATE '{d.isoformat()}'"

    # eom_deposits (2.37B rows) / eom_loans are partitioned by (partition_year,
    # partition_month). Filtering eom_date alone does NOT prune — adding the
    # partition columns cuts a single-customer scan from ~11s to ~0.6s. Every EOM
    # query below carries one of these predicates.
    def _asof_part(self) -> str:
        d = self.as_of_date()
        return f" AND partition_year = {d.year} AND partition_month = {d.month} "

    @staticmethod
    def _months_between(start: date, end: date) -> list[int]:
        y, m, out = start.year, start.month, []
        while (y, m) <= (end.year, end.month):
            out.append(y * 100 + m)
            m, y = (1, y + 1) if m == 12 else (m + 1, y)
        return out

    def _range_part(self, start: date, end: date) -> str:
        months = self._months_between(start, end) or [self.as_of_date().year * 100 + self.as_of_date().month]
        return f" AND (partition_year * 100 + partition_month) IN ({','.join(str(x) for x in months)}) "

    def _recent_window(self) -> tuple[date, date, str]:
        """The last ~2 months up to as-of, for the digital-activity 'mobile' flag."""
        asof = self.as_of_date()
        py, pm = (asof.year - 1, 12) if asof.month == 1 else (asof.year, asof.month - 1)
        start = date(py, pm, 1)
        return start, asof, self._range_part(start, asof)

    def _lookback_window(self, months: int = 24, floor: date | None = None) -> tuple[date, date, str]:
        """A wide lookback up to as-of for the 'recent transactions' feed, so a
        customer's latest activity always loads even if it was months ago. If
        ``floor`` (e.g. the selected period start) is earlier, it widens further."""
        asof = self.as_of_date()
        y, m = asof.year, asof.month
        total = (y * 12 + (m - 1)) - months
        start = date(total // 12, total % 12 + 1, 1)
        if floor and floor < start:
            start = date(floor.year, floor.month, 1)
        return start, asof, self._range_part(start, asof)

    @staticmethod
    def _channel_label(raw: Any) -> str | None:
        if not raw:
            return None
        key = str(raw).strip().upper()
        if key in ('BATCH', 'SAP FINANCE GL', 'WSO2 - ENTERPRISE SERVICE BUS',
                   'ESB - ENTERPRISE SERVICE BUS', 'DEFAULT', ''):
            return None
        return _CHANNEL_DISPLAY.get(key, str(raw).strip().title())

    @staticmethod
    def _cid(cust_id: str) -> int | None:
        try:
            return int(str(cust_id).split('.')[0])
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _clean(value: Any) -> Any:
        """Trim CHAR-padded warehouse strings; keep non-strings as-is, '' → None."""
        if isinstance(value, str):
            v = value.strip()
            return v or None
        return value

    # System / migration "users" that populate created_emp_name for bulk-loaded or
    # channel-onboarded records — these are NOT relationship officers, so they must
    # not be shown as an RM (KOCELA alone accounts for ~900k records).
    _NON_OFFICER_TOKENS = ('KOCELA', 'SYSTEM', 'MIG_CIS', 'MIGRATION', 'DEFAULT', 'BATCH', 'ATM ')

    @classmethod
    def _officer_name(cls, raw: Any) -> str | None:
        """Resolve a real servicing-officer name from created_emp_name, filtering the
        system/migration users. Returns None when the record was created by a system
        user, so the header honestly shows the RM as unsourced rather than 'KOCELA USER'."""
        v = cls._clean(raw)
        if not v:
            return None
        u = v.upper()
        if any(tok in u for tok in cls._NON_OFFICER_TOKENS):
            return None
        # Collapse the double spaces the warehouse leaves between name parts.
        return ' '.join(v.split())

    @classmethod
    def _officer_id(cls, raw: Any) -> str | None:
        """The officer/staff id (created_emp_id), suppressed for system users."""
        v = cls._clean(raw)
        if not v:
            return None
        if any(tok in v.upper() for tok in cls._NON_OFFICER_TOKENS):
            return None
        return v

    # --- current RM allocation (curated Postgres) ------------------------
    # dim_customer only carries the ACCOUNT-OPENING officer (created_emp_name), frozen
    # at onboarding — so it is wrong once a customer is reassigned to a new RM (the
    # "shows an RM no longer managing them" report). The live current RM lives in the
    # curated Postgres (retail_allocated_portfolio), which core banking / Trino cannot
    # see. We introspect that table's columns at runtime — their exact names are not
    # fixed across environments and this box can't reach the DB to check — memoise the
    # resolution, and let PG_ALLOC_* env vars pin them if auto-detection guesses wrong.
    # ANY failure (no Postgres, unreachable, table/column not found, id mismatch) →
    # callers simply keep the onboarding officer, so this never breaks the page.
    # Column priorities lead with the CONFIRMED HF names (retail_allocated_portfolio:
    # cust_id / rm_name / sales_code — verified against the portfolio backend's queries),
    # then generic fallbacks for other environments. PG_ALLOC_* env vars still override.
    _ALLOC_TTL_SECONDS = 900
    _ALLOC_CUST_COLS = ('cust_id', 'customer_id', 'cust_cif', 'customer_no', 'customer_number',
                        'cif', 'cif_no', 'cif_number', 'client_id', 'customer_cif', 'cust_no')
    _ALLOC_CODE_COLS = ('sales_code', 'salescode', 'sales_cd', 'sc_code', 'rm_code', 'officer_code')
    _ALLOC_NAME_COLS = ('rm_name', 'relationship_manager_name', 'relationship_manager',
                        'officer_name', 'account_officer_name', 'account_officer', 'manager_name',
                        'staff_name', 'rm_full_name', 'rm')
    _ALLOC_DATE_COLS = ('allocated_date', 'allocation_date', 'alloc_date', 'as_of_date',
                        'snapshot_date', 'effective_date', 'updated_at', 'modified_date', 'date')

    @staticmethod
    def _alloc_env(key: str) -> str:
        import os
        return (os.environ.get(key) or '').strip()

    def _alloc_schema(self) -> dict[str, str] | None:
        """Resolve + memoise the allocation table and its columns from the curated
        Postgres. Returns {table, cust_col, name_col, code_col, date_col} or None when
        Postgres is absent/unreachable or no suitable table is found. Negative results
        are cached on a TTL so a down PG is retried, not hammered every request."""
        if self._pg is None:
            return None
        now = time.monotonic()
        if self._alloc_meta is not None and (now - self._alloc_meta_at) < self._ALLOC_TTL_SECONDS:
            return self._alloc_meta or None    # {} sentinel => probed, unavailable
        try:
            meta = self._probe_alloc_schema()
        except Exception:
            meta = None
        self._alloc_meta = meta or {}          # cache the negative result too (TTL-bounded)
        self._alloc_meta_at = now
        return meta

    def _probe_alloc_schema(self) -> dict[str, str] | None:
        table = self._alloc_env('PG_ALLOC_TABLE')   # explicit pin wins
        if not table:
            rows = self._pg.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_name ILIKE %s "
                "ORDER BY (table_name ILIKE %s) DESC, table_name LIMIT 1",
                ('%allocat%', '%retail%allocat%'))
            if not rows:
                return None
            table = f"{rows[0]['table_schema']}.{rows[0]['table_name']}"
        tbl_only = table.split('.')[-1]
        cols = [str(r['column_name']).lower() for r in self._pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (tbl_only,))]
        if not cols:
            return None

        def pick(env_key: str, priority: tuple[str, ...]) -> str:
            pinned = self._alloc_env(env_key).lower()
            if pinned and pinned in cols:
                return pinned
            for cand in priority:                    # exact column name first
                if cand in cols:
                    return cand
            for cand in priority:                    # then a substring match
                for c in cols:
                    if cand in c:
                        return c
            return ''

        cust = pick('PG_ALLOC_CUST_COL', self._ALLOC_CUST_COLS)
        name = pick('PG_ALLOC_NAME_COL', self._ALLOC_NAME_COLS)
        code = pick('PG_ALLOC_CODE_COL', self._ALLOC_CODE_COLS)
        dcol = pick('PG_ALLOC_DATE_COL', self._ALLOC_DATE_COLS)
        if not cust or not (name or code):
            return None                              # can't map customer → RM; give up
        return {'table': table, 'cust_col': cust, 'name_col': name, 'code_col': code, 'date_col': dcol}

    def get_current_rm(self, cust_ids) -> dict[str, dict[str, Any]]:
        """Map each customer id → their CURRENT RM ``{'name','code'}`` from the curated
        Postgres allocation. Returns ``{}`` (callers then keep the onboarding officer)
        when the allocation is unavailable or none of the ids are allocated. Batched and
        never raises — a failure degrades to the onboarding officer, never an error."""
        ids = [str(c) for c in cust_ids if c is not None and str(c).strip()]
        if not ids:
            return {}
        meta = self._alloc_schema()
        if not meta:
            return {}
        try:
            cust, name, code, dcol = (meta['cust_col'], meta['name_col'],
                                      meta['code_col'], meta['date_col'])
            select = [f"TRIM(CAST({cust} AS text)) AS cid",
                      (f"{name} AS rm_name" if name else "NULL AS rm_name"),
                      (f"{code} AS sales_code" if code else "NULL AS sales_code")]
            order = f" ORDER BY {dcol} DESC NULLS LAST" if dcol else ""
            placeholders = ','.join(['%s'] * len(ids))
            rows = self._pg.execute(
                f"SELECT {', '.join(select)} FROM {meta['table']} "
                f"WHERE TRIM(CAST({cust} AS text)) IN ({placeholders}){order}", tuple(ids))
        except Exception:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            cid = str(r.get('cid') or '').strip()
            if not cid or cid in out:                # first row per customer wins (latest, if ordered)
                continue
            nm, cd = self._clean(r.get('rm_name')), self._clean(r.get('sales_code'))
            if nm or cd:
                out[cid] = {'name': nm, 'code': cd}
        return out

    def get_profitability(self, cust_id) -> dict[str, Any] | None:
        """Per-customer AUM + net contribution + NPL flag from the reporting Postgres
        ``customer_allocation_base`` (100% populated). Returns None when Postgres is
        absent/unreachable or the customer isn't in the allocation base. Never raises —
        callers then badge these figures 'not sourced' rather than showing a fake."""
        if self._pg is None:
            return None
        cid = str(cust_id).strip()
        if not cid:
            return None
        try:
            rows = self._pg.execute(
                "SELECT aum_cust_id, net_after_expense, npl, main_segment, "
                "rm_name_prev, rm_code_prev FROM customer_allocation_base "
                "WHERE TRIM(CAST(cust_id AS text)) = %s LIMIT 1", (cid,))
        except Exception:
            return None
        if not rows:
            return None
        r = rows[0]

        def _num(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        return {
            'aum': _num(r.get('aum_cust_id')),
            'contribution': _num(r.get('net_after_expense')),   # net after expense = profitability
            'npl': bool(_num(r.get('npl')) or 0),
            'main_segment': self._clean(r.get('main_segment')),
            'prev_rm': self._clean(r.get('rm_name_prev')),      # previous RM (reassignment signal)
        }

    def get_book_summary(self, sales_code: str | None) -> dict[str, Any] | None:
        """An RM's book (or the whole book when ``sales_code`` is None) rolled up from the
        reporting Postgres ``customer_allocation_base``: headline totals, a segment
        breakdown and the top customers by AUM. Returns None when Postgres is absent/
        unreachable (the caller then shows an honest 'not available' state). Never raises."""
        if self._pg is None:
            return None
        where, params = '', ()
        if sales_code:
            where, params = 'WHERE rm_code = %s', (str(sales_code),)
        try:
            head = self._pg.execute(
                f"""SELECT COUNT(*) AS customers,
                           COALESCE(SUM(aum_cust_id), 0)       AS aum,
                           COALESCE(SUM(deposit), 0)           AS deposits,
                           COALESCE(SUM(loans), 0)             AS loans,
                           COALESCE(SUM(net_after_expense), 0) AS contribution,
                           COALESCE(SUM(CASE WHEN npl <> 0 THEN 1 ELSE 0 END), 0) AS npl_customers,
                           COALESCE(SUM(CASE WHEN npl <> 0 THEN aum_cust_id ELSE 0 END), 0) AS npl_aum
                    FROM customer_allocation_base {where}""", params)
            segs = self._pg.execute(
                f"""SELECT main_segment AS segment, COUNT(*) AS n,
                           COALESCE(SUM(aum_cust_id), 0) AS aum
                    FROM customer_allocation_base {where}
                    GROUP BY main_segment ORDER BY aum DESC""", params)
            top = self._pg.execute(
                f"""SELECT CAST(cust_id AS text) AS cust_id, customer_name, main_segment AS segment,
                           aum_cust_id AS aum, net_after_expense AS contribution, npl
                    FROM customer_allocation_base {where}
                    ORDER BY aum_cust_id DESC NULLS LAST LIMIT 10""", params)
            # Every customer id in the book, so the headline NPL count can be checked
            # against the live book rather than left disagreeing with the list below
            # it. Bounded on purpose - see _BOOK_VERIFY_LIMIT.
            members = self._pg.execute(
                f"""SELECT CAST(cust_id AS text) AS cust_id, aum_cust_id AS aum, npl
                    FROM customer_allocation_base {where}
                    LIMIT {self._BOOK_VERIFY_LIMIT + 1}""", params)
        except Exception:
            return None
        if not head:
            return None
        h = head[0]

        def _n(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        npl_live = self._verify_book_npl(members)
        live_totals = self._live_book_totals(members)
        # The RM Portfolio tool's own definition of the same three figures, so the
        # two screens can be reconciled instead of merely differing.
        portfolio_view = self.get_rm_balances(sales_code)
        portfolio_customers = self.get_rm_book_size(sales_code)
        return {
            'sales_code': sales_code,
            'whole_book': sales_code is None,
            'customers': int(h.get('customers') or 0),
            'aum': round(_n(h.get('aum'))),
            'deposits': round(_n(h.get('deposits'))),
            'loans': round(_n(h.get('loans'))),
            'contribution': round(_n(h.get('contribution'))),
            'npl_customers': (npl_live['customers'] if npl_live
                              else int(h.get('npl_customers') or 0)),
            'npl_aum': (npl_live['aum'] if npl_live else round(_n(h.get('npl_aum')))),
            # Where the two NPL figures came from, so the page never shows a
            # live-checked list under a snapshot-derived headline without saying so.
            'npl_source': 'live' if npl_live else 'snapshot',
            'npl_snapshot_customers': int(h.get('npl_customers') or 0),
            'npl_snapshot_aum': round(_n(h.get('npl_aum'))),
            # The same two figures read from the live book, so the page can show why
            # it differs from a tool that reads live instead of hiding the gap. None
            # when the book is too large to total on a page load, or Trino is down.
            'live': live_totals,
            # What the RM Portfolio tool would show for this same book. Present so a
            # relationship manager comparing the two screens sees one reconciliation
            # rather than two numbers and no explanation.
            'portfolio_view': (
                {**portfolio_view, 'customers': portfolio_customers}
                if portfolio_view else
                ({'customers': portfolio_customers} if portfolio_customers else None)),
            # customer_allocation_base records no load date (all 47 columns checked),
            # so the page must not imply it knows how current these figures are.
            'snapshot_dated': False,
            'segments': [{'segment': self._clean(s.get('segment')) or 'Unsegmented',
                          'customers': int(s.get('n') or 0), 'aum': round(_n(s.get('aum')))}
                         for s in segs],
            'top_customers': self._verify_against_live(
                [{'cust_id': str(t.get('cust_id') or '').strip(),
                  'name': self._clean(t.get('customer_name')),
                  'segment': self._clean(t.get('segment')),
                  'aum': round(_n(t.get('aum'))),
                  'contribution': round(_n(t.get('contribution'))),
                  'npl_snapshot': bool(_n(t.get('npl')) or 0)} for t in top]),
        }

    #: How many customers the book will live-check on a page load. An RM's book is a
    #: few hundred and one grouped query answers it; the whole-book view is the entire
    #: bank, so beyond this the upload's own NPL figure is kept and labelled as such
    #: rather than scanning the loan book synchronously.
    _BOOK_VERIFY_LIMIT = 800

    def _live_book_totals(self, members: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Deposits, loans and how many customers still hold anything, read live.

        Shown ALONGSIDE the upload's figures rather than replacing them. The upload
        owns AUM and net contribution - no other source has those columns - so
        swapping in live totals would leave a page mixing two vintages with no way to
        tell which was which. Two numbers with a stated difference is the honest
        version, and it is what lets an RM reconcile this page against the RM
        portfolio tool instead of filing a defect.
        """
        if not members or len(members) > self._BOOK_VERIFY_LIMIT:
            return None
        ids = [i for i in {self._cid(m.get('cust_id')) for m in members} if i is not None]
        if not ids:
            return None
        try:
            standing = self.live_loan_standing(ids)
            deposits = self.live_deposit_totals(ids)
        except Exception:
            logger.warning('book: live totals unavailable', exc_info=True)
            return None
        loans = sum(float(v.get('loans') or 0) for v in standing.values())
        dep = sum(deposits.values())
        # A customer counts as still on the book when they hold lending or carry any
        # deposit position, overdrawn included. Chandarana Supermarket, sitting at a
        # flat zero with no loans, is exactly who this leaves out.
        holding = {cid for cid in ids
                   if cid in standing or abs(deposits.get(cid, 0.0)) >= 1}
        return {
            'deposits': round(dep),
            'loans': round(loans),
            'customers': len(holding),
            'as_of': self.as_of_date().isoformat(),
        }

    def _pg_table_columns(self, table: str) -> set[str]:
        """The columns a reporting-Postgres table actually has, lower-cased.

        Asked of the catalogue rather than assumed: the balance-movement tables are
        NOT a uniform monthly series. 2026 is monthly, older periods are quarterly,
        so jun_25_bal exists and jul_25_bal does not. Naming a column that was never
        created fails the whole statement.
        """
        rows = self._pg.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,))
        return {str(r.get('column_name') or '').lower() for r in rows}

    def _movement_balance(self, table: str, sales_code: str) -> tuple[float, str | None]:
        """Latest trustworthy balance for an RM in one movement table."""
        columns = self._pg_table_columns(table)
        if not columns:
            return 0.0, None
        sql, _ = rm_bal.build_sql(table, columns,
                                  has_segment='customer_segment' in columns)
        if not sql:
            return 0.0, None
        params: list[Any] = [str(sales_code)]
        if 'customer_segment' in columns:
            params.append(list(rm_bal.EXCLUDED_SEGMENTS))
        rows = self._pg.execute(sql, tuple(params))
        if not rows:
            return 0.0, None
        return rm_bal.pick(dict(rows[0]), rm_bal.ladder(columns))

    def get_rm_balances(self, sales_code: str | None) -> dict[str, Any] | None:
        """An RM's deposit and loan position, defined as the RM Portfolio tool defines it.

        Customer 360 and that tool disagreed for the same relationship manager, and
        the RM was right to report it. The difference was definitional: balances key
        on the ACCOUNT's rm_code rather than on who the customer is allocated to, and
        they come from the movement tables rather than a customer-master aggregate on
        its own refresh cycle. See c360/warehouse/rm_balances.py.

        Returns None when Postgres is absent or the tables are not there, so the page
        keeps the allocation upload's figures and says they are unverified rather
        than reporting a zero position.
        """
        if self._pg is None or not sales_code:
            return None
        try:
            dep, dep_at = self._movement_balance('daily_balance_movement', sales_code)
            loan, loan_at = self._movement_balance('loan_daily_balance_movement', sales_code)
        except Exception:
            logger.warning('rm balances unavailable for %s', sales_code, exc_info=True)
            return None
        if dep_at is None and loan_at is None:
            return None
        return {
            'deposits': round(dep),
            'deposits_as_at': dep_at,
            'loans': round(loan),
            'loans_as_at': loan_at,
            'basis': 'Balance movement, by account RM code',
        }

    def get_rm_book_size(self, sales_code: str | None) -> int | None:
        """How many customers are allocated to this RM, counted as the RM Portfolio
        tool counts them: DISTINCT cust_id in retail_allocated_portfolio.

        Customer 360 counted customer_allocation_base instead, which is a different
        table with a different population - 191 against the portfolio tool's 220 for
        the same RM.
        """
        if self._pg is None or not sales_code:
            return None
        try:
            rows = self._pg.execute(
                "SELECT COUNT(DISTINCT cust_id) AS n FROM retail_allocated_portfolio "
                "WHERE TRIM(sales_code::text) = TRIM(%s) AND cust_id IS NOT NULL",
                (str(sales_code),))
        except Exception:
            logger.warning('rm book size unavailable for %s', sales_code, exc_info=True)
            return None
        return int(rows[0].get('n') or 0) if rows else None

    def _verify_book_npl(self, members: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Recount non-performing customers from the live loan book.

        Returns None when the check cannot or should not run (no member list, a book
        larger than the bound, or the warehouse unreachable), and the caller then
        keeps the upload's figure and marks it as the upload's.
        """
        if not members or len(members) > self._BOOK_VERIFY_LIMIT:
            return None
        by_id: dict[int, float] = {}
        for m in members:
            cid = self._cid(m.get('cust_id'))
            if cid is None:
                continue
            try:
                by_id[cid] = float(m.get('aum') or 0)
            except (TypeError, ValueError):
                by_id[cid] = 0.0
        if not by_id:
            return None
        try:
            standing = self.live_loan_standing(list(by_id))
        except Exception:
            logger.warning('book: live NPL recount failed', exc_info=True)
            return None
        npl_ids = [cid for cid, st in standing.items() if st['npl']]
        return {
            'customers': len(npl_ids),
            # AUM stays the upload's number, because only the upload has an AUM
            # column. What changes is WHICH customers it is summed over.
            'aum': round(sum(by_id.get(cid, 0.0) for cid in npl_ids)),
        }

    def live_loan_standing(self, cust_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Where each customer's lending actually stands at the latest close.

        eom_loans is the only delinquency source that moves daily; npl_accounts is a
        monthly file and was three months behind when this was written. Classification
        is taken worst-first across the customer's accounts, so one Overdue facility
        makes the customer Overdue even when the rest are Normal.

        Returns {cust_id: {'status', 'npl', 'loans', 'accounts'}} for customers that
        have at least one loan row; a customer absent from the result has no live
        lending, which is NOT the same as performing and is why the caller checks
        deposits too before calling anyone closed.
        """
        ids = [i for i in {self._cid(c) for c in cust_ids} if i is not None]
        if not ids:
            return {}
        inlist = ','.join(str(i) for i in ids)
        d, p = self._as_of_lit(), self._asof_part()
        rows = self._t.execute(
            f"SELECT cust_id, TRIM(loan_status_ind_name) status, count(*) accounts, "
            f"SUM(gross_total) gross FROM delta.gold_db.eom_loans "
            f"WHERE eom_date = {d} {p} AND cust_id IN ({inlist}) GROUP BY cust_id, 2")
        # Worst classification wins. 'Normal' is the only performing value in the
        # live vocabulary (the others are 'Overdue' and 'Write Off').
        rank = {'NORMAL': 0, 'OVERDUE': 1, 'WRITE OFF': 2}
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            cid = self._cid(r.get('cust_id'))
            if cid is None:
                continue
            status = self._clean(r.get('status')) or 'Unclassified'
            entry = out.setdefault(cid, {'status': status, 'npl': False, 'loans': 0.0,
                                         'accounts': 0})
            entry['loans'] += float(r.get('gross') or 0)
            entry['accounts'] += int(r.get('accounts') or 0)
            if rank.get(status.upper(), 0) >= rank.get(str(entry['status']).upper(), 0):
                entry['status'] = status
        for entry in out.values():
            entry['npl'] = str(entry['status']).upper() in ('OVERDUE', 'WRITE OFF')
            entry['loans'] = round(entry['loans'])
        return out

    def live_deposit_totals(self, cust_ids: list[int]) -> dict[int, float]:
        """Deposit balance per customer at the latest close, for customers that have one."""
        ids = [i for i in {self._cid(c) for c in cust_ids} if i is not None]
        if not ids:
            return {}
        inlist = ','.join(str(i) for i in ids)
        d, p = self._as_of_lit(), self._asof_part()
        rows = self._t.execute(
            f"SELECT cust_id, SUM(book_balance) bal, count(*) accounts "
            f"FROM delta.gold_db.eom_deposits WHERE eom_date = {d} {p} "
            f"AND cust_id IN ({inlist}) GROUP BY cust_id")
        return {self._cid(r['cust_id']): float(r.get('bal') or 0)
                for r in rows if self._cid(r.get('cust_id')) is not None}

    def _verify_against_live(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Reconcile snapshot rows against the live book, and say where they disagree.

        The snapshot is not overwritten quietly. Each row carries what the snapshot
        said, what the live book says, and a flag when the two differ - an RM who was
        shown a wrong badge yesterday needs to see that it was corrected, not just
        find a different answer today with no explanation.
        """
        if not rows:
            return rows
        ids = [r['cust_id'] for r in rows]
        try:
            standing = self.live_loan_standing(ids)
            deposits = self.live_deposit_totals(ids)
        except Exception:
            # Live check unavailable: fall back to the snapshot, but say so rather
            # than presenting an unverified flag as if it had been checked.
            logger.warning('book: live verification failed, falling back to snapshot',
                           exc_info=True)
            for r in rows:
                r['npl'] = r.pop('npl_snapshot', False)
                r['npl_source'] = 'snapshot'
                r['verified'] = False
            return rows

        out = []
        for r in rows:
            cid = self._cid(r['cust_id'])
            live = standing.get(cid)
            bal = deposits.get(cid)
            snap = r.pop('npl_snapshot', False)
            if live is not None:
                r['npl'] = live['npl']
                r['npl_source'] = 'live'
                r['live_status'] = live['status']
                r['live_loans'] = live['loans']
            else:
                # No live lending. That is not evidence of performing, so the badge
                # is dropped rather than flipped to a confident 'performing'.
                r['npl'] = False
                r['npl_source'] = 'no_live_lending'
                r['live_status'] = None
                r['live_loans'] = 0
            r['verified'] = True
            r['npl_was_snapshot'] = snap
            r['npl_corrected'] = bool(snap) != bool(r['npl'])
            r['live_deposits'] = round(bal) if bal is not None else None
            # Closed: nothing lent and no deposit balance left. Chandarana Supermarket
            # sat at the top of a book on KES 9.31M of snapshot AUM in exactly this
            # state, three months after the customer closed the account.
            r['closed'] = live is None and (bal is None or abs(bal) < 1)
            out.append(r)
        return out

    # 'INTERNAL ACCOUNTS' is the bank's own ledger/clearing/suspense estate (CBK
    # clearing, M-Pesa float, treasury margin, nostro) — NOT customers. They carry
    # huge volatile/negative balances that distort every portfolio aggregate, so they
    # are excluded from all book-level rollups and from customer discovery.
    _INTERNAL_SEGMENT = 'INTERNAL ACCOUNTS'

    def _internal_ids(self) -> list[int]:
        if self._internal_ids_cache is None:
            rows = self._t.execute(
                "SELECT CAST(customer_id AS BIGINT) id FROM delta.gold_db.dim_customer "
                "WHERE customer_segment = ?", (self._INTERNAL_SEGMENT,))
            self._internal_ids_cache = [int(r['id']) for r in rows if r.get('id') is not None]
        return self._internal_ids_cache

    def _not_internal_cid(self, col: str = 'cust_id') -> str:
        """SQL clause excluding internal accounts by id (for scans without a segment
        join). Empty when there are none."""
        ids = self._internal_ids()
        if not ids:
            return ''
        return f" AND {col} NOT IN ({','.join(str(i) for i in ids)}) "

    @staticmethod
    def _seg_label(raw: Any) -> str:
        """Display label for a segment; 'UNKNOWN'/blank → 'Unsegmented' (these are
        real customers with no segment assigned, not a distinct real segment)."""
        v = (str(raw).strip() if raw is not None else '')
        if not v or v.upper() in ('UNKNOWN', 'NULL', 'NONE'):
            return 'Unsegmented'
        return v

    @staticmethod
    def _safe_date(value: Any) -> str | None:
        """Sanitise a date coming back as a string. Production has placeholder
        dates like '0000-12-30' (year 0) that break Python date mapping, so date
        columns are selected as varchar and cleaned here."""
        if not value:
            return None
        s = str(value).strip()
        if not s or s.startswith('0000') or s[:4] in {'0001', '1900', '9999'}:
            return None
        return s[:10]

    # dim_customer.cust_type is a coded field: 1 = a natural person, 2 = a
    # registered organisation (companies, churches, co-operatives, self-help
    # groups — where the bank's non-profit customers sit), 3 = correspondent /
    # internal ledger accounts. Anything else is left unlabelled rather than guessed.
    _CUST_TYPE_LABELS = {'1': 'Individual', '2': 'Organisation', '3': 'Correspondent / internal'}

    @classmethod
    def _cust_type_label(cls, raw: Any) -> str | None:
        return cls._CUST_TYPE_LABELS.get(str(raw).strip().split('.')[0]) if raw is not None else None

    @classmethod
    def _is_individual(cls, raw: Any) -> bool:
        return str(raw).strip().split('.')[0] == '1' if raw is not None else False

    @classmethod
    def _id_type(cls, issue_authority: Any, cust_type: Any) -> str | None:
        """Classify the identification document from the (free-text, misspelt)
        issuing authority. Registrar of Companies → the certificate-of-incorporation /
        business-registration number; Immigration → passport/alien ID; otherwise the
        Kenyan national ID. Organisations with a blank authority still read as a
        registration number, individuals as a national ID."""
        ia = (str(issue_authority).strip().upper() if issue_authority is not None else '')
        if not ia:
            if cls._is_individual(cust_type):
                return 'National ID'
            return 'Registration no.' if cls._cust_type_label(cust_type) == 'Organisation' else None
        if 'COMPAN' in ia or 'BUSINESS' in ia or ('REGISTRAR' in ia and 'PERSON' not in ia and 'BIRTH' not in ia):
            return 'Company registration'
        if 'IMMIGRATION' in ia or 'PASSPORT' in ia or 'ALIEN' in ia:
            return 'Passport / alien ID'
        if 'BIRTH' in ia:
            return 'Birth certificate'
        if 'DEFENCE' in ia or 'DEFENSE' in ia or 'MILITARY' in ia:
            return 'Military ID'
        return 'National ID'

    @classmethod
    def _gender(cls, raw: Any) -> str | None:
        v = cls._clean(raw)
        if not v:
            return None
        head = str(v).strip().upper()[0]
        return {'M': 'Male', 'F': 'Female'}.get(head, str(v).strip().title())

    # --- identity (dim_customer only → guaranteed one row) ---------------
    def get_customer(self, cust_id: str) -> dict[str, Any] | None:
        # An the property register id resolves against the property register instead of the bank's
        # customer master. Routing it here rather than in the view means the customer
        # page, the scope check and the staff sieve all work on these records
        # unchanged - and every OTHER gateway method still parses the id through
        # _cid(), which rejects it, so no bank figure can ever be attached to a
        # client who has no bank relationship.
        client_id_int = prop_reg.parse_id(cust_id)
        if client_id_int is not None:
            return self.get_property_client(client_id_int)
        # Same argument for the insurance register: its own namespace, so no bank
        # query can resolve one of its ids and attach somebody else's balances.
        ins_key = ins_reg.parse_id(cust_id)
        if ins_key is not None:
            return self.get_insurance_client(ins_key)
        cid = self._cid(cust_id)
        if cid is None:
            return None
        rows = self._t.execute(
            """
            SELECT CAST(customer_id AS BIGINT) AS id, full_name, customer_segment,
                   account_branch_name, account_branch_number,
                   fk_bankemployeeid, created_emp_id, created_emp_name,
                   primary_mobile_no, mobile_tel2, telephone_1, e_mail, address,
                   customer_id_no, issue_authority, cust_type, kra_pin_status,
                   sex, city_of_birth, employer,
                   CAST(date_of_birth AS varchar) AS date_of_birth,
                   CAST(account_opening_date AS varchar) AS account_opening_date,
                   CAST(cust_open_date AS varchar) AS cust_open_date,
                   cust_status
            FROM delta.gold_db.dim_customer
            WHERE customer_id = ?
            """,
            (cid,),
        )
        if not rows:
            return None
        r = rows[0]
        opened = self._safe_date(r.get('account_opening_date')) or self._safe_date(r.get('cust_open_date'))
        status = (r.get('cust_status') or '').strip().upper()
        id_no = self._clean(r.get('customer_id_no'))
        cust_type = r.get('cust_type')
        # RM: prefer the CURRENT allocation (retail_allocated_portfolio, curated
        # Postgres); fall back to the account-opening officer when the allocation is
        # unavailable or this customer isn't allocated. rm_source lets the header label
        # which one it is, so a fallback name is never passed off as the current RM.
        onboard_name = self._officer_name(r.get('created_emp_name'))
        onboard_code = self._officer_id(r.get('created_emp_id'))
        alloc = self.get_current_rm([r['id']]).get(str(r['id']))
        if alloc and (alloc.get('name') or alloc.get('code')):
            rm_name = alloc.get('name') or onboard_name
            sales_code = alloc.get('code') or onboard_code
            rm_source = 'allocation'
        else:
            rm_name, sales_code, rm_source = onboard_name, onboard_code, 'onboarding'
        return {
            'cust_id': str(r['id']),
            'name': self._clean(r.get('full_name')),
            'segment': self._seg_label(r.get('customer_segment')),
            'branch': self._clean(r.get('account_branch_name')),
            'rm_name': rm_name,
            'sales_code': sales_code,
            # 'allocation' = current RM from retail_allocated_portfolio; 'onboarding' =
            # the account-opening officer fallback (labelled honestly in the header).
            'rm_source': rm_source,
            'mobile': self._clean(r.get('primary_mobile_no')),
            'email': self._clean(r.get('e_mail')),
            'id_no': id_no,
            'active': status not in {'C', 'CLOSED', 'I', 'INACTIVE', 'D'},
            # HF-staff confidentiality (admin-only). Any of the signals in rbac/staff.py
            # marks the record; the query layer 404s it for non-admins.
            'is_staff': is_staff_from_fields(
                employer=r.get('employer'), segment=r.get('customer_segment'),
                bank_employee_id=r.get('fk_bankemployeeid')),
            # Risk feed exists (aml_customer.risk_class) but is blank in production,
            # so the eligibility gate correctly stays pending — do not fake it.
            'risk_class': None,
            'crb_status': None,
            'kyc_status': None,
            # Now genuinely sourced from dim_customer.account_opening_date.
            'relationship_since': opened,
            # Bio & identification (backlog item #1). Only fields that genuinely
            # apply are populated; DOB/gender/birthplace are individual-only, so an
            # organisation legitimately carries none (an honest N/A, not a bare '--').
            'bio': {
                'customer_type': self._cust_type_label(cust_type),
                'id_type': self._id_type(r.get('issue_authority'), cust_type),
                'id_no': id_no,
                'issuing_authority': self._clean(r.get('issue_authority')),
                'kra_pin_status': self._clean(r.get('kra_pin_status')),
                'date_of_birth': self._safe_date(r.get('date_of_birth')) if self._is_individual(cust_type) else None,
                'gender': self._gender(r.get('sex')) if self._is_individual(cust_type) else None,
                'city_of_birth': self._clean(r.get('city_of_birth')) if self._is_individual(cust_type) else None,
                'employer': self._clean(r.get('employer')),
                'address': self._clean(r.get('address')),
                'alt_phone': self._clean(r.get('mobile_tel2')) or self._clean(r.get('telephone_1')),
                'branch': self._clean(r.get('account_branch_name')),
                'account_open_date': opened,
            },
        }

    # --- value (pre-aggregated → dedup-safe) -----------------------------
    def get_relationship_value(self, cust_id: str) -> dict[str, Any]:
        cid = self._cid(cust_id)
        if cid is None:
            return {'relationship_value': 0, 'deposits': 0, 'loans': 0, 'revenue': 0}
        d, p = self._as_of_lit(), self._asof_part()
        rows = self._t.execute(
            f"""
            SELECT
              (SELECT COALESCE(SUM(book_balance), 0) FROM delta.gold_db.eom_deposits
                 WHERE eom_date = {d} {p} AND cust_id = ?) AS deposits,
              (SELECT COALESCE(SUM(gross_total), 0) FROM delta.gold_db.eom_loans
                 WHERE eom_date = {d} {p} AND cust_id = ?) AS loans
            """,
            (cid, cid),
        )
        deposits = float(rows[0]['deposits'] or 0)
        loans = float(rows[0]['loans'] or 0)
        return {
            'relationship_value': round(deposits + loans),
            'deposits': round(deposits),
            'loans': round(loans),
            'revenue': 0,  # not sourced from a single reconciled column yet
        }

    # --- per-account lists (one row per account, no fan-out) -------------
    def get_deposit_accounts(self, cust_id: str) -> list[dict[str, Any]]:
        cid = self._cid(cust_id)
        if cid is None:
            return []
        rows = self._t.execute(
            f"""
            SELECT account_no, product_desc, book_balance, currency, entry_status,
                   CAST(last_trx_date AS varchar) AS last_trx_date
            FROM delta.gold_db.eom_deposits
            WHERE eom_date = {self._as_of_lit()} {self._asof_part()} AND cust_id = ?
              AND book_balance <> 0
            ORDER BY book_balance DESC
            LIMIT 40
            """,
            (cid,),
        )
        out = []
        for r in rows:
            es = str(r.get('entry_status')).split('.')[0] if r.get('entry_status') is not None else ''
            out.append({
                'account_no': str(r.get('account_no') or '').strip(),
                'product': self._clean(r.get('product_desc')) or 'Deposit account',
                'balance': round(float(r.get('book_balance') or 0)),
                'currency': self._clean(r.get('currency')) or 'KES',
                'status': _ENTRY_STATUS.get(es, 'Active'),
                'last_transaction_date': self._safe_date(r.get('last_trx_date')),
            })
        return out

    def get_loan_accounts(self, cust_id: str) -> list[dict[str, Any]]:
        cid = self._cid(cust_id)
        if cid is None:
            return []
        rows = self._t.execute(
            f"""
            SELECT account_no, product_desc, gross_total,
                   CAST(acc_open_dt AS varchar) AS acc_open_dt,
                   loan_status_ind_name, final_sub_class
            FROM delta.gold_db.eom_loans
            WHERE eom_date = {self._as_of_lit()} {self._asof_part()} AND cust_id = ?
              AND gross_total <> 0
            ORDER BY gross_total DESC
            LIMIT 40
            """,
            (cid,),
        )
        return [{
            'account_no': str(r.get('account_no') or '').strip(),
            'product': self._clean(r.get('product_desc')) or 'Loan facility',
            'outstanding_balance': round(float(r.get('gross_total') or 0)),
            'currency': 'KES',
            'classification': self._clean(r.get('loan_status_ind_name')) or self._clean(r.get('final_sub_class')) or 'Performing',
            'opened': self._safe_date(r.get('acc_open_dt')),
        } for r in rows]

    # --- search (identity only; value is per-customer on the detail page) -
    def search_customers(self, query, *, sales_codes, limit=25, include_staff=True):
        raw = (query or '').strip()
        like = f"%{raw.lower()}%"
        # Identification-document search (backlog item #2): match the raw ID number
        # ignoring the punctuation the warehouse stores (spaces, dots, slashes — e.g.
        # 'BN/2016/447587', 'C.123502', national IDs). Only engaged for a reasonably
        # specific token (≥5 alphanumerics) so a short name query doesn't sweep in
        # every ID that happens to contain those characters.
        id_norm = _re.sub(r'[^A-Za-z0-9]', '', raw).upper()
        id_guard = id_norm if len(id_norm) >= 5 else ''
        id_like = f"%{id_norm}%"
        # Staff customers are admin-only; when excluding them, over-fetch so the filtered
        # page still fills to `limit` rather than showing a short list.
        fetch = int(limit) if include_staff else min(int(limit) * 4, 200)
        rows = self._t.execute(
            """
            SELECT CAST(customer_id AS BIGINT) AS id, full_name, customer_segment,
                   account_branch_name, created_emp_id, created_emp_name,
                   customer_id_no, issue_authority, cust_type, employer, fk_bankemployeeid
            FROM delta.gold_db.dim_customer
            WHERE full_name IS NOT NULL
              AND customer_segment <> 'INTERNAL ACCOUNTS'
              AND (LOWER(full_name) LIKE ?
                   OR CAST(CAST(customer_id AS BIGINT) AS varchar) LIKE ?
                   OR (? <> '' AND UPPER(REPLACE(REPLACE(REPLACE(TRIM(customer_id_no), ' ', ''), '.', ''), '/', '')) LIKE ?))
            LIMIT ?
            """,
            (like, like, id_guard, id_like, fetch),
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            staff = is_staff_from_fields(
                employer=r.get('employer'), segment=r.get('customer_segment'),
                bank_employee_id=r.get('fk_bankemployeeid'))
            if staff and not include_staff:
                continue
            out.append({
                'cust_id': str(r['id']), 'name': self._clean(r.get('full_name')),
                'segment': self._seg_label(r.get('customer_segment')), 'branch': self._clean(r.get('account_branch_name')),
                'sales_code': self._officer_id(r.get('created_emp_id')),
                'rm_name': self._officer_name(r.get('created_emp_name')),
                'id_no': self._clean(r.get('customer_id_no')),
                'id_type': self._id_type(r.get('issue_authority'), r.get('cust_type')),
                'customer_type': self._cust_type_label(r.get('cust_type')),
                'is_staff': staff,
            })
            if len(out) >= int(limit):
                break
        # Prefer the current RM allocation over the account-opening officer (one batched
        # lookup; a no-op when the Postgres allocation is unavailable).
        alloc = self.get_current_rm([row['cust_id'] for row in out])
        if alloc:
            for row in out:
                cur = alloc.get(row['cust_id'])
                if cur:
                    row['rm_name'] = cur.get('name') or row['rm_name']
                    row['sales_code'] = cur.get('code') or row['sales_code']
        return out

    # --- product holdings (canonical flags derived from real products) --------
    def get_product_holdings(self, cust_id):
        flags = {k: False for k in CANON_LABELS}
        cid = self._cid(cust_id)
        if cid is None:
            return {'flags': flags, 'product_map': dict(CANON_LABELS)}
        d, pp = self._as_of_lit(), self._asof_part()
        dep = self._t.execute(
            f"SELECT DISTINCT product_desc p FROM delta.gold_db.eom_deposits "
            f"WHERE eom_date={d} {pp} AND cust_id=?", (cid,))
        loan = self._t.execute(
            f"SELECT DISTINCT product_desc p FROM delta.gold_db.eom_loans "
            f"WHERE eom_date={d} {pp} AND cust_id=?", (cid,))
        for r in dep:
            self._apply_flags(flags, r.get('p'), _DEPOSIT_KEYWORDS, base='deposit')
        for r in loan:
            self._apply_flags(flags, r.get('p'), _LOAN_KEYWORDS, base=None)
        # Mobile-banking flag: any recent transaction on a digital channel.
        _, _, rp = self._recent_window()
        dig = self._t.execute(
            f"SELECT COUNT(*) n FROM delta.gold_db.fact_dep_trx_recording "
            f"WHERE customer_id=? {rp} "
            f"AND UPPER(TRIM(channel_description)) IN ({_DIGITAL_CHANNELS_SQL})", (cid,))
        if dig and (dig[0].get('n') or 0) > 0:
            flags['mobile'] = True
        return {'flags': flags, 'product_map': dict(CANON_LABELS)}

    @staticmethod
    def _apply_flags(flags: dict, desc: Any, keywords, *, base: str | None) -> None:
        if base:
            flags[base] = True
        if not desc:
            return
        low = str(desc).lower()
        for needle, key in keywords:
            if needle in low:
                flags[key] = True
                return

    # --- per-customer balance series (grouped by EOM day → dedup-safe) --------
    def deposit_loan_series(self, cust_id, period):
        cid = self._cid(cust_id)
        if cid is None:
            return {'deposits': [], 'loans': []}
        lo, hi = self._date_lit(period.start), self._date_lit(period.end)
        rp = self._range_part(period.start, period.end)
        dep = self._t.execute(
            f"SELECT CAST(eom_date AS varchar) p, SUM(book_balance) b "
            f"FROM delta.gold_db.eom_deposits "
            f"WHERE cust_id=? {rp} AND eom_date BETWEEN {lo} AND {hi} "
            f"GROUP BY eom_date ORDER BY eom_date", (cid,))
        loan = self._t.execute(
            f"SELECT CAST(eom_date AS varchar) p, SUM(gross_total) b "
            f"FROM delta.gold_db.eom_loans "
            f"WHERE cust_id=? {rp} AND eom_date BETWEEN {lo} AND {hi} "
            f"GROUP BY eom_date ORDER BY eom_date", (cid,))
        return {
            'deposits': [{'period': self._safe_date(r['p']), 'balance': round(float(r['b'] or 0))}
                         for r in dep if self._safe_date(r['p'])],
            'loans': [{'period': self._safe_date(r['p']), 'balance': round(float(r['b'] or 0))}
                      for r in loan if self._safe_date(r['p'])],
        }

    def get_retention_signal(self, cust_id):
        """Silent-attrition early warning (see c360/retention.py). Compares the
        earliest vs latest deposit snapshot in the trailing ~quarter (one
        partition-pruned scan) → DERIVED retention flag. None when there isn't
        enough history, or the account had no positive balance to erode from."""
        cid = self._cid(cust_id)
        if cid is None:
            return None
        asof = self.as_of_date()
        start = asof - timedelta(days=95)
        lo, hi = self._date_lit(start), self._date_lit(asof)
        rp = self._range_part(start, asof)
        rows = self._t.execute(
            f"SELECT CAST(eom_date AS varchar) p, SUM(book_balance) b "
            f"FROM delta.gold_db.eom_deposits "
            f"WHERE cust_id=? {rp} AND eom_date BETWEEN {lo} AND {hi} "
            f"GROUP BY eom_date ORDER BY eom_date", (cid,))
        pts = [(self._safe_date(r['p']), float(r['b'] or 0)) for r in rows]
        pts = [(d, b) for d, b in pts if d]
        if len(pts) < 2:
            return None
        sig = retention_derive.classify_retention(pts[0][1], pts[-1][1])
        if sig is None:
            return None
        sig['from'] = pts[0][0]
        sig['to'] = pts[-1][0]
        return sig

    def disbursement_vs_balance_series(self, cust_id, period):
        cid = self._cid(cust_id)
        if cid is None:
            return {'disbursed': [], 'balance': []}
        lo, hi = self._date_lit(period.start), self._date_lit(period.end)
        rp = self._range_part(period.start, period.end)
        rows = self._t.execute(
            f"SELECT CAST(eom_date AS varchar) p, SUM(tot_drawdown_amn) d, SUM(gross_total) b "
            f"FROM delta.gold_db.eom_loans "
            f"WHERE cust_id=? {rp} AND eom_date BETWEEN {lo} AND {hi} "
            f"GROUP BY eom_date ORDER BY eom_date", (cid,))
        disbursed, balance = [], []
        for r in rows:
            p = self._safe_date(r['p'])
            if not p:
                continue
            disbursed.append({'period': p, 'amount': round(float(r['d'] or 0))})
            balance.append({'period': p, 'amount': round(float(r['b'] or 0))})
        return {'disbursed': disbursed, 'balance': balance}

    # --- transaction activity trend (fact_dep_trx_recording, daily count) -----
    # Count of customer-facing transactions per day: dense, covers every customer,
    # runs right up to the as-of day. (The rpt_c360 trend table was empty for
    # ~all customers — that was the "no data" on this chart.)
    def transaction_series(self, cust_id, period):
        cid = self._cid(cust_id)
        if cid is None:
            return []
        lo, hi = self._date_lit(period.start), self._date_lit(period.end)
        rp = self._range_part(period.start, period.end)
        rows = self._t.execute(
            f"SELECT CAST(transaction_date AS varchar) d, COUNT(*) n "
            f"FROM delta.gold_db.fact_dep_trx_recording "
            f"WHERE customer_id=? {rp} AND transaction_date BETWEEN {lo} AND {hi} "
            f"AND UPPER(TRIM(channel_description)) NOT IN ({_SYS_CHANNELS_SQL}) "
            f"AND UPPER(justific_descrption) NOT LIKE '%ACCRUED INTEREST%' "
            f"GROUP BY CAST(transaction_date AS varchar) ORDER BY 1", (cid,))
        return [{'period': self._safe_date(r['d']), 'count': int(r['n'] or 0)}
                for r in rows if self._safe_date(r['d'])]

    def channel_usage(self, cust_id, period):
        cid = self._cid(cust_id)
        if cid is None:
            return []
        lo, hi = self._date_lit(period.start), self._date_lit(period.end)
        rp = self._range_part(period.start, period.end)
        rows = self._t.execute(
            f"SELECT TRIM(channel_description) ch, COUNT(*) n "
            f"FROM delta.gold_db.fact_dep_trx_recording "
            f"WHERE customer_id=? {rp} AND transaction_date BETWEEN {lo} AND {hi} "
            f"AND UPPER(TRIM(channel_description)) NOT IN ({_SYS_CHANNELS_SQL}) "
            f"AND UPPER(justific_descrption) NOT LIKE '%ACCRUED INTEREST%' "
            f"GROUP BY TRIM(channel_description)", (cid,))
        agg: dict[str, int] = {}
        for r in rows:
            label = self._channel_label(r.get('ch'))
            if label:
                agg[label] = agg.get(label, 0) + int(r['n'] or 0)
        total = sum(agg.values())
        if total <= 0:
            return []
        ordered = sorted(agg.items(), key=lambda kv: kv[1], reverse=True)
        return [{'channel': name, 'share': round(n / total, 4)} for name, n in ordered]

    def recent_transactions(self, cust_id, *, period=None, limit=8, lookback_months=24):
        """Latest customer-facing transactions. Uses a wide (24-month) lookback so
        the feed always shows the customer's most recent activity even if it was a
        while ago — 'recent' means most-recent-available, not last-N-days. When a
        period is given, the window widens to cover it (so QTD/YTD still work).
        ``lookback_months`` can be shrunk by callers that only need a light activity
        probe (e.g. the recommendation engine) to avoid a wide scan per customer."""
        cid = self._cid(cust_id)
        if cid is None:
            return []
        floor = getattr(period, 'start', None)
        start, asof, rp = self._lookback_window(lookback_months, floor=floor)
        rows = self._t.execute(
            f"SELECT CAST(transaction_date AS varchar) d, TRIM(justific_descrption) j, "
            f"channel_description ch, i_amount amt "
            f"FROM delta.gold_db.fact_dep_trx_recording "
            f"WHERE customer_id=? {rp} "
            f"AND transaction_date BETWEEN {self._date_lit(start)} AND {self._date_lit(asof)} "
            f"AND UPPER(TRIM(channel_description)) NOT IN ({_SYS_CHANNELS_SQL}) "
            f"AND UPPER(justific_descrption) NOT LIKE '%ACCRUED INTEREST%' "
            f"AND i_amount <> 0 "
            f"ORDER BY transaction_date DESC LIMIT ?", (cid, int(limit)))
        out = []
        for r in rows:
            out.append({
                'date': self._safe_date(r.get('d')),
                'description': (self._clean(r.get('j')) or 'Transaction').title(),
                'channel': self._channel_label(r.get('ch')) or 'Other',
                'amount': round(float(r.get('amt') or 0)),
                'currency': 'KES',
            })
        return out

    def _months_ago(self, n: int) -> date:
        """First day of the month ``n`` months before the as-of month."""
        asof = self.as_of_date()
        total = asof.year * 12 + (asof.month - 1) - n
        return date(total // 12, total % 12 + 1, 1)

    # Descending, NON-overlapping lookback windows (months-before-as-of: older, newer),
    # scanned newest first. We stop at the first that holds a transaction, so a recently
    # active customer pays only a small partition-pruned scan and only a long-dormant one
    # reaches back a dozen years, never the whole 2.37B-row fact table.
    _LAST_TXN_WINDOWS = ((12, 0), (36, 12), (84, 36), (144, 84))

    def last_transaction_date(self, cust_id: str):
        """The customer's most recent CUSTOMER-FACING transaction date, all-time: the
        honest 'when was this account last active'. Uses the same table and channel /
        accrued-interest filters as ``recent_transactions`` so it agrees with the
        transaction history a user browses: an account can read 'Active' yet show nothing
        under a YTD filter because its last real transaction was years ago. Deliberately
        NOT ``eom_deposits.last_trx_date`` — that column moves with month-end interest /
        system postings and would report a misleadingly recent date.

        Cost varies (a recent customer is one cheap scan; a decade-dormant one walks the
        windows), so callers MUST treat it as a slow probe and keep it off the hot path.
        Returns None when nothing customer-facing is found within ~12 years."""
        cid = self._cid(cust_id)
        if cid is None:
            return None
        asof = self.as_of_date()
        for older, newer in self._LAST_TXN_WINDOWS:
            lo = self._months_ago(older)
            hi = asof if newer == 0 else self._months_ago(newer)
            rp = self._range_part(lo, hi)
            rows = self._t.execute(
                f"SELECT CAST(MAX(transaction_date) AS varchar) d "
                f"FROM delta.gold_db.fact_dep_trx_recording "
                f"WHERE customer_id=? {rp} "
                f"AND transaction_date BETWEEN {self._date_lit(lo)} AND {self._date_lit(hi)} "
                f"AND UPPER(TRIM(channel_description)) NOT IN ({_SYS_CHANNELS_SQL}) "
                f"AND UPPER(justific_descrption) NOT LIKE '%ACCRUED INTEREST%' "
                f"AND i_amount <> 0", (cid,))
            d = self._safe_date(rows[0]['d']) if rows else None
            if d:
                return d
        return None

    # --- bounded live portfolio sample (whole-book stays precompute, §6) ------
    def list_customers(self, *, sales_codes, include_staff: bool = True, sample: int = 200):
        """A bounded set of real loan-holding customers (meaningful value),
        aggregated dedup-safe — feeds the RM-scoped/mock overview fallback. Staff
        customers are dropped unless ``include_staff`` (admin-only)."""
        d, p = self._as_of_lit(), self._asof_part()
        id_rows = self._t.execute(
            f"SELECT DISTINCT cust_id FROM delta.gold_db.eom_loans "
            f"WHERE eom_date={d} {p} AND cust_id IS NOT NULL LIMIT {int(sample)}")
        ids = [self._cid(r['cust_id']) for r in id_rows]
        rows = self._aggregate_customers([i for i in ids if i is not None])
        return rows if include_staff else [r for r in rows if not r.get('is_staff')]

    def list_prospects(self, *, sample: int = 150):
        """Cross-sell *prospects* for the worklist: real customers in higher-value
        segments who currently hold only 1–2 deposit products, so they sit below
        their segment's product average and rule C has genuine gaps to surface."""
        d, p = self._as_of_lit(), self._asof_part()
        id_rows = self._t.execute(
            f"SELECT e.cust_id c FROM ("
            f"  SELECT cust_id, COUNT(DISTINCT product_desc) pc FROM delta.gold_db.eom_deposits "
            f"  WHERE eom_date={d} {p} GROUP BY cust_id HAVING COUNT(DISTINCT product_desc) BETWEEN 1 AND 2) e "
            f"JOIN delta.gold_db.dim_customer dc ON dc.customer_id=e.cust_id "
            f"WHERE dc.customer_segment IN ('PRIVATE','STANDARD','MEDIUM ENTERPRISES',"
            f"'SMALL ENTERPRISES','INSTITUTIONAL BANKING','PROJECT FINANCE','SCHEME') "
            f"LIMIT {int(sample)}", ())
        ids = [self._cid(r['c']) for r in id_rows]
        return self._aggregate_customers([i for i in ids if i is not None])

    def _aggregate_customers(self, ids: list[int]) -> list[dict]:
        """Batch dedup-safe aggregation (value / products / identity) for an id set."""
        if not ids:
            return []
        d, p = self._as_of_lit(), self._asof_part()
        inlist = ','.join(str(i) for i in ids)
        dep = {self._cid(r['c']): (float(r['v'] or 0), int(r['n'] or 0)) for r in self._t.execute(
            f"SELECT cust_id c, SUM(book_balance) v, COUNT(DISTINCT product_desc) n "
            f"FROM delta.gold_db.eom_deposits WHERE eom_date={d} {p} AND cust_id IN ({inlist}) GROUP BY cust_id")}
        loan = {self._cid(r['c']): (float(r['v'] or 0), int(r['n'] or 0)) for r in self._t.execute(
            f"SELECT cust_id c, SUM(gross_total) v, COUNT(DISTINCT product_desc) n "
            f"FROM delta.gold_db.eom_loans WHERE eom_date={d} {p} AND cust_id IN ({inlist}) GROUP BY cust_id")}
        idn = {self._cid(r['id']): r for r in self._t.execute(
            f"SELECT CAST(customer_id AS BIGINT) id, full_name, customer_segment, "
            f"account_branch_name, created_emp_id, created_emp_name, employer, fk_bankemployeeid "
            f"FROM delta.gold_db.dim_customer WHERE customer_id IN ({inlist})")}
        # Current RM for the whole batch in one lookup (empty when Postgres allocation
        # is unavailable → each row keeps its account-opening officer).
        alloc = self.get_current_rm(ids)
        out = []
        for i in ids:
            dv, dn = dep.get(i, (0.0, 0))
            lv, ln = loan.get(i, (0.0, 0))
            ident = idn.get(i, {})
            cur = alloc.get(str(i)) or {}
            out.append({
                'cust_id': str(i),
                'name': self._clean(ident.get('full_name')) or f'Customer {i}',
                'segment': self._seg_label(ident.get('customer_segment')),
                'branch': self._clean(ident.get('account_branch_name')),
                'sales_code': cur.get('code') or self._officer_id(ident.get('created_emp_id')),
                'rm_name': cur.get('name') or self._officer_name(ident.get('created_emp_name')),
                'value': round(dv + lv), 'deposits': round(dv), 'loans': round(lv),
                'products_held': dn + ln,
                'is_staff': is_staff_from_fields(
                    employer=ident.get('employer'), segment=ident.get('customer_segment'),
                    bank_employee_id=ident.get('fk_bankemployeeid')),
            })
        out.sort(key=lambda c: c['value'], reverse=True)
        return out

    def get_linked_parties(self, cust_id):
        """Other customer records belonging to the same legal person — matched on the
        national ID (``dim_customer.customer_id_no``). A person commonly holds several
        customer numbers (personal, joint, business signatory); this stitches them into
        one relationship. Returns None when the ID is blank/placeholder (can't link
        safely) or nothing else shares it. Members carry value/identity (dedup-safe via
        ``_aggregate_customers``) and an ``is_staff`` stamp so the caller's scope + the
        staff sieve can filter them."""
        cid = self._cid(cust_id)
        if cid is None:
            return None
        rows = self._t.execute(
            "SELECT TRIM(customer_id_no) nid FROM delta.gold_db.dim_customer WHERE customer_id=? LIMIT 1", (cid,))
        nid = (rows[0]['nid'] if rows else None) or ''
        if len(nid) < 5 or not any(ch.isdigit() for ch in nid) or nid.strip('0') == '':
            return None
        linked = self._t.execute(
            "SELECT CAST(customer_id AS BIGINT) id FROM delta.gold_db.dim_customer "
            "WHERE TRIM(customer_id_no) = ? AND customer_id <> ? "
            "AND customer_segment <> 'INTERNAL ACCOUNTS' "
            "ORDER BY customer_id LIMIT 12", (nid, cid))
        linked_ids = [int(r['id']) for r in linked if r['id'] is not None]
        if not linked_ids:
            return None
        prim = self._aggregate_customers([cid])
        return {
            'basis': 'National ID',
            'primary_value': prim[0]['value'] if prim else 0,
            'members': self._aggregate_customers(linked_ids),
        }

    def get_related_parties(self, cust_id):
        """Related parties from the curated Postgres ``public.relationship`` register.

        This is a real related-party table — 42k rows of
        ``origin_customer → related_customer`` with a ``relationship_type`` — and it
        answers what ``get_linked_parties`` cannot. That one matches the SAME legal
        person across several customer numbers (national ID); this one links two
        DIFFERENT parties and names the role between them: the directors and
        signatories of a company, the companies a person sits on.

        Notes from the live table, all verified rather than assumed:
        * ``relationship_type`` is space-padded ('DIRECTOR    ') → must be trimmed.
        * ``origin_customer`` / ``related_customer`` are varchar but 100% numeric
          (length 3-7), so they cast cleanly to ``dim_customer.customer_id``.
        * ``expiry_date`` is a varchar and blank on 42,096 of 42,263 rows; the 167
          that carry one are closed relationships and are excluded.
        * The register is DIRECTIONAL, so both columns are searched and the side the
          row was found on becomes the direction shown.

        Returns None when Postgres is not wired, the table is absent (this deployment
        may point at a different database), or the customer has no open relationship —
        the caller then simply shows nothing, exactly as it does today.
        """
        if self._pg is None:
            return None
        cid = self._cid(cust_id)
        if cid is None:
            return None
        key = str(cid)
        try:
            rows = self._pg.execute(
                """
                SELECT related_customer AS other, relationship_type AS rel,
                       'outbound' AS direction, comments, effective_from
                  FROM public.relationship
                 WHERE origin_customer = %s AND COALESCE(expiry_date, '') = ''
                UNION ALL
                SELECT origin_customer AS other, relationship_type AS rel,
                       'inbound' AS direction, comments, effective_from
                  FROM public.relationship
                 WHERE related_customer = %s AND COALESCE(expiry_date, '') = ''
                 LIMIT 60
                """,
                (key, key),
            )
        except Exception:
            # Absent table, wrong database, or Postgres down. A missing optional
            # source must never take the customer page with it.
            logger.warning('related-party lookup failed for %s', cust_id, exc_info=True)
            return None
        if not rows:
            return None

        seen: dict[int, dict] = {}
        for r in rows:
            other = self._cid(r.get('other'))
            if other is None or other == cid:
                continue
            rel = rel_shape.normalise(self._clean(r.get('rel')))
            # One row per counterparty: a pair can be registered under more than one
            # role (a director who also signs), and the panel should say so once.
            entry = seen.setdefault(other, {
                'cust_id': str(other), 'roles': [], 'direction': r.get('direction'),
            })
            if rel and rel not in entry['roles']:
                entry['roles'].append(rel)

        if not seen:
            return None
        # Which of those ids the customer master actually still holds. The register
        # outlives records: it can name a customer number that has since gone. Asked
        # first because `_aggregate_customers` names anything it is handed, falling
        # back to 'Customer <id>' — which would read as a resolved name and quietly
        # assert the record exists. An id we cannot resolve is carried with no name,
        # and the panel says so on its face.
        ids = list(seen)
        inlist = ','.join(str(i) for i in ids)
        present = {self._cid(r['id']) for r in self._t.execute(
            f"SELECT CAST(customer_id AS BIGINT) id FROM delta.gold_db.dim_customer "
            f"WHERE customer_id IN ({inlist})")}
        detail = {self._cid(d['cust_id']): d
                  for d in self._aggregate_customers([i for i in ids if i in present])}
        return rel_shape.shape([
            rel_shape.shape_member(entry, detail.get(other)) for other, entry in seen.items()
        ])

    # --- derived risk / KYC (computed from held data, no dedicated feed) ------
    def get_risk_profile(self, cust_id):
        """Compute KYC (identity completeness) + operational risk (loan performance
        + leverage) from data we already hold — the warehouse has no populated
        risk/KYC column. Two cheap queries: identity attributes and the customer's
        loan classifications; deposits/loans reuse the value snapshot."""
        cid = self._cid(cust_id)
        if cid is None:
            return None
        idr = self._t.execute(
            "SELECT customer_id_no, kra_pin_status, primary_mobile_no, e_mail, "
            "CAST(date_of_birth AS varchar) dob, address "
            "FROM delta.gold_db.dim_customer WHERE customer_id=? LIMIT 1", (cid,))
        if not idr:
            return None
        r = idr[0]
        identity = {
            'id_no': self._clean(r.get('customer_id_no')),
            'kra_pin_status': self._clean(r.get('kra_pin_status')),
            'mobile': self._clean(r.get('primary_mobile_no')),
            'email': self._clean(r.get('e_mail')),
            'date_of_birth': self._safe_date(r.get('dob')),
            'address': self._clean(r.get('address')),
        }
        # Loan classifications at the as-of snapshot (drives operational risk).
        d, p = self._as_of_lit(), self._asof_part()
        lrows = self._t.execute(
            f"SELECT DISTINCT loan_status_ind_name s, final_sub_class f "
            f"FROM delta.gold_db.eom_loans WHERE eom_date={d} {p} AND cust_id=? "
            f"AND gross_total <> 0", (cid,))
        statuses: list[str] = []
        for lr in lrows:
            statuses.append(self._clean(lr.get('s')) or '')
            statuses.append(self._clean(lr.get('f')) or '')
        val = self.get_relationship_value(cust_id)
        return risk_derive.derive_profile(
            identity, statuses, float(val['deposits']), float(val['loans']))

    def get_credit_bureau(self, cust_id):
        """Latest TransUnion (CRB) scorecard record for the customer, matched by
        national ID. The source table repeats each national ID many times, so we take
        the newest row by ``created_at`` (dedupe). Returns None when the customer has
        no numeric national ID or no bureau record; a ``no_hit`` record when they are on
        the bureau but have no scoreable history. Never raises — a probe failure lets the
        caller fall back to 'not sourced' rather than break the page.

        This is a real external feed but a POINT-IN-TIME pull (the load stopped ~Mar
        2026), so the shaped record carries ``as_of`` = the row's load date, which the
        UI shows as provenance. Shaping/sentinel logic lives in c360/credit_bureau.py."""
        cid = self._cid(cust_id)
        if cid is None:
            return None
        nrow = self._t.execute(
            "SELECT TRIM(customer_id_no) nid FROM delta.gold_db.dim_customer "
            "WHERE customer_id=? LIMIT 1", (cid,))
        nid = (nrow[0]['nid'] if nrow else None) or ''
        # The bureau key is a numeric national_id (bigint); a passport/alien id won't
        # match, so bail rather than mis-join. Guard blank/short ids too.
        if len(nid) < 5 or not nid.isdigit():
            return None
        rows = self._t.execute(
            "SELECT score, score_grade, probability, non_performing, arrears_90_days, "
            "max_arrears_last_6_months, number_of_enquiries, enquiries_90_days, "
            "CAST(created_at AS varchar) created "
            "FROM delta.gold_db.score_card_review_tu_accounts_summary "
            "WHERE national_id = ? ORDER BY created_at DESC LIMIT 1", (int(nid),))
        if not rows:
            return None
        r = rows[0]
        as_of = (self._clean(r.get('created')) or '')[:10] or None
        return bureau_shape.shape_bureau(r, as_of=as_of)

    def portfolio_trends(self, customers, period):
        """Real book / segment / top-mover history for the sample from the EOM
        balance snapshots — no simulation. Two partition-pruned scans (deposits +
        loans) over the sample id list serve all three: the book & segment series
        follow the selected period, and top movers are the per-customer value change
        from the first to the last snapshot in that period (so movers respond to the
        period slider too) — computed from the same fetched data, no extra scans."""
        ids = [self._cid(c['cust_id']) for c in customers]
        pairs = [(i, c) for i, c in zip(ids, customers) if i is not None]
        if not pairs:
            return None
        ids = [i for i, _ in pairs]
        seg_of = {i: (c.get('segment') or 'Unsegmented') for i, c in pairs}
        name_of = {i: c.get('name') for i, c in pairs}
        inlist = ','.join(str(i) for i in ids)
        lo, hi = self._date_lit(period.start), self._date_lit(period.end)
        rp = self._range_part(period.start, period.end)

        def eom(table, col):
            return self._t.execute(
                f"SELECT CAST(eom_date AS varchar) d, cust_id c, SUM({col}) v "
                f"FROM delta.gold_db.{table} "
                f"WHERE eom_date BETWEEN {lo} AND {hi} {rp} AND cust_id IN ({inlist}) "
                f"GROUP BY eom_date, cust_id", ())

        dep_rows = eom('eom_deposits', 'book_balance')
        loan_rows = eom('eom_loans', 'gross_total')

        # Aggregate to per-date book totals, per-(date, segment) value, and
        # per-(date, customer) value (the last drives movers, no extra query).
        book_dep: dict[str, float] = {}
        book_loan: dict[str, float] = {}
        seg_by_date: dict[str, dict[str, float]] = {}
        cust_by_date: dict[str, dict[int, float]] = {}
        for rows, bucket in ((dep_rows, book_dep), (loan_rows, book_loan)):
            for r in rows:
                d = self._safe_date(r['d'])
                if not d:
                    continue
                v = float(r['v'] or 0)
                cid = self._cid(r['c'])
                bucket[d] = bucket.get(d, 0.0) + v
                seg = seg_of.get(cid, 'Unsegmented')
                day = seg_by_date.setdefault(d, {})
                day[seg] = day.get(seg, 0.0) + v
                cday = cust_by_date.setdefault(d, {})
                cday[cid] = cday.get(cid, 0.0) + v

        dates = sorted(set(book_dep) | set(book_loan) | set(seg_by_date))
        if not dates:
            return None
        seg_names = sorted({s for day in seg_by_date.values() for s in day},
                           key=lambda s: -sum(day.get(s, 0) for day in seg_by_date.values()))
        book = {
            'deposits': [{'period': d, 'balance': round(book_dep.get(d, 0.0))} for d in dates],
            'loans': [{'period': d, 'balance': round(book_loan.get(d, 0.0))} for d in dates],
        }
        segments_data = [{'period': d, **{s: round(seg_by_date.get(d, {}).get(s, 0.0)) for s in seg_names}}
                         for d in dates]
        movers = self._movers_from(cust_by_date, dates, seg_of, name_of)
        return {'book': book, 'segments': seg_names, 'segments_data': segments_data, 'movers': movers}

    @staticmethod
    def _movers_from(cust_by_date, dates, seg_of, name_of, limit=6):
        """Top movers = per-customer value change from the first to the last snapshot
        in the fetched window (real, period-responsive, reuses the trend data)."""
        if len(dates) < 2:
            return []
        first, last = cust_by_date.get(dates[0], {}), cust_by_date.get(dates[-1], {})
        movers = []
        for cid in set(first) | set(last):
            prev, now = first.get(cid, 0.0), last.get(cid, 0.0)
            delta = now - prev
            if prev <= 0 and now <= 0:
                continue
            pct = (delta / prev) if prev > 0 else (1.0 if delta > 0 else 0.0)
            movers.append({
                'cust_id': str(cid), 'name': name_of.get(cid) or f'Customer {cid}',
                'segment': seg_of.get(cid, 'Unsegmented'),
                'value': round(now), 'delta_pct': round(pct, 3), 'delta_value': round(delta),
                'direction': 'up' if delta >= 0 else 'down',
            })
        movers.sort(key=lambda m: abs(m['delta_value']), reverse=True)
        return movers[:limit]

    # --- batch lookups (fast worklist: a few queries for the whole set) -------
    def batch_product_flags(self, cust_ids: list[str]) -> dict[str, dict]:
        """Canonical product flags for many customers in 3 queries (not 3 per
        customer) — feeds the cross-sell worklist without per-customer round-trips."""
        ids = [self._cid(c) for c in cust_ids]
        ids = [i for i in ids if i is not None]
        if not ids:
            return {}
        inlist = ','.join(str(i) for i in ids)
        d, p = self._as_of_lit(), self._asof_part()
        flags = {i: {k: False for k in CANON_LABELS} for i in ids}
        for r in self._t.execute(
            f"SELECT cust_id c, product_desc pd FROM delta.gold_db.eom_deposits "
            f"WHERE eom_date={d} {p} AND cust_id IN ({inlist}) GROUP BY cust_id, product_desc", ()):
            cid = self._cid(r['c'])
            if cid in flags:
                self._apply_flags(flags[cid], r.get('pd'), _DEPOSIT_KEYWORDS, base='deposit')
        for r in self._t.execute(
            f"SELECT cust_id c, product_desc pd FROM delta.gold_db.eom_loans "
            f"WHERE eom_date={d} {p} AND cust_id IN ({inlist}) GROUP BY cust_id, product_desc", ()):
            cid = self._cid(r['c'])
            if cid in flags:
                self._apply_flags(flags[cid], r.get('pd'), _LOAN_KEYWORDS, base=None)
        _, _, rp = self._recent_window()
        for r in self._t.execute(
            f"SELECT DISTINCT customer_id c FROM delta.gold_db.fact_dep_trx_recording "
            f"WHERE customer_id IN ({inlist}) {rp} "
            f"AND UPPER(TRIM(channel_description)) IN ({_DIGITAL_CHANNELS_SQL})", ()):
            cid = self._cid(r['c'])
            if cid in flags:
                flags[cid]['mobile'] = True
        return {str(i): {'flags': flags[i], 'product_map': dict(CANON_LABELS)} for i in ids}

    def batch_risk_profiles(self, cust_ids: list[str], value_map: dict[str, tuple]) -> dict[str, dict]:
        """Derived risk/KYC for many customers in 2 queries. ``value_map`` supplies
        (deposits, loans) already fetched by the roster, so no per-customer value query."""
        ids = [self._cid(c) for c in cust_ids]
        ids = [i for i in ids if i is not None]
        if not ids:
            return {}
        inlist = ','.join(str(i) for i in ids)
        d, p = self._as_of_lit(), self._asof_part()
        idn = {self._cid(r['id']): r for r in self._t.execute(
            f"SELECT CAST(customer_id AS BIGINT) id, customer_id_no, kra_pin_status, "
            f"primary_mobile_no, e_mail, CAST(date_of_birth AS varchar) dob, address "
            f"FROM delta.gold_db.dim_customer WHERE customer_id IN ({inlist})", ())}
        lstat: dict[int, list] = {}
        for r in self._t.execute(
            f"SELECT cust_id c, loan_status_ind_name s, final_sub_class f "
            f"FROM delta.gold_db.eom_loans WHERE eom_date={d} {p} AND cust_id IN ({inlist}) "
            f"AND gross_total <> 0", ()):
            cid = self._cid(r['c'])
            lstat.setdefault(cid, []).extend([self._clean(r.get('s')) or '', self._clean(r.get('f')) or ''])
        out = {}
        for i in ids:
            r = idn.get(i, {})
            identity = {
                'id_no': self._clean(r.get('customer_id_no')),
                'kra_pin_status': self._clean(r.get('kra_pin_status')),
                'mobile': self._clean(r.get('primary_mobile_no')),
                'email': self._clean(r.get('e_mail')),
                'date_of_birth': self._safe_date(r.get('dob')),
                'address': self._clean(r.get('address')),
            }
            dep, loan = value_map.get(str(i), (0.0, 0.0))
            out[str(i)] = risk_derive.derive_profile(identity, lstat.get(i, []), float(dep), float(loan))
        return out

    # --- whole-book portfolio (real aggregates, no sampling) ------------------
    def _whole_book_asof(self) -> dict[str, Any]:
        """Period-independent whole-book aggregates at the as-of date — segment mix,
        risk distribution, month-over-month movers and the per-segment product
        benchmark. Memoised because these scan the whole book and don't change within
        the day. All partition-pruned to the current month."""
        if self._wb_asof is not None and (time.time() - self._wb_asof_at) < self._WB_TTL_SECONDS:
            return self._wb_asof
        d, p = self._as_of_lit(), self._asof_part()

        excl = f" AND dc.customer_segment <> '{self._INTERNAL_SEGMENT}' "
        dep_seg = self._t.execute(
            f"SELECT dc.customer_segment seg, COUNT(DISTINCT e.cust_id) n, SUM(e.book_balance) v "
            f"FROM delta.gold_db.eom_deposits e JOIN delta.gold_db.dim_customer dc "
            f"ON dc.customer_id = e.cust_id WHERE e.eom_date={d} {p} {excl} GROUP BY dc.customer_segment", ())
        loan_seg = self._t.execute(
            f"SELECT dc.customer_segment seg, COUNT(DISTINCT e.cust_id) n, SUM(e.gross_total) v "
            f"FROM delta.gold_db.eom_loans e JOIN delta.gold_db.dim_customer dc "
            f"ON dc.customer_id = e.cust_id WHERE e.eom_date={d} {p} {excl} GROUP BY dc.customer_segment", ())

        seg: dict[str, dict] = {}
        for r in dep_seg:
            s = self._seg_label(r.get('seg'))
            seg.setdefault(s, {'segment': s, 'customers': 0, 'value': 0, 'deposits': 0, 'loans': 0})
            seg[s]['deposits'] += round(float(r.get('v') or 0))
            seg[s]['customers'] += int(r.get('n') or 0)   # segments are disjoint → sum, no dup
        for r in loan_seg:
            s = self._seg_label(r.get('seg'))
            seg.setdefault(s, {'segment': s, 'customers': 0, 'value': 0, 'deposits': 0, 'loans': 0})
            seg[s]['loans'] += round(float(r.get('v') or 0))
        for s in seg.values():
            s['value'] = s['deposits'] + s['loans']
        segment_mix = sorted(seg.values(), key=lambda x: x['value'], reverse=True)

        # Product benchmark + overall avg products, per segment (drives rule C).
        prod = self._t.execute(
            f"SELECT dc.customer_segment seg, AVG(pc) avg_products, COUNT(*) custs FROM ("
            f"  SELECT cust_id, COUNT(DISTINCT product_desc) pc FROM delta.gold_db.eom_deposits "
            f"  WHERE eom_date={d} {p} GROUP BY cust_id) e "
            f"JOIN delta.gold_db.dim_customer dc ON dc.customer_id=e.cust_id "
            f"WHERE dc.customer_segment <> '{self._INTERNAL_SEGMENT}' "
            f"GROUP BY dc.customer_segment", ())
        benchmarks = {self._seg_label(r['seg']): round(float(r['avg_products'] or 0), 2)
                      for r in prod}
        tot_c = sum(int(r['custs'] or 0) for r in prod) or 1
        avg_products = round(sum(float(r['avg_products'] or 0) * int(r['custs'] or 0) for r in prod) / tot_c, 1)

        risk_rows = self._whole_book_risk(d, p)
        movers = self._whole_book_movers()

        ni = self._not_internal_cid()
        # Active customers = distinct customers holding at least one ACTIVE account
        # (entry_status = 1), vs the total on-file count — the "how much of the book is
        # live" KPI. Most of the book is closed/dormant (largely VIRTUAL wallets), so
        # this is a genuine portfolio-health signal, not a vanity total.
        act = self._t.execute(
            f"SELECT COUNT(DISTINCT cust_id) n FROM delta.gold_db.eom_deposits "
            f"WHERE eom_date={d} {p} {ni} AND TRY_CAST(entry_status AS integer) = 1", ())
        active_customers = int(act[0]['n'] or 0) if act else 0

        # Top deposit products by balance — "what actually holds the book" (cross-sell
        # headroom lives in the products a segment under-holds).
        tp = self._t.execute(
            f"SELECT product_desc pd, SUM(book_balance) v, COUNT(DISTINCT cust_id) n "
            f"FROM delta.gold_db.eom_deposits WHERE eom_date={d} {p} {ni} AND book_balance > 0 "
            f"GROUP BY product_desc ORDER BY 2 DESC LIMIT 8", ())
        top_products = [{'product': (self._clean(r.get('pd')) or 'Other').title(),
                         'value': round(float(r.get('v') or 0)),
                         'customers': int(r.get('n') or 0)} for r in tp]

        # Headline count = distinct customers with any holding (the risk population),
        # excluding internal accounts — so it matches the risk donut exactly and never
        # double-counts a deposit + loan customer.
        customers = sum(r['customers'] for r in risk_rows)
        deposits = sum(s['deposits'] for s in segment_mix)
        loans = sum(s['loans'] for s in segment_mix)

        # Refuse a partial read. eom_deposits is 2.37B rows; a transient timeout can
        # return the loan half of the book while the deposit scans come back empty, so
        # deposits/active/products collapse to 0 while loans stay populated — never a
        # real state for this book. Caching it would freeze silent live KES 0s
        # (provenance: never a silent dash). Raise instead: the caller falls back to
        # the labelled roster sample and the next request retries a clean read.
        if self._wb_degraded(customers, active_customers, deposits, loans):
            raise LiveDataNotReady(
                f'whole-book read looks partial — deposits={deposits:.0f}, loans={loans:.0f}, '
                f'active={active_customers}, customers={customers}; not caching')

        self._benchmarks = benchmarks
        self._wb_asof = {
            'customers': customers, 'active_customers': active_customers,
            'deposits': deposits, 'loans': loans,
            'avg_products': avg_products, 'segment_mix': segment_mix,
            'risk_rows': risk_rows, 'movers': movers, 'benchmarks': benchmarks,
            'top_products': top_products,
        }
        self._wb_asof_at = time.time()
        return self._wb_asof

    @staticmethod
    def _wb_degraded(customers: int, active_customers: int, deposits: float, loans: float) -> bool:
        """A whole-book snapshot is trustworthy only if both halves of the book came
        through together. If exactly one of deposits/loans is present the other scan
        failed; if holdings exist but the live population is empty the count scans
        failed. Both-zero is left to pass (ambiguous — genuinely-empty book or a bad
        as-of date, not our asymmetric partial-read signature)."""
        has_dep, has_loan = deposits > 0, loans > 0
        if has_dep != has_loan:
            return True
        if (has_dep or has_loan) and (customers <= 0 or active_customers <= 0):
            return True
        return False

    def _whole_book_risk(self, d, p) -> list[dict]:
        ni = self._not_internal_cid()
        rows = self._t.execute(
            f"SELECT bucket, COUNT(*) n FROM ("
            f" SELECT CASE "
            f"   WHEN COALESCE(l.loan,0) <= 0 AND COALESCE(dp.dep,0) <= 0 THEN 'Unclassified' "
            f"   WHEN COALESCE(l.loan,0) <= 0 THEN 'Low' "
            f"   WHEN l.loan / (COALESCE(dp.dep,0) + 1) >= 8 AND l.loan >= 1000000 THEN 'High' "
            f"   WHEN l.loan / (COALESCE(dp.dep,0) + 1) >= 3 THEN 'Medium' ELSE 'Low' END bucket "
            f" FROM (SELECT cust_id, SUM(book_balance) dep FROM delta.gold_db.eom_deposits "
            f"       WHERE eom_date={d} {p} {ni} GROUP BY cust_id) dp "
            f" FULL OUTER JOIN (SELECT cust_id, SUM(gross_total) loan FROM delta.gold_db.eom_loans "
            f"       WHERE eom_date={d} {p} {ni} GROUP BY cust_id) l ON dp.cust_id = l.cust_id"
            f") GROUP BY bucket", ())
        counts = {self._clean(r['bucket']): int(r['n'] or 0) for r in rows}
        return [{'class': c, 'customers': counts.get(c, 0)}
                for c in ('Low', 'Medium', 'High', 'Unclassified')]

    def _whole_book_movers(self, limit=6) -> list[dict]:
        """Whole-book top movers — largest value change between the previous month-end
        and the as-of snapshot (period-independent, so it's part of the cached bundle)."""
        asof = self.as_of_date()
        prev_me = date(asof.year, asof.month, 1)
        rp = self._range_part(prev_me, asof)
        ni = self._not_internal_cid()
        now, prev = self._date_lit(asof), self._date_lit(prev_me)
        rows = self._t.execute(
            f"SELECT cust_id, "
            f"  SUM(CASE WHEN eom_date={now} THEN v ELSE 0 END) - SUM(CASE WHEN eom_date={prev} THEN v ELSE 0 END) delta, "
            f"  SUM(CASE WHEN eom_date={now} THEN v ELSE 0 END) now_v FROM ("
            f"  SELECT cust_id, eom_date, book_balance v FROM delta.gold_db.eom_deposits WHERE eom_date IN ({now},{prev}) {rp} {ni} "
            f"  UNION ALL SELECT cust_id, eom_date, gross_total v FROM delta.gold_db.eom_loans WHERE eom_date IN ({now},{prev}) {rp} {ni} "
            f") GROUP BY cust_id "
            f"ORDER BY abs(SUM(CASE WHEN eom_date={now} THEN v ELSE 0 END) - SUM(CASE WHEN eom_date={prev} THEN v ELSE 0 END)) DESC "
            f"LIMIT {int(limit)}", ())
        ids = [self._cid(r['cust_id']) for r in rows if r.get('cust_id') is not None]
        ids = [i for i in ids if i is not None]
        idn = {}
        if ids:
            inlist = ','.join(str(i) for i in ids)
            idn = {self._cid(r['id']): r for r in self._t.execute(
                f"SELECT CAST(customer_id AS BIGINT) id, full_name, customer_segment "
                f"FROM delta.gold_db.dim_customer WHERE customer_id IN ({inlist})", ())}
        out = []
        for r in rows:
            cid = self._cid(r['cust_id'])
            now_v = float(r.get('now_v') or 0)
            delta = float(r.get('delta') or 0)
            prev_v = now_v - delta
            pct = (delta / prev_v) if prev_v > 0 else (1.0 if delta > 0 else 0.0)
            ident = idn.get(cid, {})
            out.append({
                'cust_id': str(cid), 'name': self._clean(ident.get('full_name')) or f'Customer {cid}',
                'segment': self._seg_label(ident.get('customer_segment')),
                'value': round(now_v), 'delta_pct': round(pct, 3), 'delta_value': round(delta),
                'direction': 'up' if delta >= 0 else 'down',
            })
        return out

    def portfolio_whole_book(self, period):
        """Real whole-book portfolio — no sampling. Headline / segment mix / risk /
        movers / benchmark come from the memoised as-of bundle; the book trend is a
        real per-day whole-book sum over the period; the per-segment value trend is
        interpolated between two real snapshots (period start & as-of) because the
        full per-day segment join exceeds the cluster memory limit."""
        base = self._whole_book_asof()
        lo, hi = self._date_lit(period.start), self._date_lit(period.end)
        rp = self._range_part(period.start, period.end)
        ni = self._not_internal_cid()

        bt_dep = self._t.execute(
            f"SELECT CAST(eom_date AS varchar) d, SUM(book_balance) v FROM delta.gold_db.eom_deposits "
            f"WHERE eom_date BETWEEN {lo} AND {hi} {rp} {ni} GROUP BY eom_date ORDER BY eom_date", ())
        bt_loan = self._t.execute(
            f"SELECT CAST(eom_date AS varchar) d, SUM(gross_total) v FROM delta.gold_db.eom_loans "
            f"WHERE eom_date BETWEEN {lo} AND {hi} {rp} {ni} GROUP BY eom_date ORDER BY eom_date", ())
        dep_by = {self._safe_date(r['d']): float(r['v'] or 0) for r in bt_dep if self._safe_date(r['d'])}
        loan_by = {self._safe_date(r['d']): float(r['v'] or 0) for r in bt_loan if self._safe_date(r['d'])}
        dates = sorted(set(dep_by) | set(loan_by))
        book = {
            'deposits': [{'period': dt, 'balance': round(dep_by.get(dt, 0.0))} for dt in dates],
            'loans': [{'period': dt, 'balance': round(loan_by.get(dt, 0.0))} for dt in dates],
        }

        # Segment value trend: interpolate each segment's share between the real
        # start-of-period and as-of shares, applied to the real per-day book total.
        seg_start = self._segment_values(period.start)
        seg_end = {s['segment']: s['value'] for s in base['segment_mix']}
        seg_names = [s['segment'] for s in base['segment_mix']]
        tot_start = sum(seg_start.values()) or 1.0
        tot_end = sum(seg_end.values()) or 1.0
        share_start = {s: seg_start.get(s, 0.0) / tot_start for s in seg_names}
        share_end = {s: seg_end.get(s, 0.0) / tot_end for s in seg_names}
        span = max((period.end - period.start).days, 1)
        segments_data = []
        for dt in dates:
            frac = min(max((date.fromisoformat(dt) - period.start).days / span, 0.0), 1.0)
            total = dep_by.get(dt, 0.0) + loan_by.get(dt, 0.0)
            row = {'period': dt}
            for s in seg_names:
                share = share_start[s] + frac * (share_end[s] - share_start[s])
                row[s] = round(total * share)
            segments_data.append(row)

        return {
            **{k: base[k] for k in ('customers', 'active_customers', 'deposits', 'loans',
                                    'avg_products', 'segment_mix', 'risk_rows', 'movers',
                                    'top_products')},
            'book_trend': book, 'segments': seg_names, 'segments_data': segments_data,
        }

    def _segment_values(self, on: date) -> dict[str, float]:
        """Per-segment total value (deposits + loans) at a single date — one join per
        table (cheap at a single date, unlike the full-period join)."""
        dd = self._date_lit(on)
        pp = self._asof_part() if on == self.as_of_date() else self._range_part(on, on)
        out: dict[str, float] = {}
        for table, col in (('eom_deposits', 'book_balance'), ('eom_loans', 'gross_total')):
            for r in self._t.execute(
                f"SELECT dc.customer_segment seg, SUM(e.{col}) v FROM delta.gold_db.{table} e "
                f"JOIN delta.gold_db.dim_customer dc ON dc.customer_id=e.cust_id "
                f"WHERE e.eom_date={dd} {pp} AND dc.customer_segment <> '{self._INTERNAL_SEGMENT}' "
                f"GROUP BY dc.customer_segment", ()):
                s = self._seg_label(r.get('seg'))
                out[s] = out.get(s, 0.0) + float(r.get('v') or 0)
        return out

    def segment_product_benchmark(self, segment):
        """Real per-segment average product count (whole-segment aggregate), so rule C
        fires against a true peer benchmark instead of abstaining. Memoised via the
        whole-book bundle. Raises LiveDataNotReady only if the aggregate can't run."""
        if self._benchmarks is None:
            self._whole_book_asof()
        seg = self._seg_label(segment)
        val = (self._benchmarks or {}).get(seg)
        if val is None:
            raise LiveDataNotReady('no benchmark for this segment')
        return val

    @staticmethod
    def _whizz_cat(j: Any) -> str:
        if not j:
            return 'Whizz transaction'
        u = str(j).strip().upper()
        for needle, label in _WHIZZ_CATEGORY.items():
            if needle in u:
                return label
        return str(j).strip().title()

    def get_whizz(self, cust_id, period):
        """Whizz domain from the customer's KOCELA transactions (cust_id-keyed) plus
        the customers_whizz profile (joined by phone). Returns None when there's no
        Whizz footprint at all → the service shows an honest empty state. Journal
        movements are excluded so the mix reflects real consumer Whizz usage."""
        cid = self._cid(cust_id)
        if cid is None:
            return None
        lo, hi = self._date_lit(period.start), self._date_lit(period.end)
        rp = self._range_part(period.start, period.end)
        where = (f"FROM delta.gold_db.fact_dep_trx_recording WHERE customer_id=? {rp} "
                 f"AND transaction_date BETWEEN {lo} AND {hi} AND {_KOCELA_SQL} "
                 f"AND i_amount <> 0 AND UPPER(justific_descrption) NOT LIKE '%JOURNAL%'")
        cats = self._t.execute(
            f"SELECT TRIM(justific_descrption) j, COUNT(*) n, SUM(i_amount) v {where} "
            f"GROUP BY TRIM(justific_descrption) ORDER BY v DESC", (cid,))
        txn_count = sum(int(r['n'] or 0) for r in cats)

        # No Whizz activity in the window → honest empty state (a registered-but-
        # dormant Whizz profile alone isn't worth a page of empty charts).
        if txn_count == 0:
            return None

        # Profile (best-effort; join by phone number — the reliable bridge key).
        prof = self._t.execute(
            "SELECT cust_status st, CAST(date_of_reg AS varchar) reg FROM delta.gold_db.customers_whizz "
            "WHERE CAST(phone_number AS varchar) = "
            "(SELECT TRIM(primary_mobile_no) FROM delta.gold_db.dim_customer WHERE customer_id=? LIMIT 1) "
            "LIMIT 1", (cid,))
        has_profile = bool(prof)

        categories = [{'label': self._whizz_cat(r['j']), 'count': int(r['n'] or 0),
                       'value': round(float(r['v'] or 0))} for r in cats]
        txn_value = sum(c['value'] for c in categories)
        activity = self._t.execute(
            f"SELECT CAST(transaction_date AS varchar) d, COUNT(*) n, SUM(i_amount) v {where} "
            f"GROUP BY CAST(transaction_date AS varchar) ORDER BY 1", (cid,))
        activity_pts = [{'period': self._safe_date(r['d']), 'count': int(r['n'] or 0),
                         'value': round(float(r['v'] or 0))}
                        for r in activity if self._safe_date(r['d'])]
        recent = self._t.execute(
            f"SELECT CAST(transaction_date AS varchar) d, TRIM(justific_descrption) j, i_amount amt {where} "
            f"ORDER BY transaction_date DESC LIMIT 8", (cid,))
        recent_rows = [{'date': self._safe_date(r['d']), 'description': self._whizz_cat(r['j']),
                        'amount': round(float(r['amt'] or 0)), 'currency': 'KES'} for r in recent]

        if has_profile:
            status = 'Active' if str(prof[0].get('st')).split('.')[0] == '1' else 'Registered'
            since = self._safe_date(prof[0].get('reg'))
        else:
            status, since = 'Transacting', None
        return {
            'status': status,
            'registered_since': since,
            'txn_count': txn_count,
            'txn_value': txn_value,
            'services_used': len(categories),
            'activity': activity_pts,
            'categories': categories,
            'recent': recent_rows,
        }

    def get_properties(self, cust_id):
        """Property units held by a bank customer. Bridged by national ID
        (dim_customer.customer_id_no = hfdi client_idno), sourced from the pre-built
        rpt_c360_customer_property table. That table is event-sourced and repeats
        each unit many times, so we DEDUPE by unit_id (GROUP BY) before summing —
        rpt_c360_property_value's own totals are inflated by that duplication and are
        NOT trusted. Returns None when the customer owns no the property register unit."""
        # An the property register client owns their units directly - no national-ID bridge needed,
        # and none available for the 78% who are not bank customers. The property
        # table keys on client_id, so this is the exact path rather than the
        # best-effort one the bank side has to use.
        client_id_int = prop_reg.parse_id(cust_id)
        if client_id_int is not None:
            return self._properties_for_units(self._property_units_by_client(client_id_int))
        cid = self._cid(cust_id)
        if cid is None:
            return None
        nid_rows = self._t.execute(
            "SELECT TRIM(customer_id_no) nid FROM delta.gold_db.dim_customer WHERE customer_id=? LIMIT 1", (cid,))
        nid = (nid_rows[0]['nid'] if nid_rows else None) or ''
        # Guard against blank/placeholder national IDs matching many property clients.
        if len(nid) < 5 or not any(ch.isdigit() for ch in nid):
            return None
        units = self._t.execute(
            "SELECT cp.unit_id, MAX(TRIM(cp.project_name)) project, MAX(TRIM(cp.unit_name)) unit, "
            "MAX(TRY_CAST(cp.unit_value AS double)) value, MAX(cp.perc_paid) paid "
            "FROM delta.gold_db.rpt_c360_customer_property cp "
            "WHERE TRIM(cp.client_idno) = ? AND cp.unit_id IS NOT NULL "
            "GROUP BY cp.unit_id", (nid,))
        if not units:
            # No units for this national ID. Before saying "no properties", make sure the
            # source itself is populated — an empty/unreachable property table must surface
            # as "couldn't load", not as a false "this customer owns nothing".
            if not self._source_has_rows('delta.gold_db.rpt_c360_customer_property'):
                raise LiveDataNotReady('property source (rpt_c360_customer_property) is empty or unreachable')
            return None
        return self._properties_for_units(units)

    def _property_units_by_client(self, client_id: int) -> list[dict[str, Any]]:
        """Units owned by one property-register client, deduped by unit_id.

        rpt_c360_customer_property repeats each unit many times (it is event-sourced),
        so this GROUPs before anything sums - the same reason the bank-side query
        does, and the reason rpt_c360_property_value's own totals are not trusted.
        """
        return self._t.execute(
            "SELECT cp.unit_id, MAX(TRIM(cp.project_name)) project, MAX(TRIM(cp.unit_name)) unit, "
            "MAX(TRY_CAST(cp.unit_value AS double)) value, MAX(cp.perc_paid) paid "
            "FROM delta.gold_db.rpt_c360_customer_property cp "
            "WHERE cp.client_id = ? AND cp.unit_id IS NOT NULL "
            "GROUP BY cp.unit_id", (float(client_id),))

    def _properties_for_units(self, units: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Shape deduped unit rows into the properties payload, flagging mortgages."""
        if not units:
            return None
        unit_ids = [int(u['unit_id']) for u in units if u['unit_id'] is not None]
        mortgaged: set[int] = set()
        if unit_ids:
            inlist = ','.join(str(i) for i in unit_ids)
            mrows = self._t.execute(
                f"SELECT DISTINCT unit_id FROM delta.gold_db.hfdi_mortgage_data WHERE unit_id IN ({inlist})")
            mortgaged = {int(r['unit_id']) for r in mrows if r['unit_id'] is not None}
        properties = []
        for u in units:
            uid = int(u['unit_id'])
            paid = float(u['paid'] or 0)
            properties.append({
                'unit': self._clean(u['unit']) or f'Unit {uid}',
                'project': self._clean(u['project']) or 'Property',
                'value': round(float(u['value'] or 0)),
                'paid_pct': round(min(max(paid, 0.0), 1.0), 3),
                'mortgage': uid in mortgaged,
            })
        properties.sort(key=lambda p: p['value'], reverse=True)
        return {'properties': properties}

    # --- the property register property clients (their own universe, see c360/property_register.py) --------

    _REGISTER_SELECT = (
        "SELECT client_id, TRIM(client_name) client_name, TRIM(client_idno) client_idno, "
        "TRIM(client_phone) client_phone, TRIM(client_email) client_email, "
        "TRIM(client_pin) client_pin FROM delta.gold_db.hfdi_client_data "
    )

    def _hfdi_units_summary(self, client_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Unit count, total value and payment progress per client, deduped by unit."""
        if not client_ids:
            return {}
        inlist = ','.join(str(float(i)) for i in client_ids)
        rows = self._t.execute(
            "SELECT client_id, count(*) units, SUM(value) units_value, AVG(paid) paid_pct, "
            "ARRAY_AGG(DISTINCT project) projects FROM ("
            "  SELECT cp.client_id, cp.unit_id, MAX(TRY_CAST(cp.unit_value AS double)) value, "
            "         MAX(cp.perc_paid) paid, MAX(TRIM(cp.project_name)) project "
            "    FROM delta.gold_db.rpt_c360_customer_property cp "
            f"   WHERE cp.client_id IN ({inlist}) AND cp.unit_id IS NOT NULL "
            "   GROUP BY cp.client_id, cp.unit_id) u GROUP BY client_id")
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            paid = r.get('paid_pct')
            out[int(float(r['client_id']))] = {
                'units': int(r.get('units') or 0),
                'units_value': float(r.get('units_value') or 0),
                'paid_pct': round(min(max(float(paid), 0.0), 1.0), 3) if paid is not None else None,
                'projects': sorted(p for p in (r.get('projects') or []) if p),
            }
        return out

    def _hfdi_bank_matches(self, idnos: list[str]) -> dict[str, dict[str, Any]]:
        """Which of these property-register national IDs belong to a bank customer too.

        Matched on the normalised id (punctuation stripped, upper-cased) because the
        two systems punctuate registration numbers differently. That normalisation is
        worth two extra clients out of 3,429 - the gap is real, not cosmetic - but it
        costs nothing and removes a class of false 'not a customer'.
        """
        clean = [i for i in {(i or '').strip() for i in idnos} if len(i) >= 5]
        if not clean:
            return {}
        quoted = ','.join("'" + i.replace("'", "''") + "'" for i in clean)
        rows = self._t.execute(
            "SELECT CAST(customer_id AS BIGINT) id, TRIM(customer_id_no) idno, "
            "customer_segment, employer, fk_bankemployeeid "
            f"FROM delta.gold_db.dim_customer WHERE TRIM(customer_id_no) IN ({quoted})")
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = prop_reg.normalise_idno(r.get('idno'))
            if not key or key in out:
                continue
            out[key] = {
                'cust_id': str(r['id']),
                'is_staff': is_staff_from_fields(
                    employer=r.get('employer'), segment=r.get('customer_segment'),
                    bank_employee_id=r.get('fk_bankemployeeid')),
            }
        return out

    def _hfdi_decorate(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach unit totals and the bank bridge to raw register rows."""
        if not rows:
            return []
        ids = [int(float(r['client_id'])) for r in rows if r.get('client_id') is not None]
        units = self._hfdi_units_summary(ids)
        banked = self._hfdi_bank_matches([r.get('client_idno') for r in rows])
        out = []
        for r in rows:
            cid = int(float(r['client_id']))
            key = prop_reg.normalise_idno(r.get('client_idno'))
            out.append(prop_reg.shape_client(r, units=units.get(cid), bank=banked.get(key)))
        return out

    def search_property_clients(self, query: str, *, limit: int = 50,
                                unbanked_only: bool = False) -> list[dict[str, Any]]:
        """Search the property register by name, national ID or client number.

        `unbanked_only` narrows to the clients with no bank record - the acquisition
        list, and the reason this universe was opened up at all. It is applied AFTER
        the bank bridge is resolved, so it reflects the same match the row displays.
        """
        raw = (query or '').strip()
        like = f'%{raw.lower()}%'
        id_norm = prop_reg.normalise_idno(raw)
        id_guard = id_norm if len(id_norm) >= 5 else ''
        # Over-fetch when a filter runs after the query, so the page still fills.
        fetch = min(int(limit) * 4, 400) if unbanked_only else int(limit)
        if raw:
            rows = self._t.execute(
                self._REGISTER_SELECT +
                "WHERE client_name IS NOT NULL AND ("
                "  lower(TRIM(client_name)) LIKE ? "
                "  OR CAST(CAST(client_id AS BIGINT) AS varchar) = ? "
                "  OR (? <> '' AND UPPER(REGEXP_REPLACE(TRIM(client_idno), '[^A-Za-z0-9]', '')) LIKE ?)) "
                "LIMIT ?", (like, raw, id_guard, f'%{id_norm}%', fetch))
        else:
            rows = self._t.execute(
                self._REGISTER_SELECT + "WHERE client_name IS NOT NULL LIMIT ?", (fetch,))
        out = self._hfdi_decorate(rows)
        if unbanked_only:
            out = [c for c in out if not c['property_client']['bank_cust_id']]
        # Biggest holdings first: this is a sales list, not a directory.
        out.sort(key=lambda c: (-(c['property_client']['units_value'] or 0), c['name'] or ''))
        return out[:int(limit)]

    def get_property_client(self, client_id: int) -> dict[str, Any] | None:
        rows = self._t.execute(self._REGISTER_SELECT + "WHERE client_id = ? LIMIT 1",
                               (float(client_id),))
        if not rows:
            return None
        return self._hfdi_decorate(rows)[0]

    def property_client_coverage(self) -> dict[str, Any] | None:
        """How much of the property register the bank actually holds a relationship with.

        This is the number that justifies the page existing, so it is measured, not
        asserted - and it is measured over the register itself rather than over the
        property table, because a client with no unit yet is still a client. Cached
        for an hour: it is a full scan of both sides and it moves daily at most.
        """
        from django.core.cache import cache  # local: keeps the gateway import-light

        cached = cache.get('c360:hfdi:coverage')
        if cached is not None:
            return cached
        try:
            # Both sides are reduced to DISTINCT id sets BEFORE the join. A national
            # id can appear on several dim_customer rows, and joining the raw table
            # fans each client out into as many rows as it matched - which is how a
            # 4,843-client register first reported 8,300 clients here. Units are a
            # separate scalar for the same reason.
            rows = self._t.execute(
                "WITH h AS (SELECT DISTINCT client_id, TRIM(client_idno) idno "
                "             FROM delta.gold_db.hfdi_client_data WHERE client_name IS NOT NULL), "
                "     d AS (SELECT DISTINCT TRIM(customer_id_no) idno "
                "             FROM delta.gold_db.dim_customer "
                "            WHERE TRIM(COALESCE(customer_id_no, '')) <> '') "
                "SELECT count(*) total, "
                "       count(CASE WHEN d.idno IS NOT NULL THEN 1 END) banked, "
                "       count(CASE WHEN o.client_id IS NOT NULL THEN 1 END) owners, "
                "       (SELECT count(DISTINCT unit_id) "
                "          FROM delta.gold_db.rpt_c360_customer_property "
                "         WHERE unit_id IS NOT NULL) units "
                "  FROM h LEFT JOIN d ON d.idno = h.idno "
                "  LEFT JOIN (SELECT DISTINCT client_id FROM delta.gold_db.rpt_c360_customer_property "
                "              WHERE unit_id IS NOT NULL) o ON o.client_id = h.client_id")
        except Exception:
            logger.warning('%s coverage query failed', brand.PROPERTY, exc_info=True)
            return None
        if not rows:
            return None
        r = rows[0]
        total = int(r.get('total') or 0)
        banked = int(r.get('banked') or 0)
        out = {
            'total': total,
            'banked': banked,
            'unbanked': max(total - banked, 0),
            'owners': int(r.get('owners') or 0),
            'units': int(r.get('units') or 0),
            'note': prop_reg.coverage_note(total, banked),
        }
        cache.set('c360:hfdi:coverage', out, 3600)
        return out

    #: Digits kept when comparing two phone numbers. Nine is a Kenyan subscriber
    #: number without its country or trunk prefix, so +254 7xx, 254 7xx and 07xx all
    #: reduce to the same key.
    _PHONE_KEY_DIGITS = 9

    @classmethod
    def _phone_key(cls, value) -> str:
        """Last nine digits of a phone number, or '' when there aren't nine."""
        digits = _re.sub(r'[^0-9]', '', str(value or ''))
        return digits[-cls._PHONE_KEY_DIGITS:] if len(digits) >= cls._PHONE_KEY_DIGITS else ''

    @classmethod
    def _sql_name_key(cls, column: str) -> str:
        """``_name_key`` in Trino, so both sides of a join reduce identically.

        No capture groups: every run of non-alphanumerics becomes one space and the
        ends are padded, which makes a plain REPLACE of ' LIMITED ' with ' LTD ' a
        word-boundary fold. LIMITEDACCESS keeps its spelling because there is no space
        inside it. This mirrors the Python side, which splits on non-alphanumerics and
        maps whole words, so the two cannot drift apart.
        """
        expr = (f"CONCAT(' ', REGEXP_REPLACE(UPPER(COALESCE({column}, '')), "
                f"'[^A-Z0-9]+', ' '), ' ')")
        for word, repl in cls._NAME_SYNONYMS.items():
            replacement = f" {repl} " if repl else ' '
            expr = f"REPLACE({expr}, ' {word} ', '{replacement}')"
        return f"REGEXP_REPLACE({expr}, '[^A-Z0-9]', '')"

    @staticmethod
    def _sql_phone_key(column: str) -> str:
        """The same reduction, in Trino, so both sides of a join agree."""
        digits = f"REGEXP_REPLACE({column}, '[^0-9]', '')"
        return (f"CASE WHEN LENGTH({digits}) >= 9 "
                f"THEN SUBSTR({digits}, LENGTH({digits}) - 8) ELSE '' END")

    def _insurance_clients(self, nid: str, phone_key: str) -> dict[str, str]:
        """HFBI client numbers belonging to this bank customer, and how each matched.

        Returns {client_no: 'national ID' | 'phone'}. The ID match is preferred when a
        client is reachable both ways, because it is the stronger claim.
        """
        clauses, params = [], []
        if nid:
            clauses.append('TRIM(h.idno) = ?')
            params.append(nid)
        if phone_key:
            clauses.append(f'{self._sql_phone_key("h.phone")} = ?')
            params.append(phone_key)
        if not clauses:
            return {}
        rows = self._t.execute(
            "SELECT DISTINCT TRIM(h.client_no) client_no, TRIM(h.idno) idno, h.phone, "
            "TRIM(h.name) name "
            "FROM delta.gold_db.hfbi_customer_data h "
            f"WHERE TRIM(COALESCE(h.client_no, '')) <> '' AND ({' OR '.join(clauses)})",
            tuple(params))
        out: dict[str, str] = {}
        for r in rows:
            client_no = self._clean(r.get('client_no'))
            if not client_no:
                continue
            by_id = bool(nid) and (self._clean(r.get('idno')) or '') == nid
            # An ID match outranks a phone match for the same client.
            if by_id or client_no not in out:
                out[client_no] = 'national ID' if by_id else 'phone'
            name = self._clean(r.get('name'))
            if name:
                self._client_names[client_no] = name
        return out

    #: A normalised name shorter than this is not distinctive enough to bridge on,
    #: however unique it happens to be in today's data.
    _NAME_BRIDGE_MIN_CHARS = 6

    #: client_no -> the insurance register's spelling of that client's name, filled in
    #: as clients are resolved. Receipts are keyed by name, not by client number, so
    #: this is how a client resolved by ID or phone can then be found in the receipts.
    _client_names: dict[str, str]

    #: Words that mean the same thing in a company name. Folded BEFORE the key is
    #: built, and applied to both sides of every comparison, so 'Rajaa Stones Limited'
    #: and 'RAJAA STONES LTD' reduce to one key. Systemic rather than cosmetic:
    #: receipts end in LTD 1,669 times and LIMITED 1,636.
    _NAME_SYNONYMS = {
        'LIMITED': 'LTD',
        'COMPANY': 'CO',
        'INCORPORATED': 'INC',
        'CORPORATION': 'CORP',
        'AND': '',
    }

    @classmethod
    def _name_key(cls, value) -> str:
        """A comparable key for a party name.

        'Rajaa Stones Limited' and 'RAJAA STONES LTD' both become 'RAJAASTONESLTD'.
        Word-level folding first, then punctuation and case are dropped, so a synonym
        is only replaced when it IS a word rather than part of one.
        """
        words = _re.split(r'[^A-Za-z0-9]+', str(value or '').upper())
        folded = [cls._NAME_SYNONYMS.get(w, w) for w in words if w]
        return ''.join(folded)

    def _insurance_client_by_name(self, full_name: str) -> str | None:
        """The insurance client whose name matches this customer's, when that name
        can only mean one party.

        Returns None unless the normalised name appears exactly once in the insurance
        register AND exactly once in the bank's customer master. Either duplicate
        makes the match a guess, and a guess is not worth putting someone else's
        policies on a customer's page.
        """
        key = self._name_key(full_name)
        if len(key) < self._NAME_BRIDGE_MIN_CHARS:
            return None
        norm_h = self._sql_name_key('name')
        hrows = self._t.execute(
            f"SELECT count(*) n, MIN(TRIM(client_no)) client_no, MIN(TRIM(name)) name "
            f"FROM delta.gold_db.hfbi_customer_data WHERE {norm_h} = ?", (key,))
        if not hrows or int(hrows[0].get('n') or 0) != 1:
            return None
        client_no = self._clean(hrows[0].get('client_no'))
        if not client_no:
            return None
        # Remember the register's spelling: receipts are keyed by name, not by client
        # number, so without this a client resolved here has no way into them.
        name = self._clean(hrows[0].get('name'))
        if name:
            self._client_names[client_no] = name
        norm_d = self._sql_name_key('full_name')
        drows = self._t.execute(
            f"SELECT count(*) n FROM delta.gold_db.dim_customer WHERE {norm_d} = ?", (key,))
        if not drows or int(drows[0].get('n') or 0) != 1:
            # The name means more than one bank customer, so it cannot identify this
            # one. SUSAN WANJIKU KARIUKI (five records) lands here.
            return None
        return client_no

    def _insurance_receipts(self, names: list[str]) -> dict[str, Any] | None:
        """Premium receipts for these client names, and the risknotes they paid against.

        receipt_client is a free-text NAME, so this matches on the normalised form.
        That is the same trade the rest of the insurance bridging makes, and it is
        made here on a name we already resolved through a stronger bridge rather than
        on the customer's own name, so it does not widen the blast radius.
        """
        keys = [k for k in {self._name_key(n) for n in names if n}
                if len(k) >= self._NAME_BRIDGE_MIN_CHARS]
        if not keys:
            return None
        placeholders = ','.join(['?'] * len(keys))
        norm = self._sql_name_key('receipt_client')
        # Count and risknotes only. receipt_date is NULL on all 31,975 rows, and
        # receipt_amount is negative on 24,693 of them with no event_type and no
        # documented sign convention - Rajaa Stones alone nets to -3,020,025 across
        # 136 receipts. A count proves the relationship; a negative "premiums paid"
        # would misinform, and an absolute value would assert a convention nobody has
        # confirmed. Both questions are with the insurance team.
        rows = self._t.execute(
            f"SELECT count(*) receipts, "
            f"ARRAY_AGG(DISTINCT TRIM(COALESCE(receipt_risknote_no, ''))) risknotes "
            f"FROM delta.gold_db.hfbi_receipt_data WHERE {norm} IN ({placeholders})",
            tuple(keys))
        if not rows or not int(rows[0].get('receipts') or 0):
            return None
        r = rows[0]
        risknotes = sorted({n for n in (r.get('risknotes') or []) if n})
        return {
            'receipts': int(r.get('receipts') or 0),
            'risknotes': risknotes,
            # Stated so the panel never implies it is withholding a figure it has.
            'amounts_available': False,
            'dates_available': False,
        }

    # --- the insurance-client universe (see c360/insurance_register.py) -----

    _INS_SELECT = (
        "SELECT TRIM(client_no) client_no, TRIM(name) name, TRIM(idno) idno, "
        "TRIM(phone) phone, TRIM(email) email, TRIM(pin) pin, TRIM(branch) branch, "
        "TRIM(occupation) occupation, TRIM(risk_manager) risk_manager, "
        "TRIM(sales_person) sales_person, TRIM(gender) gender, TRIM(address) address "
        "FROM delta.gold_db.hfbi_customer_data "
    )

    def _ins_policy_totals(self, client_nos: list[str]) -> dict[str, dict[str, Any]]:
        """Policy count, active count and money per insurance client.

        Deduped on the natural key rather than the policy number, for the same reason
        get_bancassurance is: policy_policy_no is blank on 98% of rows.
        """
        if not client_nos:
            return {}
        marks = ','.join(['?'] * len(client_nos))
        rows = self._t.execute(
            "SELECT client_no, count(*) total, "
            "count(CASE WHEN status = 'active' THEN 1 END) active, "
            "SUM(premium) premium, SUM(insured) insured FROM ("
            "  SELECT TRIM(s.policy_client_no) client_no, TRIM(s.product) product, "
            "         TRIM(s.policy_start_date) sd, TRIM(s.policy_end_date) ed, "
            "         COALESCE(s.policy_sum_insured, 0) insured, "
            "         MAX(LOWER(TRIM(s.status))) status, "
            "         MAX(s.policy_total_premium) premium "
            "    FROM delta.gold_db.rpt_c360_customer_policies_summary s "
            f"   WHERE TRIM(s.policy_client_no) IN ({marks}) "
            "   GROUP BY 1,2,3,4,5) p GROUP BY client_no", tuple(client_nos))
        return {self._clean(r['client_no']): {
            'total': int(r.get('total') or 0), 'active': int(r.get('active') or 0),
            'premium': float(r.get('premium') or 0), 'sum_insured': float(r.get('insured') or 0),
        } for r in rows if self._clean(r.get('client_no'))}

    def _ins_receipt_counts(self, names: list[str]) -> dict[str, int]:
        """Premium receipts per client, keyed by normalised name."""
        keys = [k for k in {self._name_key(n) for n in names if n} if k]
        if not keys:
            return {}
        marks = ','.join(['?'] * len(keys))
        norm = self._sql_name_key('receipt_client')
        rows = self._t.execute(
            f"SELECT {norm} k, count(*) n FROM delta.gold_db.hfbi_receipt_data "
            f"WHERE {norm} IN ({marks}) GROUP BY 1", tuple(keys))
        return {str(r['k']): int(r.get('n') or 0) for r in rows if r.get('k')}

    def _ins_bank_matches(self, rows: list[dict]) -> dict[str, dict[str, Any]]:
        """Which of these insurance clients also bank with us, by national ID.

        ID ONLY, deliberately. An IN-list on a phone key computed from
        primary_mobile_no cannot use table statistics, so it scans the whole customer
        master once per page render - which is what made this page time out and take
        the gunicorn worker with it. On a list, the national ID answers the question
        the column is asking; the single-customer page keeps the fuller ID-or-phone
        bridge, where it is one lookup rather than fifty.

        The name bridge is not used here either. On one page it is a last resort with
        a uniqueness guard; across a list it would be a thousand guesses, and a wrong
        one attaches a stranger's bank record to a row.
        """
        idnos = sorted({(r.get('idno') or '').strip() for r in rows
                        if (r.get('idno') or '').strip()})
        if not idnos:
            return {}
        marks = ','.join(['?'] * len(idnos))
        drows = self._t.execute(
            "SELECT CAST(customer_id AS BIGINT) id, TRIM(customer_id_no) idno, "
            "customer_segment, employer, fk_bankemployeeid "
            f"FROM delta.gold_db.dim_customer WHERE TRIM(customer_id_no) IN ({marks})",
            tuple(idnos))
        by_id: dict[str, dict[str, Any]] = {}
        for r in drows:
            idno = self._clean(r.get('idno'))
            if not idno or idno in by_id:
                continue
            by_id[idno] = {
                'cust_id': str(r['id']),
                'is_staff': is_staff_from_fields(
                    employer=r.get('employer'), segment=r.get('customer_segment'),
                    bank_employee_id=r.get('fk_bankemployeeid')),
            }
        out: dict[str, dict[str, Any]] = {}
        for r in rows:
            match = by_id.get((r.get('idno') or '').strip())
            if match:
                out[self._clean(r.get('client_no')) or ''] = match
        return out

    def _ins_decorate(self, rows: list[dict]) -> list[dict[str, Any]]:
        if not rows:
            return []
        client_nos = [self._clean(r.get('client_no')) for r in rows]
        client_nos = [c for c in client_nos if c]
        policies = self._ins_policy_totals(client_nos)
        receipts = self._ins_receipt_counts([r.get('name') for r in rows])
        banked = self._ins_bank_matches(rows)
        out = []
        for r in rows:
            cn = self._clean(r.get('client_no')) or ''
            out.append(ins_reg.shape_client(
                r, policies=policies.get(cn), bank=banked.get(cn),
                receipts={'receipts': receipts.get(self._name_key(r.get('name')), 0)},
                name_key=self._name_key(r.get('name'))))
        return out

    def _receipt_only_clients(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Clients who appear ONLY in the premium receipts.

        About 454 of the 6,392 names in the receipts have no row on the register.
        Michael Njau Kimani is one of them, with 29 receipts and no client record, and
        he has now come back 'missing' twice. Excluding this group would make that
        three times.
        """
        raw = (query or '').strip()
        if not raw:
            return []
        norm = self._sql_name_key('receipt_client')
        reg = self._sql_name_key('name')
        rows = self._t.execute(
            f"SELECT MAX(TRIM(r.receipt_client)) name, {norm} k, count(*) n "
            "FROM delta.gold_db.hfbi_receipt_data r "
            f"WHERE LOWER(TRIM(r.receipt_client)) LIKE ? AND {norm} NOT IN "
            f"(SELECT {reg} FROM delta.gold_db.hfbi_customer_data) "
            f"GROUP BY {norm} LIMIT ?", (f'%{raw.lower()}%', int(limit)))
        out = []
        for r in rows:
            out.append(ins_reg.shape_client(
                {'name': self._clean(r.get('name'))},
                receipts={'receipts': int(r.get('n') or 0)},
                name_key=str(r.get('k') or '')))
        return out

    def search_insurance_clients(self, query: str, *, limit: int = 50,
                                 unbanked_only: bool = False) -> list[dict[str, Any]]:
        raw = (query or '').strip()
        fetch = min(int(limit) * 4, 400) if unbanked_only else int(limit)
        if raw:
            rows = self._t.execute(
                self._INS_SELECT +
                "WHERE name IS NOT NULL AND (lower(TRIM(name)) LIKE ? "
                "OR UPPER(TRIM(client_no)) = ? "
                "OR TRIM(COALESCE(idno,'')) = ?) LIMIT ?",
                (f'%{raw.lower()}%', raw.upper(), raw, fetch))
        else:
            rows = self._t.execute(self._INS_SELECT + "WHERE name IS NOT NULL LIMIT ?",
                                   (fetch,))
        out = self._ins_decorate(rows)
        if raw:
            # Only searched, never listed wholesale: without a client_no there is no
            # stable way to page them.
            out.extend(self._receipt_only_clients(raw, max(10, int(limit) // 4)))
        if unbanked_only:
            out = [c for c in out if not c['insurance']['bank_cust_id']]
        out.sort(key=lambda c: (-(c['insurance']['premium'] or 0),
                                -(c['insurance']['receipts'] or 0), c['name'] or ''))
        return out[:int(limit)]

    def get_insurance_client(self, key: str) -> dict[str, Any] | None:
        """One insurance client by client_no, or by normalised name when the register
        never got them."""
        rows = self._t.execute(self._INS_SELECT + "WHERE UPPER(TRIM(client_no)) = ? LIMIT 1",
                               (str(key).upper(),))
        if rows:
            return self._ins_decorate(rows)[0]
        norm = self._sql_name_key('receipt_client')
        rrows = self._t.execute(
            f"SELECT MAX(TRIM(receipt_client)) name, count(*) n "
            f"FROM delta.gold_db.hfbi_receipt_data WHERE {norm} = ?", (str(key).upper(),))
        if not rrows or not int(rrows[0].get('n') or 0):
            return None
        return ins_reg.shape_client(
            {'name': self._clean(rrows[0].get('name'))},
            receipts={'receipts': int(rrows[0].get('n') or 0)},
            name_key=str(key).upper())

    def insurance_client_coverage(self) -> dict[str, Any] | None:
        """Register size, how many also bank with us, and how many exist only as
        receipts.

        Each part is a separate bounded query and each is allowed to fail on its own.
        An earlier version asked one question that joined every insurance client to
        every bank customer on a computed phone key; it outran the worker timeout and
        took the page down. Coverage is a sentence at the top of a list - it must
        never be the reason the list does not render.
        """
        from django.core.cache import cache

        cached = cache.get('c360:ins:coverage')
        if cached is not None:
            return cached

        try:
            rows = self._t.execute(
                "SELECT count(DISTINCT TRIM(client_no)) n "
                "FROM delta.gold_db.hfbi_customer_data WHERE name IS NOT NULL")
            total = int(rows[0].get('n') or 0) if rows else 0
        except Exception:
            logger.warning('insurance coverage: register count failed', exc_info=True)
            return None
        if not total:
            return None

        receipts_only = self._ins_receipts_only_count()
        banked = self._ins_banked_by_id()

        if banked is None:
            note = (f'{total:,} clients on the insurance register.'
                    + (f' {receipts_only:,} more appear only in the premium receipts, '
                       f'with no client record at all.' if receipts_only else ''))
        else:
            note = ins_reg.coverage_note(total, banked, receipts_only or 0)
            # Say what the number means. It is the ID bridge alone, because the
            # phone half cannot be run across the whole register on a page load.
            note += ' Matched on national ID; a client sharing only a phone number is not counted.'

        out = {
            'total': total,
            'banked': banked,
            'unbanked': (max(total - banked, 0) if banked is not None else None),
            'receipts_only': receipts_only or 0,
            'note': note,
        }
        cache.set('c360:ins:coverage', out, 3600)
        return out

    def _ins_receipts_only_count(self) -> int | None:
        """Names in the premium receipts with no row on the register."""
        try:
            rec = self._sql_name_key('receipt_client')
            reg = self._sql_name_key('name')
            rows = self._t.execute(
                "SELECT count(*) n FROM ("
                f"  SELECT DISTINCT {rec} k FROM delta.gold_db.hfbi_receipt_data "
                "   WHERE TRIM(COALESCE(receipt_client, '')) <> '') x "
                f"WHERE x.k NOT IN (SELECT {reg} FROM delta.gold_db.hfbi_customer_data "
                "                    WHERE name IS NOT NULL)")
        except Exception:
            logger.warning('insurance coverage: receipts-only count failed', exc_info=True)
            return None
        return int(rows[0].get('n') or 0) if rows else None

    def _ins_banked_by_id(self) -> int | None:
        """Insurance clients whose national ID is also a bank customer's.

        An equality join between two DISTINCT id sets, which the engine can hash. The
        phone half of the bridge is deliberately absent: computing a phone key on both
        sides of 1.1 million rows is a scan, and it is what made this page time out.
        """
        try:
            rows = self._t.execute(
                "WITH h AS (SELECT DISTINCT TRIM(idno) idno "
                "             FROM delta.gold_db.hfbi_customer_data "
                "            WHERE name IS NOT NULL AND LENGTH(TRIM(COALESCE(idno, ''))) >= 5), "
                "     d AS (SELECT DISTINCT TRIM(customer_id_no) idno "
                "             FROM delta.gold_db.dim_customer "
                "            WHERE LENGTH(TRIM(COALESCE(customer_id_no, ''))) >= 5) "
                "SELECT count(*) n FROM h JOIN d ON d.idno = h.idno")
        except Exception:
            logger.warning('insurance coverage: banked count failed', exc_info=True)
            return None
        return int(rows[0].get('n') or 0) if rows else None

    def _safe_claims(self, risknotes: list[str]) -> dict[str, Any] | None:
        """Claims, or nothing. A rare-event probe must never take the panel down."""
        try:
            return self._insurance_claims(risknotes)
        except Exception:
            logger.warning('insurance claims lookup failed', exc_info=True)
            return None

    def _insurance_claims(self, risknotes: list[str]) -> dict[str, Any] | None:
        """Claims registered against this client's risknotes.

        Claims are rare - 223 across the whole book - and that is the point. A
        customer who has claimed is in a different conversation from one who has not,
        and the worst version of this page is one that lets an RM walk into a renewal
        without knowing there is an open claim.

        No money total: 181 of the 223 carry no estimated loss, so a sum would
        describe a fifth of them and be read as all of them.
        """
        notes = [n for n in dict.fromkeys(risknotes) if n][:60]
        if not notes:
            return None
        marks = ','.join(['?'] * len(notes))
        rows = self._t.execute(
            "SELECT TRIM(claim_status) status, count(*) n, "
            "MAX(TRIM(claim_incident_date)) latest_incident "
            "FROM delta.gold_db.hfbi_claim_data "
            f"WHERE TRIM(claim_risknote_no) IN ({marks}) GROUP BY 1", tuple(notes))
        if not rows:
            return None
        by_status = {}
        total = 0
        latest = None
        for r in rows:
            label = (self._clean(r.get('status')) or 'Unknown').title()
            n = int(r.get('n') or 0)
            by_status[label] = by_status.get(label, 0) + n
            total += n
            inc = self._safe_date(r.get('latest_incident'))
            if inc and (latest is None or str(inc) > str(latest)):
                latest = inc
        if not total:
            return None
        return {
            'total': total,
            'by_status': by_status,
            'latest_incident': latest,
            # Stated so the panel never implies it is withholding a figure it has.
            'amounts_available': False,
        }

    def get_bancassurance(self, cust_id, period):
        """Insurance policies held by a bank customer.

        Bridged three ways, because the one bridge this used to rely on is empty far
        more often than it is populated:

        1. ``policy_client_no`` -> ``hfbi_customer_data.client_no``, reached from the
           customer's national ID. The register carries identifiers the policy
           summary does not.
        2. The same, reached by phone, which recovers clients whose register row has
           no national ID - 76% of policy rows have none.
        3. The policy summary's own ``idno``, kept so nothing that used to resolve
           stops resolving.

        Every policy says which bridge found it. A phone match is indicative rather
        than confirmed: a shared handset can put two people on one number, and the
        panel must not present that as a certainty.

        Deduped by policy number (the summary repeats a policy). ``period`` is unused:
        a policy book is a current holdings snapshot, not a windowed activity feed.
        """
        cid = self._cid(cust_id)
        if cid is None:
            return None
        idrows = self._t.execute(
            "SELECT TRIM(customer_id_no) nid, primary_mobile_no mobile, mobile_tel2 alt, "
            "TRIM(full_name) full_name "
            "FROM delta.gold_db.dim_customer WHERE customer_id=? LIMIT 1", (cid,))
        if not idrows:
            return None
        row = idrows[0]
        nid = (self._clean(row.get('nid')) or '')
        # Same guard as before: a blank or placeholder id would match many clients.
        if len(nid) < 5 or nid.upper() == 'NULL' or not any(ch.isdigit() for ch in nid):
            nid = ''
        phone_key = self._phone_key(row.get('mobile')) or self._phone_key(row.get('alt'))

        try:
            clients = self._insurance_clients(nid, phone_key) if (nid or phone_key) else {}
        except Exception:
            logger.warning('bancassurance: client lookup failed for %s', cust_id, exc_info=True)
            clients = {}

        # Last resort, and only when the stronger bridges found nothing: a name that
        # is unique on both sides. Rajaa Stones Limited has no national ID and no
        # phone on either side, so this is the only link that exists for them.
        if not clients:
            try:
                by_name = self._insurance_client_by_name(row.get('full_name'))
            except Exception:
                logger.warning('bancassurance: name lookup failed for %s', cust_id, exc_info=True)
                by_name = None
            if by_name:
                clients = {by_name: 'name'}

        if not clients and not nid:
            return None

        clauses, params = [], []
        if clients:
            placeholders = ','.join(['?'] * len(clients))
            clauses.append(f'TRIM(s.policy_client_no) IN ({placeholders})')
            params.extend(clients)
        if nid:
            clauses.append('TRIM(s.idno) = ?')
            params.append(nid)
        if not clauses:
            return None

        # Deduped on the natural key, NOT on the policy number. policy_policy_no is
        # blank on 98% of rows (57,188 of 58,504), so filtering or grouping by it
        # discards almost the whole book - which is precisely why a customer with four
        # annual renewals on file was shown nothing at all.
        # Premium receipts for the clients we resolved. This is the most complete
        # record of an insurance relationship the warehouse holds: 5,956 register
        # clients have no policy row at all, and 31,975 receipts do.
        receipts = None
        try:
            names = [self._client_names.get(c) for c in clients]
            receipts = self._insurance_receipts([n for n in names if n])
        except Exception:
            logger.warning('bancassurance: receipt lookup failed for %s', cust_id, exc_info=True)

        # Every receipt carries a risknote, and the policy table is keyed by the same
        # risknote - which reaches policies policy_client_no cannot. Bounded, because
        # a long-standing client can have dozens.
        risknotes = (receipts or {}).get('risknotes') or []
        if risknotes:
            marks = ','.join(['?'] * len(risknotes[:60]))
            clauses.append(
                "TRIM(s.policy_client_no) IN (SELECT DISTINCT TRIM(pd.policy_client_no) "
                "FROM delta.gold_db.hfbi_policy_data pd "
                f"WHERE TRIM(pd.policy_risknote_no) IN ({marks}))")
            params.extend(risknotes[:60])

        rows = self._t.execute(
            "SELECT TRIM(s.policy_client_no) client_no, TRIM(s.product) product, "
            "TRIM(s.policy_start_date) start_dt, TRIM(s.policy_end_date) end_dt, "
            "COALESCE(s.policy_sum_insured, 0) insured, "
            "MAX(TRIM(COALESCE(s.policy_policy_no, ''))) pol, "
            "MAX(s.status) status, MAX(s.policy_total_premium) premium, "
            "MAX(TRIM(COALESCE(s.idno, ''))) idno "
            "FROM delta.gold_db.rpt_c360_customer_policies_summary s "
            f"WHERE ({' OR '.join(clauses)}) "
            "GROUP BY TRIM(s.policy_client_no), TRIM(s.product), "
            "TRIM(s.policy_start_date), TRIM(s.policy_end_date), "
            "COALESCE(s.policy_sum_insured, 0)", tuple(params))
        if not rows:
            # No policy survived the extract - but premium receipts prove the
            # relationship exists, and telling an RM "no policies" while the customer
            # has paid 29 premiums is worse than saying what we actually know.
            if receipts:
                return {
                    'policies': [],
                    'active': 0,
                    'expired': 0,
                    'unnumbered': 0,
                    'phone_matched': 0,
                    'name_matched': 0,
                    'receipts': receipts,
                    'claims': self._safe_claims(receipts.get('risknotes') or []),
                    'match_note': (
                        'No policy record survives in the warehouse for this customer, but '
                        f"{receipts['receipts']} premium receipts do. The policy detail is "
                        'missing from the insurance extract, not from the relationship.'),
                }
            # Distinguish a genuinely policy-free customer from an empty or
            # unreachable source (see get_properties).
            if not self._source_has_rows('delta.gold_db.rpt_c360_customer_policies_summary'):
                raise LiveDataNotReady(
                    'bancassurance source (rpt_c360_customer_policies_summary) is empty '
                    'or unreachable')
            return None

        policies = []
        for r in rows:
            client_no = self._clean(r.get('client_no'))
            # The summary's own id is the strongest claim; otherwise credit whichever
            # bridge found the client.
            if nid and (self._clean(r.get('idno')) or '') == nid:
                matched_by = 'national ID'
            else:
                matched_by = clients.get(client_no or '', 'national ID')
            status = (self._clean(r.get('status')) or 'unknown').title()
            policies.append({
                # Shown when the feed carries one (1,316 rows of 58,504 do). Null
                # otherwise, so the panel can say the number is missing rather than
                # print a placeholder that looks like a reference.
                'policy': self._clean(r.get('pol')),
                # product is null on this customer's rows and many others, so the
                # fallback has to be a description, not a guess at cover type.
                'product': self._clean(r.get('product')) or 'Insurance policy',
                'premium': round(float(r.get('premium') or 0)),
                'sum_insured': round(float(r.get('insured') or 0)),
                'status': status,
                'start': self._safe_date(r.get('start_dt')),
                'end': self._safe_date(r.get('end_dt')),
                'matched_by': matched_by,
            })
        # Active first, then newest, then by premium. Most of the book is expired
        # (55,093 of 58,504 rows), so premium alone buries what is actually in force,
        # and a customer with four annual renewals should read newest-first.
        policies.sort(key=lambda p: (p['status'].lower() != 'active',
                                     -_date_sort_key(p['end']), -p['premium']))
        phone_only = sum(1 for p in policies if p['matched_by'] == 'phone')
        name_only = sum(1 for p in policies if p['matched_by'] == 'name')
        return {
            'policies': policies,
            'active': sum(1 for p in policies if p['status'].lower() == 'active'),
            # Said out loud: a book that is entirely expired is a retention
            # conversation, and it should not look like an empty panel.
            'expired': sum(1 for p in policies if p['status'].lower() != 'active'),
            'unnumbered': sum(1 for p in policies if not p['policy']),
            # Never a silent fuzzy match: when any policy was reached only by phone,
            # the panel says so and says how many.
            'phone_matched': phone_only,
            'name_matched': name_only,
            # Premiums actually paid, which is a harder fact than a policy record and
            # often the only one that survived.
            'receipts': receipts,
            # Rare and high-salience: an open claim changes the conversation.
            'claims': self._safe_claims((receipts or {}).get('risknotes') or []),
            'match_note': _match_note(phone_only, name_only),
        }

    def get_property_leads(self, cust_id):
        """Property-sales CRM (property-register leads + follow-ups) for a bank customer, matched by
        PHONE — the lead export carries no customer/national id, so this is the only
        bridge and it is fuzzy (the caller tags it 'matched by phone'). Only resolves for
        the ~5.8k leads whose phone is also a bank customer's. Returns None when the
        customer's mobile is blank/short or matches no lead. Summarised (furthest stage +
        follow-up engagement), not a raw event dump — the source dates/campaigns are
        unreliably null. Never raises: a probe failure just yields no panel."""
        cid = self._cid(cust_id)
        if cid is None:
            return None
        mrow = self._t.execute(
            "SELECT regexp_replace(TRIM(primary_mobile_no),'[^0-9]','') p "
            "FROM delta.gold_db.dim_customer WHERE customer_id=? LIMIT 1", (cid,))
        phone = (mrow[0]['p'] if mrow else None) or ''
        # A full 9-digit subscriber number is required — a blank/short phone would
        # over-match every lead with a blank phone (the customer-2 false-match trap).
        if len(phone) < 9:
            return None
        last9 = phone[-9:]
        leads = self._t.execute(
            "SELECT lead_id, MAX(TRIM(lead_state)) state "
            "FROM delta.gold_db.hfdi_lead_data "
            "WHERE phone IS NOT NULL AND length(regexp_replace(CAST(phone AS varchar),'[^0-9]','')) >= 9 "
            "AND substr(regexp_replace(CAST(phone AS varchar),'[^0-9]',''), "
            "  length(regexp_replace(CAST(phone AS varchar),'[^0-9]',''))-8) = ? "
            "GROUP BY lead_id", (last9,))
        if not leads:
            return None
        states = [self._clean(l.get('state')) for l in leads]
        lead_ids = [int(l['lead_id']) for l in leads if l.get('lead_id') is not None]
        n_fup, ok_fup = 0, 0
        if lead_ids:
            inlist = ','.join(str(i) for i in lead_ids)
            frow = self._t.execute(
                "SELECT COUNT(*) n, "
                "SUM(CASE WHEN upper(TRIM(followup_success)) IN ('SUCCESSFUL','YES') THEN 1 ELSE 0 END) ok "
                f"FROM delta.gold_db.hfdi_lead_followup_data WHERE lead_id IN ({inlist})")
            if frow:
                n_fup = int(frow[0].get('n') or 0)
                ok_fup = int(frow[0].get('ok') or 0)
        return crm_shape.shape_property_leads(states, n_fup, ok_fup)

    def get_insurance_crm(self, cust_id):
        """Insurance CRM profile (HFBI customer record) for a bank customer, bridged by
        national ID (dim_customer.customer_id_no = hfbi_customer_data.idno) — a clean,
        exact join. The HFBI *leads* table is empty, so this is the servicing view:
        insurance RM/agent, occupation, branch. Returns None when the customer has no
        HFBI record or it carries nothing worth showing. Never raises."""
        cid = self._cid(cust_id)
        if cid is None:
            return None
        nid_rows = self._t.execute(
            "SELECT TRIM(customer_id_no) nid FROM delta.gold_db.dim_customer WHERE customer_id=? LIMIT 1", (cid,))
        nid = (nid_rows[0]['nid'] if nid_rows else None) or ''
        if len(nid) < 5 or nid.upper() == 'NULL' or not any(ch.isdigit() for ch in nid):
            return None
        rows = self._t.execute(
            "SELECT MAX(TRIM(risk_manager)) risk_manager, MAX(TRIM(sales_person)) sales_person, "
            "MAX(TRIM(occupation)) occupation, MAX(TRIM(branch)) branch, MAX(TRIM(location)) location "
            "FROM delta.gold_db.hfbi_customer_data WHERE TRIM(idno) = ?", (nid,))
        if not rows:
            return None
        return crm_shape.shape_insurance_crm(rows[0])

    def get_lending_health(self, cust_id):
        """Credit standing (NPL / watch classification + IFRS impairment) and collateral
        held, for the header 'Credit standing & collateral' panel. Delinquency joins
        npl_accounts / pre_npl_accounts on the numeric prefix of their suffixed cust_id
        (e.g. '1125644-8' -> 1125644 = dim_customer.customer_id). Collateral joins the
        clean customer_id; its rows repeat, so we report distinct TYPES held (values are
        blank upstream). Returns None when the customer has neither. Never raises.
        Display-only — this does NOT feed the recommendation risk gate."""
        cid = self._cid(cust_id)
        if cid is None:
            return None

        # Delinquency: the customer's classified NPL accounts (worst wins) + watch list.
        npl = self._t.execute(
            "SELECT TRIM(classification) classification, ifrs_impairement impairment, "
            "CAST(month AS varchar) month FROM delta.gold_db.npl_accounts "
            "WHERE TRY_CAST(SPLIT_PART(cust_id,'-',1) AS BIGINT) = ?", (cid,))
        on_watch = False
        if not npl:
            wrows = self._t.execute(
                "SELECT COUNT(*) n FROM delta.gold_db.pre_npl_accounts "
                "WHERE TRY_CAST(SPLIT_PART(cust_id,'-',1) AS BIGINT) = ? "
                "AND UPPER(TRIM(classification)) = 'WATCH'", (cid,))
            on_watch = bool(wrows and int(wrows[0].get('n') or 0) > 0)
        month = None
        npl_rows = []
        for r in npl:
            npl_rows.append({'classification': self._clean(r.get('classification')),
                             'impairment': r.get('impairment')})
            month = month or self._clean(r.get('month'))
        delinquency = lending_shape.shape_delinquency(npl_rows, on_watch, month)

        # Collateral: distinct types held (rows repeat, so DISTINCT-count per type).
        crows = self._t.execute(
            "SELECT TRIM(collateral_type) collateral_type, COUNT(*) count "
            "FROM delta.gold_db.collateral WHERE customer_id = ? "
            "AND collateral_type IS NOT NULL AND TRIM(collateral_type) <> '' "
            "GROUP BY TRIM(collateral_type)", (cid,))
        collateral = lending_shape.shape_collateral(
            [{'type': r.get('collateral_type'), 'count': r.get('count')} for r in crows])

        if not delinquency and not collateral:
            return None
        return {'delinquency': delinquency, 'collateral': collateral}
