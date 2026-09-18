"""The book page must not put a stale flag on screen without checking it.

An RM reported three faults on "My book" and all three were one fault: every figure
there came from ``customer_allocation_base``, a periodic upload to the reporting
Postgres, and nothing reconciled it against the warehouse.

Reproduced from the live warehouse on 2026-09-18, with the loan book fresh to
2026-09-17:

* KAMEL PARK LIMITED was badged NPL. Live status Normal, 2 accounts, KES 36.0M, and
  absent from ``npl_accounts``.
* ARN SECURITY was badged NPL. Live status Normal, KES 19.9M, absent from
  ``npl_accounts`` and on ``pre_npl_accounts`` classified NORMAL.
* CHANDARANA SUPERMARKET was shown holding KES 9.31M of AUM. No live loan rows and a
  deposit balance of 0.00 - the account was closed in June.

``npl_accounts`` could not have caught any of it: it holds exactly one month (May).
``eom_loans`` is the only delinquency source that moves daily, so it decides.

These fixtures use the real customer numbers and the real classifications so the
test fails if the reconciliation is ever removed.
"""
from django.test import SimpleTestCase

from c360.warehouse.trino.trino_gateway import TrinoWarehouse

# The snapshot's view: three customers, two of them flagged non-performing.
KAMEL, ARN, CHANDARANA, HEALTHY = 1203358, 152632, 1097848, 500001


class _StaleSnapshotPG:
    """customer_allocation_base as it actually was: NPL flags months out of date."""

    def execute(self, sql, params=None):
        s = sql.lower()
        if 'group by main_segment' in s:
            return [{'segment': 'BUSINESS BANKING', 'n': 4, 'aum': 91_310_000}]
        if 'order by aum_cust_id desc' in s:
            return [
                {'cust_id': str(KAMEL), 'customer_name': 'KAMEL PARK LIMITED',
                 'segment': 'BUSINESS BANKING', 'aum': 36_200_000, 'contribution': 60_000, 'npl': 1},
                {'cust_id': str(ARN), 'customer_name': 'ARN SECURITY CONSULTANTS',
                 'segment': 'BUSINESS BANKING', 'aum': 10_800_000, 'contribution': 23_000, 'npl': 1},
                {'cust_id': str(CHANDARANA), 'customer_name': 'CHANDARANA SUPERMARKET LIMITED',
                 'segment': 'BUSINESS BANKING', 'aum': 9_310_000, 'contribution': 0, 'npl': 0},
                {'cust_id': str(HEALTHY), 'customer_name': 'GENUINELY OVERDUE LTD',
                 'segment': 'BUSINESS BANKING', 'aum': 5_000_000, 'contribution': 1_000, 'npl': 0},
            ]
        if 'count(*) as customers' in s:
            return [{'customers': 191, 'aum': 185_800_000, 'deposits': 47_200_000,
                     'loans': 138_600_000, 'contribution': 900_000,
                     'npl_customers': 2, 'npl_aum': 47_000_000}]
        # The member list the headline recount reads: every customer in the book,
        # id and AUM only.
        if 'aum_cust_id as aum, npl' in s:
            return [{'cust_id': str(KAMEL), 'aum': 36_200_000, 'npl': 1},
                    {'cust_id': str(ARN), 'aum': 10_800_000, 'npl': 1},
                    {'cust_id': str(CHANDARANA), 'aum': 9_310_000, 'npl': 0},
                    {'cust_id': str(HEALTHY), 'aum': 5_000_000, 'npl': 0}]
        return []


class _LiveBook:
    """The warehouse at the latest close, matching what the live query returned."""

    def __init__(self, fail=False):
        self.fail = fail

    def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError('trino unavailable')
        s = sql.lower()
        if 'eom_loans' in s and 'loan_status_ind_name' in s:
            return [
                {'cust_id': KAMEL, 'status': 'Normal', 'accounts': 2, 'gross': 36_025_515.57},
                {'cust_id': ARN, 'status': 'Normal', 'accounts': 1, 'gross': 19_908_147.31},
                # A customer the snapshot calls performing and the live book does not.
                {'cust_id': HEALTHY, 'status': 'Normal', 'accounts': 1, 'gross': 2_000_000.0},
                {'cust_id': HEALTHY, 'status': 'Overdue', 'accounts': 1, 'gross': 3_000_000.0},
            ]
        if 'eom_deposits' in s:
            return [
                {'cust_id': KAMEL, 'bal': -1_344_093.97, 'accounts': 1},
                {'cust_id': ARN, 'bal': 182_693.50, 'accounts': 1},
                {'cust_id': CHANDARANA, 'bal': 0.00, 'accounts': 1},
                {'cust_id': HEALTHY, 'bal': 10_000.0, 'accounts': 1},
            ]
        if 'max(eom_date)' in s:
            return [{'d': '2026-09-17'}]
        return []


