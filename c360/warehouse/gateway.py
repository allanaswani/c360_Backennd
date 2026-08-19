"""The warehouse gateway — Customer 360's semantic data-access boundary.

Rather than sprinkling SQL through views, every query the app needs is a named
method here. Two implementations satisfy this interface:

* ``MockWarehouse``  — seeded, in-process preview data (default).
* ``TrinoWarehouse`` — the real repositories; each method holds the documented
  SQL against the verified tables from architecture §7A, executed through the
  injected ``TrinoConnector``. Not run until credentials land.

Swapping between them is the whole point of the seam: services and API never know
which one they hold, so wiring live data touches zero frontend code and no service
logic. Records are plain dicts (already snake_cased / normalised) so the naming
inconsistency across the estate — ``CUST_ID`` / ``cust_cif`` / ``customer_id`` —
is absorbed here and never leaks to the client.
"""
from __future__ import annotations

import abc
from datetime import date
from typing import Any

from .periods import ResolvedPeriod


class WarehouseGateway(abc.ABC):
    # --- anchors ---------------------------------------------------------
    @abc.abstractmethod
    def as_of_date(self) -> date:
        """Last closed business date (``bank_parameters.PREV_TRX_DATE``)."""

    # --- identity & scope ------------------------------------------------
    @abc.abstractmethod
    def get_customer(self, cust_id: str) -> dict[str, Any] | None:
        """Identity + allocation + risk/KYC placeholders for one customer. Every record
        carries ``is_staff`` — True when the customer is an HF employee (see
        ``rbac/staff.py``); the query layer hides those from non-admins."""

    @abc.abstractmethod
    def search_customers(
        self, query: str, *, sales_codes: list[str] | None, limit: int = 25,
        include_staff: bool = True,
    ) -> list[dict[str, Any]]:
        """Customer table bridge into Level 2, RBAC-scoped by ``sales_codes``. When
        ``include_staff`` is False, HF-staff customers are excluded (admin-only)."""

    @abc.abstractmethod
    def list_customers(
        self, *, sales_codes: list[str] | None, include_staff: bool = True
    ) -> list[dict[str, Any]]:
        """All customers visible to the caller — feeds the cross-sell worklist. Excludes
        HF-staff customers unless ``include_staff`` is True. Rows carry ``is_staff``."""

    # --- core-banking holdings & value ----------------------------------
    @abc.abstractmethod
    def get_product_holdings(self, cust_id: str) -> dict[str, Any]:
        """Per-product boolean flags + product_map (rec-engine gap input)."""

    @abc.abstractmethod
    def get_relationship_value(self, cust_id: str) -> dict[str, Any]:
        """Snapshot deposits / loans / revenue / relationship value."""

    @abc.abstractmethod
    def get_deposit_accounts(self, cust_id: str) -> list[dict[str, Any]]:
        ...

    @abc.abstractmethod
    def get_loan_accounts(self, cust_id: str) -> list[dict[str, Any]]:
        ...

    # --- time series (long-format; unpivoted server-side) ---------------
    @abc.abstractmethod
    def deposit_loan_series(self, cust_id: str, period: ResolvedPeriod) -> dict[str, list[dict]]:
        """Deposit & loan balance over the period → {'deposits': [...], 'loans': [...]}"""

    @abc.abstractmethod
    def disbursement_vs_balance_series(self, cust_id: str, period: ResolvedPeriod) -> dict[str, list[dict]]:
        ...

    @abc.abstractmethod
    def transaction_series(self, cust_id: str, period: ResolvedPeriod) -> list[dict[str, Any]]:
        ...

    @abc.abstractmethod
    def channel_usage(self, cust_id: str, period: ResolvedPeriod) -> list[dict[str, Any]]:
        ...

    @abc.abstractmethod
    def recent_transactions(self, cust_id: str, *, period: ResolvedPeriod | None = None,
                            limit: int = 8, lookback_months: int = 24) -> list[dict[str, Any]]:
        """Latest customer-facing transactions — a wide lookback so a customer's most
        recent activity always loads even if it was months ago (never a blank feed).
        ``lookback_months`` may be reduced for a light activity probe."""

    # --- whole-book portfolio (real aggregates, no sampling) ------------
    @abc.abstractmethod
    def portfolio_whole_book(self, period: ResolvedPeriod) -> dict[str, Any] | None:
        """Real whole-book Level-1 aggregates (headline totals, segment mix, risk
        distribution, book & segment trends, top movers) — no sampling. Returns None
        when a whole-book scan isn't available (mock mode) so the caller falls back to
        the bounded-sample path."""

    # --- portfolio trends (real month-end history where available) ------
    @abc.abstractmethod
    def portfolio_trends(self, customers: list[dict[str, Any]], period: ResolvedPeriod) -> dict[str, Any] | None:
        """Real book / segment / movers history for the given sample, from month-end
        (EOM) snapshots. Returns None when a real series can't be built (e.g. mock
        mode) so the caller falls back to its simulated preview. Shape:
        ``{'book': {'deposits':[...], 'loans':[...]}, 'segments': [names],
           'segments_data': [{period, <seg>:v, …}], 'movers': [ {…} ]}``."""

    # --- derived risk / KYC ---------------------------------------------
    @abc.abstractmethod
    def get_risk_profile(self, cust_id: str) -> dict[str, Any] | None:
        """Derived KYC + operational risk for one customer, computed from held
        identity attributes and loan performance (see ``c360.risk``). Shape:
        ``{'kyc': {status, score, checks, note}, 'risk': {class, score, factors, note}}``.
        Returns None for an unknown customer."""

    # --- benchmark (rule C) ---------------------------------------------
    @abc.abstractmethod
    def segment_product_benchmark(self, segment: str) -> float:
        """Average number of products held by customers in this segment."""

    # --- non-core domains (preview until pipelines land) ----------------
    # Verified source systems exist (Kocela MySQL, HFDI CRM, insurance_policies)
    # but aren't yet piped into the warehouse, so the mock returns preview data
    # and the live gateway raises until the pipeline is built. Each returns None
    # when the customer has no holding in that domain (→ honest empty state).
    @abc.abstractmethod
    def get_whizz(self, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
        ...

    @abc.abstractmethod
    def get_properties(self, cust_id: str) -> dict[str, Any] | None:
        ...

    @abc.abstractmethod
    def get_bancassurance(self, cust_id: str, period: ResolvedPeriod) -> dict[str, Any] | None:
        ...

    # --- retention / silent-attrition early warning (optional capability) -----
    # Default: not computed. A gateway that holds a deposit-balance history overrides
    # this to return {'flag','trend_pct','note','from','to'} (see c360/retention.py).
    def get_retention_signal(self, cust_id: str) -> dict[str, Any] | None:
        return None

    # --- data-health report (admin panel; live gateways override) -------------
    def health_report(self) -> dict[str, Any]:
        return {'data_mode': 'mock', 'freshness': None, 'checks': [],
                'note': 'Preview (mock) data — connect the live warehouse '
                        '(C360_DATA_MODE=live) to see source health.'}
