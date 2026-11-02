"""Digital (Whizz) loan history (c360/digital_loans.py), on rows shaped like customer
8851's: plain mobile loans plus Mwanabiashara loans the agreement table did not type."""
from django.test import SimpleTestCase

from c360 import digital_loans

NAMES = {1: 'MOBILE LOAN', 14: 'MWANABIASHARA 3-MONTHS MOBILE LOAN', 13: 'MWANABIASHARA 1 MONTH LOAN'}
ROWS = [
    {'account_no': '0600389859', 'opened': '2026-10-28', 'due': '2026-11-26', 'amount': 30000, 'fee': 2160, 'programme': 1},
    {'account_no': '0600383621', 'opened': '2026-07-29', 'due': '2026-10-29', 'amount': 100000, 'fee': 7200, 'programme': 14},
    {'account_no': '0600381613', 'opened': '2026-06-29', 'due': '2026-07-30', 'amount': 90000, 'fee': 6480, 'programme': 13},
    {'account_no': '0600300000', 'opened': '2021-03-03', 'due': '2021-04-02', 'amount': 8200, 'fee': 0, 'programme': 10},
    {'account_no': 'X', 'opened': None, 'amount': 5000, 'fee': 0, 'programme': 1},       # undated: dropped
]


class DigitalLoanTests(SimpleTestCase):
    def setUp(self):
        self.out = digital_loans.build(ROWS, NAMES, as_of='2026-10-30')

    def test_every_programme_is_counted(self):
        self.assertEqual(self.out['loans_taken'], 4)
        self.assertEqual(self.out['largest'], {'opened': '2026-07-29', 'amount': 100000,
                                               'programme': 'Mwanabiashara 3-Months Mobile Loan'})
        self.assertEqual([p['programme'] for p in self.out['programmes']],
                         ['Mobile Loan', 'Mwanabiashara 3-Months Mobile Loan', 'Mwanabiashara 1 Month Loan',
                          'Programme 10'])

    def test_totals_and_last_twelve_months(self):
        self.assertEqual((self.out['borrowed_total'], self.out['fees_total']), (228200, 15840))
        self.assertEqual(self.out['last_12m'], {'loans': 3, 'borrowed': 220000, 'fees': 15840, 'highest': 100000})

    def test_newest_first(self):
        self.assertEqual(self.out['latest']['opened'], '2026-10-28')
        self.assertEqual(self.out['first_opened'], '2021-03-03')
        self.assertEqual(self.out['loans'][0]['account_no'], '0600389859')

    def test_none_without_loans(self):
        self.assertIsNone(digital_loans.build([], NAMES, as_of='2026-10-30'))


class WhizzTabSectionTests(SimpleTestCase):
    def test_tab_shows_largest_loan_and_fees(self):
        from c360.services.domains import _digital_sections
        dig = digital_loans.build(ROWS, NAMES, as_of='2026-10-30')
        metrics, charts, tables = _digital_sections(dig, 'live')
        by = {m['label']: m for m in metrics}
        self.assertEqual(by['Largest loan']['value'], 100000)
        self.assertEqual(by['Fees charged, 12 months']['value'], 15840)
        self.assertEqual(tables[0]['columns'], ['date', 'product', 'account_no', 'amount', 'fee', 'due'])
        self.assertEqual(charts[0]['data'][0]['period'], '2021-03-03')     # oldest first on the chart