def _book(live=None, pg=None):
    gw = TrinoWarehouse(live or _LiveBook(), postgres=pg or _StaleSnapshotPG())
    return {r['cust_id']: r for r in gw.get_book_summary('SC1')['top_customers']}


class StaleNplFlagTests(SimpleTestCase):
    def test_a_performing_customer_is_not_badged_npl(self):
        """The reported fault, twice over. Both were badged NPL by a snapshot that
        had not been refreshed; both are Normal in the live book."""
        rows = _book()
        for cid in (str(KAMEL), str(ARN)):
            self.assertFalse(rows[cid]['npl'], cid)
            self.assertEqual(rows[cid]['live_status'], 'Normal')
            self.assertEqual(rows[cid]['npl_source'], 'live')

    def test_the_correction_is_visible_not_silent(self):
        """An RM who saw a wrong badge yesterday needs to see it was corrected, not
        just find a different answer today with no explanation."""
        row = _book()[str(KAMEL)]
        self.assertTrue(row['npl_was_snapshot'])
        self.assertTrue(row['npl_corrected'])

    def test_a_genuinely_overdue_customer_is_still_flagged(self):
        """The fix must not simply clear every badge. Worst classification wins, so
        one Overdue facility marks the customer even when another is Normal."""
        row = _book()[str(HEALTHY)]
        self.assertTrue(row['npl'])
        self.assertEqual(row['live_status'], 'Overdue')
        self.assertTrue(row['npl_corrected'])       # the snapshot had said performing

    def test_no_live_lending_is_not_reported_as_performing(self):
        """Absence of a loan row is not evidence that a customer is performing, so
        the badge is dropped rather than flipped to a confident 'performing'."""
        row = _book()[str(CHANDARANA)]
        self.assertFalse(row['npl'])
        self.assertEqual(row['npl_source'], 'no_live_lending')
        self.assertIsNone(row['live_status'])


class ClosedAccountTests(SimpleTestCase):
    def test_a_closed_customer_is_marked(self):
        """Chandarana closed in June and still sat at the top of a book on KES 9.31M
        of snapshot AUM three months later."""
        row = _book()[str(CHANDARANA)]
        self.assertTrue(row['closed'])
        self.assertEqual(row['live_deposits'], 0)
        self.assertEqual(row['live_loans'], 0)

    def test_an_active_customer_is_not_marked_closed(self):
        rows = _book()
        for cid in (str(KAMEL), str(ARN), str(HEALTHY)):
            self.assertFalse(rows[cid]['closed'], cid)

    def test_a_negative_balance_is_not_mistaken_for_closed(self):
        """Kamel Park sits at -1.34M on deposits. An overdrawn account is very much
        open, so 'closed' tests for the absence of a position, not a zero one."""
        self.assertFalse(_book()[str(KAMEL)]['closed'])
        self.assertEqual(_book()[str(KAMEL)]['live_deposits'], -1_344_094)


class LiveCheckUnavailableTests(SimpleTestCase):
    def test_snapshot_is_used_but_labelled_unverified(self):
        """When the warehouse cannot be reached the page still renders, but a flag it
        could not check must not be presented as though it had been."""
        rows = _book(live=_LiveBook(fail=True))
        row = rows[str(KAMEL)]
        self.assertTrue(row['npl'])                  # the snapshot's value, unchanged
        self.assertEqual(row['npl_source'], 'snapshot')
        self.assertFalse(row['verified'])

    def test_the_page_still_lists_everyone(self):
        self.assertEqual(len(_book(live=_LiveBook(fail=True))), 4)


class LiveStandingTests(SimpleTestCase):
    def test_worst_classification_wins_across_accounts(self):
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        standing = gw.live_loan_standing([KAMEL, HEALTHY])
        self.assertEqual(standing[KAMEL]['status'], 'Normal')
        self.assertFalse(standing[KAMEL]['npl'])
        # Normal + Overdue on the same customer resolves to Overdue.
        self.assertEqual(standing[HEALTHY]['status'], 'Overdue')
        self.assertTrue(standing[HEALTHY]['npl'])
        self.assertEqual(standing[HEALTHY]['accounts'], 2)
        self.assertEqual(standing[HEALTHY]['loans'], 5_000_000)

    def test_a_customer_with_no_loans_is_absent_rather_than_performing(self):
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        self.assertNotIn(CHANDARANA, gw.live_loan_standing([CHANDARANA]))

    def test_empty_input_does_not_query(self):
        gw = TrinoWarehouse(_LiveBook(fail=True), postgres=_StaleSnapshotPG())
        self.assertEqual(gw.live_loan_standing([]), {})
        self.assertEqual(gw.live_deposit_totals([]), {})


