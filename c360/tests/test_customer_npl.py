"""The customer page must not contradict itself, or the book page.

KAMEL PARK LIMITED rendered "Loan status: Non-performing" beside "Risk class: Low"
and KES 36.0M of lending the live book classifies Normal. One page, two answers, and
the wrong one came from the allocation upload's npl column.

Reproduced from the live warehouse on 2026-09-18: customer 1203358, two loan
accounts, KES 36,025,515.57 gross, status Normal, absent from npl_accounts.

This is the page every RM lands on, so fixing it here is what fixes it for everyone.
"""
from django.test import SimpleTestCase

from c360.services.hfcb import _npl_metric

PERFORMING = {'status': 'Normal', 'npl': False, 'loans': 36_025_516, 'accounts': 2}
OVERDUE = {'status': 'Overdue', 'npl': True, 'loans': 5_000_000, 'accounts': 2}
STALE_NPL = {'npl': 1}          # what the allocation upload said
STALE_CLEAN = {'npl': 0}


class LiveClassificationWinsTests(SimpleTestCase):
    def test_the_reported_case(self):
        """Live says Normal, the upload says non-performing. Live wins."""
        m = _npl_metric(STALE_NPL, 36_025_516, PERFORMING)
        self.assertEqual(m['value'], 'Performing')
        self.assertIn('Normal', m['note'])

    def test_the_disagreement_is_stated_not_hidden(self):
        note = _npl_metric(STALE_NPL, 36_025_516, PERFORMING)['note']
        self.assertIn('allocation upload says non-performing', note)
        self.assertIn('does not support', note)

    def test_a_genuinely_overdue_customer_is_still_flagged(self):
        """The fix must not simply clear every flag."""
        m = _npl_metric(STALE_CLEAN, 5_000_000, OVERDUE)
        self.assertEqual(m['value'], 'Non-performing')
        self.assertIn('Overdue', m['note'])

    def test_agreement_needs_no_caveat(self):
        note = _npl_metric(STALE_NPL, 5_000_000, OVERDUE)['note']
        self.assertNotIn('does not support', note)


class NoLiveLendingTests(SimpleTestCase):
    def test_no_loan_is_not_reported_as_non_performing(self):
        m = _npl_metric(STALE_NPL, 0, None)
        self.assertEqual(m['value'], 'No active loan')
        self.assertIn('most likely a loan that has since been cleared', m['note'])

    def test_no_loan_and_no_flag(self):
        self.assertEqual(_npl_metric(STALE_CLEAN, 0, None)['value'], 'No active loan')


class LiveUnavailableTests(SimpleTestCase):
    def test_the_upload_is_used_but_marked_unverified(self):
        """A flag we could not check must not be presented as one we did."""
        m = _npl_metric(STALE_NPL, 36_025_516, None)
        self.assertEqual(m['value'], 'Non-performing')
        self.assertIn('unverified', m['note'])

    def test_pending_when_there_is_no_flag_at_all(self):
        m = _npl_metric(None, 36_025_516, None)
        self.assertEqual(m['status'], 'to_source')


class ConsistencyWithTheBookPageTests(SimpleTestCase):
    """Both pages now read the same source, so they cannot disagree about a customer."""

    def test_both_pages_use_the_same_rule(self):
        from c360.warehouse.trino.trino_gateway import TrinoWarehouse

        class _Live:
            def execute(self, sql, params=None):
                if 'eom_loans' in sql and 'loan_status_ind_name' in sql:
                    return [{'cust_id': 1203358, 'status': 'Normal', 'accounts': 2,
                             'gross': 36_025_515.57}]
                if 'max(eom_date)' in sql.lower():
                    return [{'d': '2026-09-17'}]
                return []

        standing = TrinoWarehouse(_Live()).live_loan_standing([1203358])[1203358]
        self.assertFalse(standing['npl'])
        # The customer page reaches the same verdict from the same record.
        self.assertEqual(_npl_metric(STALE_NPL, 36_025_516, standing)['value'], 'Performing')
