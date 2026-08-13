"""Whole-book snapshot hardening (regression for the frozen-KES-0 incident).

A transient partial read of eom_deposits (2.37B rows) once returned the loan half of
the book while the deposit scans came back empty, so deposits / active / avg-products
collapsed to 0 while loans stayed populated — and that half-empty snapshot got memoised
for the whole process life and served with a "Live data" badge (a silent live KES 0).
These tests pin the two fixes: reject a partial read (never cache it), and expire the
memo so a good snapshot can't freeze forever either.
"""
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from c360.warehouse.trino.trino_gateway import LiveDataNotReady, TrinoWarehouse


class _FakeConn:
    """Minimal Trino stand-in for _whole_book_asof. Routes by SQL shape and returns a
    populated loan book always; the deposit-side scans are toggled by ``deposits_ok``
    to reproduce the partial-read failure (loans present, deposits empty)."""

    def __init__(self, *, deposits_ok: bool):
        self.deposits_ok = deposits_ok
        self.calls = 0

    def execute(self, sql, params=None):
        self.calls += 1
        s = sql.lower()
        if 'bank_parameters' in s:                                    # as_of_date()
            return [{'d': date(2026, 8, 12)}]
        if 'eom_loans' in s and 'group by dc.customer_segment' in s:  # loan segment mix
            return [{'seg': 'STANDARD', 'n': 100, 'v': Decimal('50000000')}]
        if 'full outer join' in s and 'bucket' in s:                 # risk distribution
            return [{'bucket': 'Medium', 'n': 100}]
        if 'eom_deposits' in s and 'eom_loans' not in s:             # deposit-side scans
            if not self.deposits_ok:
                return []                                            # ← the transient failure
            if 'avg(pc)' in s:                                       # product benchmark
                return [{'seg': 'STANDARD', 'avg_products': 1.2, 'custs': 90}]
            if 'entry_status' in s:                                  # active customers
                return [{'n': 80}]
            if 'order by 2 desc limit 8' in s:                       # top products
                return [{'pd': 'Savings', 'v': Decimal('40000000'), 'n': 90}]
            if 'group by dc.customer_segment' in s:                  # deposit segment mix
                return [{'seg': 'STANDARD', 'n': 90, 'v': Decimal('40000000')}]
        return []                                                    # movers, internal ids, …


class WholeBookDegradedGuardTests(SimpleTestCase):
    def test_partial_read_raises_and_is_not_cached(self):
        gw = TrinoWarehouse(_FakeConn(deposits_ok=False))
        with self.assertRaises(LiveDataNotReady):
            gw._whole_book_asof()
        # The half-empty snapshot must not be memoised — the next request retries clean.
        self.assertIsNone(gw._wb_asof)

    def test_partial_read_retries_every_call_never_freezes(self):
        gw = TrinoWarehouse(_FakeConn(deposits_ok=False))
        for _ in range(3):
            with self.assertRaises(LiveDataNotReady):
                gw._whole_book_asof()
        self.assertIsNone(gw._wb_asof)

    def test_healthy_snapshot_is_cached_with_timestamp(self):
        conn = _FakeConn(deposits_ok=True)
        gw = TrinoWarehouse(conn)
        wb = gw._whole_book_asof()
        self.assertEqual(wb['deposits'], 40000000)
        self.assertEqual(wb['loans'], 50000000)
        self.assertEqual(wb['active_customers'], 80)
        self.assertGreater(gw._wb_asof_at, 0.0)
        # Second call is served from the memo — no further queries hit the connector.
        calls_after_first = conn.calls
        again = gw._whole_book_asof()
        self.assertIs(again, wb)
        self.assertEqual(conn.calls, calls_after_first)

    def test_expired_memo_recomputes(self):
        conn = _FakeConn(deposits_ok=True)
        gw = TrinoWarehouse(conn)
        gw._whole_book_asof()
        calls_after_first = conn.calls
        gw._wb_asof_at -= (gw._WB_TTL_SECONDS + 1)   # pretend the memo aged past its TTL
        gw._whole_book_asof()
        self.assertGreater(conn.calls, calls_after_first)  # it re-read rather than freezing


class WholeBookDegradedMatrixTests(SimpleTestCase):
    """The pure decision the guard makes, independent of any query."""

    def test_the_production_failure_is_degraded(self):
        # loans populated, deposit scans empty → deposits/active/products 0.
        self.assertTrue(TrinoWarehouse._wb_degraded(26099, 0, 0, 50412065567))

    def test_mirror_failure_is_degraded(self):
        # deposits populated, loan scans empty.
        self.assertTrue(TrinoWarehouse._wb_degraded(1095064, 67460, 56811639728, 0))

    def test_holdings_without_live_population_is_degraded(self):
        self.assertTrue(TrinoWarehouse._wb_degraded(0, 0, 56811639728, 50412065567))

    def test_a_healthy_snapshot_passes(self):
        self.assertFalse(TrinoWarehouse._wb_degraded(1095064, 67460, 56811639728, 50412065567))

    def test_both_halves_zero_is_left_alone(self):
        # Ambiguous (genuinely empty book or a bad as-of date), not our asymmetric
        # partial-read signature — don't reject, so we never loop on a real empty book.
        self.assertFalse(TrinoWarehouse._wb_degraded(0, 0, 0, 0))