class HeadlineReconciliationTests(SimpleTestCase):
    """The rail and the list must agree.

    Verifying only the visible rows left the rail saying "2 non-performing" from the
    upload while the list below showed no badges. A page that contradicts itself is
    worse than one uniformly wrong, because then neither number can be trusted.
    """

    def test_headline_npl_is_recounted_from_the_live_book(self):
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        book = gw.get_book_summary('SC1')
        # The upload claimed two. Live: Kamel and ARN are Normal, Chandarana has no
        # lending, and only GENUINELY OVERDUE LTD is actually non-performing.
        self.assertEqual(book['npl_customers'], 1)
        self.assertEqual(book['npl_source'], 'live')
        self.assertEqual(book['npl_snapshot_customers'], 2)

    def test_headline_and_list_agree(self):
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        book = gw.get_book_summary('SC1')
        badged = sum(1 for c in book['top_customers'] if c['npl'])
        self.assertEqual(book['npl_customers'], badged)

    def test_npl_aum_is_summed_over_the_right_customers(self):
        """AUM stays the upload's number because only the upload has an AUM column.
        What changes is which customers it is summed over."""
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        self.assertEqual(gw.get_book_summary('SC1')['npl_aum'], 5_000_000)

    def test_falls_back_to_the_snapshot_when_live_is_unavailable(self):
        gw = TrinoWarehouse(_LiveBook(fail=True), postgres=_StaleSnapshotPG())
        book = gw.get_book_summary('SC1')
        self.assertEqual(book['npl_source'], 'snapshot')
        self.assertEqual(book['npl_customers'], 2)

    def test_a_book_larger_than_the_bound_is_not_scanned_synchronously(self):
        """The whole-book view is the entire bank. It keeps the upload's figure and
        says so rather than scanning the loan book on a page load."""
        class _HugeBook(_StaleSnapshotPG):
            def execute(self, sql, params=None):
                if 'aum_cust_id as aum, npl' in sql.lower():
                    return [{'cust_id': str(i), 'aum': 1, 'npl': 0}
                            for i in range(TrinoWarehouse._BOOK_VERIFY_LIMIT + 1)]
                return super().execute(sql, params)

        gw = TrinoWarehouse(_LiveBook(), postgres=_HugeBook())
        self.assertEqual(gw.get_book_summary(None)['npl_source'], 'snapshot')


class LiveTotalsTests(SimpleTestCase):
    """The RM compared this page against the RM portfolio tool and found deposits
    47.2M vs 55.77M and loans 138.6M vs 250.82M. The other tool reads live and says
    "yesterday"; this one read an upload with no load date on it.

    customer_allocation_base carries no date column at all (all 47 checked), so the
    page cannot say how stale it is. Showing the live figures beside it is what lets
    an RM reconcile the two instead of filing a defect.
    """

    def test_live_totals_are_reported_beside_the_upload(self):
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        book = gw.get_book_summary('SC1')
        # The upload's figures are untouched.
        self.assertEqual(book['deposits'], 47_200_000)
        self.assertEqual(book['loans'], 138_600_000)
        # And the live book's are alongside them.
        live = book['live']
        self.assertIsNotNone(live)
        self.assertEqual(live['loans'], round(36_025_515.57 + 19_908_147.31 + 5_000_000))
        self.assertEqual(live['deposits'],
                         round(-1_344_093.97 + 182_693.50 + 0.00 + 10_000.0))

    def test_a_closed_customer_is_not_counted_as_still_holding(self):
        """Chandarana sits at a flat zero with no loans, which is what 'no longer on
        the book' looks like in the live data."""
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        live = gw.get_book_summary('SC1')['live']
        self.assertEqual(live['customers'], 3)          # of the upload's 4

    def test_an_overdrawn_customer_still_counts(self):
        """Kamel Park is at -1.34M. Overdrawn is not gone."""
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        self.assertEqual(gw.get_book_summary('SC1')['live']['customers'], 3)

    def test_the_page_is_told_the_upload_has_no_date(self):
        """So it cannot imply it knows how current the figures are."""
        gw = TrinoWarehouse(_LiveBook(), postgres=_StaleSnapshotPG())
        self.assertFalse(gw.get_book_summary('SC1')['snapshot_dated'])

    def test_live_totals_are_absent_rather_than_wrong_when_unavailable(self):
        gw = TrinoWarehouse(_LiveBook(fail=True), postgres=_StaleSnapshotPG())
        self.assertIsNone(gw.get_book_summary('SC1')['live'])
