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


class _StaleParamsConn(_AsOfConn):
    """bank_parameters a day behind core banking; ``next_day`` postings already in the
    ledger after its curr_trx_date. Snapshot tables hold whatever ``dep_max``/``loan_max``
    say, capped at the date the resolver asks for (as the real max(eom_date) <= cap)."""

    def __init__(self, *, prev, curr, next_day, dep_max, loan_max, fail=False):
        super().__init__(biz=prev, dep_max=dep_max, loan_max=loan_max)
        self.curr, self.next_day, self.fail = curr, next_day, fail

    def execute(self, sql, params=None):
        s = sql.lower()
        if 'bank_parameters' in s:
            return [{'d': self.biz, 'c': self.curr}]
        if 'fact_dep_trx_recording' in s and 'count(*)' in s:
            if self.fail:
                raise RuntimeError('trino down')
            return [{'n': self.next_day}]
        if 'max(eom_date)' in s:
            import re
            cap = date.fromisoformat(re.search(r"eom_date <= date '([0-9-]+)'", s).group(1))
            have = self.dep_max if 'eom_deposits' in s else self.loan_max
            return [{'d': min(have, cap)}]
        return []


class ClosedBusinessDayTests(SimpleTestCase):
    # 2026-11-03: the warehouse copy said prev 31 Oct / curr 2 Nov; core banking had
    # closed 2 Nov and the ledger held 3 Nov postings; eom tables held 2 Nov.
    def _gw(self, **kw):
        args = dict(prev=date(2026, 10, 31), curr=date(2026, 11, 2), next_day=11530,
                    dep_max=date(2026, 11, 2), loan_max=date(2026, 11, 2))
        args.update(kw)
        return TrinoWarehouse(_StaleParamsConn(**args))

    def _today(self, d=date(2026, 11, 3)):
        from unittest import mock

        class _D(date):
            @classmethod
            def today(cls):
                return d
        return mock.patch('c360.warehouse.trino.trino_gateway.date', _D)

    def test_stale_parameters_do_not_hold_the_snapshot_back(self):
        with self._today():
            gw = self._gw()
            self.assertEqual(gw.as_of_date(), date(2026, 11, 2))
            self.assertEqual(gw._source_dates['business'], date(2026, 10, 31))   # Data health still sees it

    def test_too_few_next_day_postings_keep_prev(self):
        with self._today():
            self.assertEqual(self._gw(next_day=12).as_of_date(), date(2026, 10, 31))

    def test_failed_probe_keeps_prev(self):
        with self._today():
            self.assertEqual(self._gw(fail=True).as_of_date(), date(2026, 10, 31))

    def test_no_curr_or_curr_today_keeps_prev(self):
        with self._today():
            self.assertEqual(self._gw(curr=None).as_of_date(), date(2026, 10, 31))
            self.assertEqual(self._gw(curr=date(2026, 11, 3)).as_of_date(), date(2026, 10, 31))

    def test_snapshot_not_loaded_for_curr_falls_back(self):
        with self._today():
            gw = self._gw(dep_max=date(2026, 10, 31))
            self.assertEqual(gw.as_of_date(), date(2026, 10, 31))
