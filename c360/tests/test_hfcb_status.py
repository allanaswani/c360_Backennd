"""Loan-performance status must never contradict the live loan book.

The NPL flag comes from ``customer_allocation_base`` — a periodic snapshot. Printing it
blind produced screens that said 'Non-performing' next to live loans of KES 0 and a
'no loan facilities' panel (the snapshot still reflected a since-cleared loan). These
tests pin the reconciliation: with no live loan the status is 'No active loan' and the
stale flag is demoted to a note; only a live loan yields Performing/Non-performing.
"""
from django.test import SimpleTestCase

from c360.services.hfcb import _npl_metric


class NplReconciliationTests(SimpleTestCase):
    def test_no_live_loan_never_shows_non_performing(self):
        # Snapshot flags NPL, but there is no live loan → must not scream 'Non-performing'.
        m = _npl_metric({'npl': True}, loans=0)
        self.assertEqual(m['value'], 'No active loan')
        self.assertIn('non-performing', m['note'].lower())   # history preserved in the note

    def test_no_live_loan_no_flag(self):
        m = _npl_metric({'npl': False}, loans=0)
        self.assertEqual(m['value'], 'No active loan')

    def test_live_loan_non_performing(self):
        m = _npl_metric({'npl': True}, loans=24_000_000)
        self.assertEqual(m['value'], 'Non-performing')

    def test_live_loan_performing(self):
        m = _npl_metric({'npl': False}, loans=1_000_000)
        self.assertEqual(m['value'], 'Performing')

    def test_live_loan_but_no_feed_is_not_sourced(self):
        # A live loan exists but the allocation feed is absent → honestly not sourced.
        m = _npl_metric(None, loans=1_000_000)
        self.assertEqual(m['status'], 'to_source')
