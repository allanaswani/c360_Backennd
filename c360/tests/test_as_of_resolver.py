"""As-of anchor resolves to the latest AVAILABLE snapshot, not the raw business date.

Regression for the second frozen-KES-0 incident: bank_parameters.prev_trx_date rolls
forward daily, but eom_deposits lags (and both tables skip weekends/holidays). Pinning
value queries to the raw business date returned 0 deposits for every customer the day
the deposit partition had not yet posted — a deposit-only customer then read 'KES 0'
though the balance was one day back. The resolver must fall back to the newest date
BOTH fact tables share, so deposits and loans are one consistent, non-empty snapshot.
"""
from datetime import date

from django.test import SimpleTestCase

from c360.warehouse.trino.trino_gateway import TrinoWarehouse


class _AsOfConn:
    """Routes the three probes the resolver issues: the business date, and the latest
    available eom_date in each fact table (deposits deliberately one day behind loans)."""

    def __init__(self, *, biz, dep_max, loan_max):
        self.biz, self.dep_max, self.loan_max = biz, dep_max, loan_max

    def execute(self, sql, params=None):
        s = sql.lower()
        if 'bank_parameters' in s:
            return [{'d': self.biz}]
        if 'max(eom_date)' in s and 'eom_deposits' in s:
            return [{'d': self.dep_max}]
        if 'max(eom_date)' in s and 'eom_loans' in s:
            return [{'d': self.loan_max}]
        return []


class AsOfResolverTests(SimpleTestCase):
    def test_uses_common_latest_when_deposits_lag(self):
        # Business date 08-19; loans loaded to 08-19 but deposits only to 08-18.
        gw = TrinoWarehouse(_AsOfConn(
            biz=date(2026, 8, 19), dep_max=date(2026, 8, 18), loan_max=date(2026, 8, 19)))
        self.assertEqual(gw.as_of_date(), date(2026, 8, 18))   # not the empty 08-19

    def test_uses_business_date_when_both_current(self):
        gw = TrinoWarehouse(_AsOfConn(
            biz=date(2026, 8, 19), dep_max=date(2026, 8, 19), loan_max=date(2026, 8, 19)))
        self.assertEqual(gw.as_of_date(), date(2026, 8, 19))

    def test_loans_lagging_also_snaps_back(self):
        # Mirror case: deposits current, loans a day behind → still the shared date.
        gw = TrinoWarehouse(_AsOfConn(
            biz=date(2026, 8, 19), dep_max=date(2026, 8, 19), loan_max=date(2026, 8, 18)))
        self.assertEqual(gw.as_of_date(), date(2026, 8, 18))

    def test_falls_back_to_business_date_when_probe_empty(self):
        # No max(eom_date) rows readable (e.g. window miss) → business date, never a crash.
        gw = TrinoWarehouse(_AsOfConn(
            biz=date(2026, 8, 19), dep_max=None, loan_max=None))
        self.assertEqual(gw.as_of_date(), date(2026, 8, 19))
