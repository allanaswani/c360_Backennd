"""Reaching the insurance book, and refusing to guess when it would be a guess.

Reported: bank customers' policies were not showing. Rajaa Stones Limited and Susan
Wanjiku Kariuki both hold insurance records; neither showed anything.

Two separate causes, measured on the live warehouse (2026-09-18):

1. **98% of the book was discarded before bridging.** The query filtered on
   ``policy_policy_no`` and grouped by it, and that column is blank on 57,188 of
   58,504 rows, covering 10,740 of 11,105 clients. Rajaa Stones has four policy
   rows; all four are unnumbered; all four were thrown away.

2. **The only bridge was a national ID that mostly is not there.** ``idno`` is blank
   on 76% of policy rows, and on the register rows for both reported customers. Of
   the 2,328 clients holding an active policy, only 538 carry an ID.

Bridges now, strongest first: national ID, phone (last nine digits, so +254/254/0 all
agree), then a name that is unique on BOTH sides. The last one reaches 8,490 clients
against the ID's 4,413, and it is safe precisely because of the uniqueness
requirement: SUSAN WANJIKU KARIUKI has five bank records, so that name is ambiguous
and the bridge declines rather than guessing.
"""
from datetime import date

from django.test import SimpleTestCase

from c360.warehouse.trino.trino_gateway import TrinoWarehouse

RAJAA_BANK = 130095          # bank record: national ID present, no mobile
RAJAA_CLIENT = 'RA386'       # insurance record: no national ID, no phone
SUSAN_BANK = 902930          # one of FIVE bank records with this name


class _Warehouse:
    """Only what each customer actually has, as verified against the live tables."""

    def __init__(self, *, bank_nid='', bank_mobile='', bank_name='RAJAA STONES LIMITED',
                 hfbi_name_count=1, bank_name_count=1, register=(), policies=None):
        self.bank_nid = bank_nid
        self.bank_mobile = bank_mobile
        self.bank_name = bank_name
        self.hfbi_name_count = hfbi_name_count
        self.bank_name_count = bank_name_count
        self.register = list(register)
        self.policies = policies if policies is not None else _RAJAA_ROWS
        self.name_lookups = 0

    def execute(self, sql, params=None):
        s = sql.lower()
        if 'from delta.gold_db.dim_customer where customer_id' in s:
            return [{'nid': self.bank_nid, 'mobile': self.bank_mobile,
                     'alt': None, 'full_name': self.bank_name}]
        if 'from delta.gold_db.hfbi_customer_data' in s and 'count(*) n' in s:
            self.name_lookups += 1
            # The register's own spelling comes back too: receipts are keyed by name,
            # so without it a client resolved here has no way into them.
            return [{'n': self.hfbi_name_count, 'client_no': RAJAA_CLIENT,
                     'name': 'Rajaa Stones Limited'}]
        if 'from delta.gold_db.dim_customer where' in s and 'count(*) n' in s:
            return [{'n': self.bank_name_count}]
        if 'from delta.gold_db.hfbi_customer_data' in s:
            return [{**r, 'name': r.get('name', 'Rajaa Stones Limited')} for r in self.register]
        if 'rpt_c360_customer_policies_summary' in s and 'count(' in s:
            return [{'n': 1}]                      # the source is populated
        if 'rpt_c360_customer_policies_summary' in s:
            return self.policies
        return []


# Rajaa Stones' four annual renewals, all unnumbered, exactly as the feed holds them.
_RAJAA_ROWS = [
    {'client_no': RAJAA_CLIENT, 'product': None, 'start_dt': '2022-09-21',
     'end_dt': '2022-12-31', 'insured': 5_000_000, 'pol': '', 'status': 'expired',
     'premium': 60_000, 'idno': ''},
    {'client_no': RAJAA_CLIENT, 'product': None, 'start_dt': '2023-01-01',
     'end_dt': '2023-12-31', 'insured': 5_000_000, 'pol': '', 'status': 'expired',
     'premium': 70_000, 'idno': ''},
    {'client_no': RAJAA_CLIENT, 'product': None, 'start_dt': '2024-01-01',
     'end_dt': '2024-12-31', 'insured': 5_000_000, 'pol': '', 'status': 'expired',
     'premium': 80_000, 'idno': ''},
    {'client_no': RAJAA_CLIENT, 'product': None, 'start_dt': '2025-01-01',
     'end_dt': '2025-12-31', 'insured': 5_000_000, 'pol': '', 'status': 'expired',
     'premium': 90_000, 'idno': ''},
]


class UnnumberedPoliciesTests(SimpleTestCase):
    """A policy with no number is still a policy."""

    def test_the_reported_case_now_resolves(self):
        gw = TrinoWarehouse(_Warehouse(bank_nid='C.12345'))
        out = gw.get_bancassurance(str(RAJAA_BANK), None)
        self.assertIsNotNone(out)
        self.assertEqual(len(out['policies']), 4)
        self.assertEqual(out['unnumbered'], 4)

    def test_renewals_are_kept_as_separate_policies(self):
        """Four years of cover is a different story from one policy, so the natural
        key includes the term rather than collapsing them."""
        gw = TrinoWarehouse(_Warehouse(bank_nid='C.12345'))
        ends = [p['end'] for p in gw.get_bancassurance(str(RAJAA_BANK), None)['policies']]
        self.assertEqual(len(set(ends)), 4)

    def test_newest_first(self):
        gw = TrinoWarehouse(_Warehouse(bank_nid='C.12345'))
        ends = [p['end'] for p in gw.get_bancassurance(str(RAJAA_BANK), None)['policies']]
        self.assertEqual(ends, sorted(ends, reverse=True))

    def test_an_all_expired_book_is_counted_not_hidden(self):
        gw = TrinoWarehouse(_Warehouse(bank_nid='C.12345'))
        out = gw.get_bancassurance(str(RAJAA_BANK), None)
        self.assertEqual(out['active'], 0)
        self.assertEqual(out['expired'], 4)

    def test_a_missing_number_is_null_not_a_fake_reference(self):
        gw = TrinoWarehouse(_Warehouse(bank_nid='C.12345'))
        self.assertIsNone(gw.get_bancassurance(str(RAJAA_BANK), None)['policies'][0]['policy'])


class NameBridgeTests(SimpleTestCase):
    """Rajaa Stones has no ID and no phone on either side. The name is the only link."""

    def test_a_name_unique_on_both_sides_bridges(self):
        gw = TrinoWarehouse(_Warehouse(bank_nid='', bank_mobile=''))
        out = gw.get_bancassurance(str(RAJAA_BANK), None)
        self.assertEqual(len(out['policies']), 4)
        self.assertTrue(all(p['matched_by'] == 'name' for p in out['policies']))
        self.assertEqual(out['name_matched'], 4)

    def test_an_ambiguous_bank_name_refuses_to_bridge(self):
        """SUSAN WANJIKU KARIUKI is five different bank customers. Putting one
        person's policies on another's page is worse than showing nothing."""
        gw = TrinoWarehouse(_Warehouse(bank_nid='', bank_mobile='',
                                       bank_name='SUSAN WANJIKU KARIUKI',
                                       bank_name_count=5))
        self.assertIsNone(gw.get_bancassurance(str(SUSAN_BANK), None))

    def test_an_ambiguous_insurance_name_refuses_to_bridge(self):
        gw = TrinoWarehouse(_Warehouse(bank_nid='', bank_mobile='', hfbi_name_count=3))
        self.assertIsNone(gw.get_bancassurance(str(RAJAA_BANK), None))

    def test_a_short_name_is_not_distinctive_enough(self):
        wh = _Warehouse(bank_nid='', bank_mobile='', bank_name='AB Ltd')
        self.assertIsNone(TrinoWarehouse(wh).get_bancassurance(str(RAJAA_BANK), None))
        self.assertEqual(wh.name_lookups, 0)       # rejected before querying

    def test_the_name_bridge_is_a_last_resort(self):
        """It costs two extra scans, so it only runs when ID and phone found nothing."""
        wh = _Warehouse(bank_nid='C.12345',
                        register=[{'client_no': RAJAA_CLIENT, 'idno': 'C.12345', 'phone': None}])
        TrinoWarehouse(wh).get_bancassurance(str(RAJAA_BANK), None)
        self.assertEqual(wh.name_lookups, 0)


class PhoneBridgeTests(SimpleTestCase):
    def test_country_and_trunk_prefixes_reduce_to_the_same_key(self):
        for number in ('+254712345678', '254712345678', '0712345678', '0712 345 678'):
            self.assertEqual(TrinoWarehouse._phone_key(number), '712345678')

    def test_a_short_number_yields_no_key(self):
        for number in ('12345', '', None, '0712'):
            self.assertEqual(TrinoWarehouse._phone_key(number), '')

    def test_a_phone_match_is_labelled_indicative(self):
        wh = _Warehouse(bank_nid='', bank_mobile='+254712345678',
                        register=[{'client_no': RAJAA_CLIENT, 'idno': '', 'phone': '0712345678'}])
        out = TrinoWarehouse(wh).get_bancassurance(str(RAJAA_BANK), None)
        self.assertEqual(out['phone_matched'], 4)
        self.assertIn('shared handset', out['match_note'])


class MatchNoteTests(SimpleTestCase):
    def test_no_note_when_everything_matched_on_id(self):
        wh = _Warehouse(bank_nid='C.12345',
                        register=[{'client_no': RAJAA_CLIENT, 'idno': 'C.12345', 'phone': None}])
        self.assertIsNone(TrinoWarehouse(wh).get_bancassurance(str(RAJAA_BANK), None)['match_note'])

    def test_the_note_names_the_weaker_bridge(self):
        gw = TrinoWarehouse(_Warehouse(bank_nid='', bank_mobile=''))
        note = gw.get_bancassurance(str(RAJAA_BANK), None)['match_note']
        self.assertIn('unique in both systems', note)
        self.assertTrue(note.startswith('Worth a check:'))


class NameKeyTests(SimpleTestCase):
    def test_punctuation_and_case_do_not_matter(self):
        for variant in ('RAJAA STONES LIMITED', 'Rajaa Stones Limited', 'Rajaa-Stones, Ltd.'):
            self.assertTrue(TrinoWarehouse._name_key(variant).startswith('RAJAASTONES'))
        self.assertEqual(TrinoWarehouse._name_key('RAJAA STONES LIMITED'),
                         TrinoWarehouse._name_key('Rajaa Stones Limited'))


class NameSynonymTests(SimpleTestCase):
    """LTD and LIMITED are the same word.

    Rajaa Stones is 'Rajaa Stones Limited' in the insurance register and 'RAJAA
    STONES LTD' on its 133 receipts, so its own premium history was invisible to its
    own policy panel. Systemic rather than one customer: receipts end in LTD 1,669
    times and LIMITED 1,636; the register 210 and 640.
    """

    def test_ltd_and_limited_fold_together(self):
        self.assertEqual(TrinoWarehouse._name_key('Rajaa Stones Limited'),
                         TrinoWarehouse._name_key('RAJAA STONES LTD'))
        self.assertEqual(TrinoWarehouse._name_key('Rajaa Stones Limited'), 'RAJAASTONESLTD')

    def test_other_company_words_fold_too(self):
        self.assertEqual(TrinoWarehouse._name_key('Acme Company'),
                         TrinoWarehouse._name_key('ACME CO'))
        self.assertEqual(TrinoWarehouse._name_key('A and B Ltd'),
                         TrinoWarehouse._name_key('A & B LIMITED'))

    def test_a_synonym_inside_a_word_is_not_folded(self):
        """LIMITEDACCESS must not become LTDACCESS."""
        self.assertEqual(TrinoWarehouse._name_key('Limitedaccess Holdings'),
                         'LIMITEDACCESSHOLDINGS')

    def test_the_sql_key_folds_the_same_way_without_capture_groups(self):
        """Trino rejects ${1} and a stray backslash would silently stop every name
        comparison matching, so the SQL side uses padded plain REPLACEs instead."""
        sql = TrinoWarehouse._sql_name_key('name')
        self.assertIn("REPLACE", sql)
        self.assertNotIn('$1', sql)
        self.assertNotIn('\1', sql)
        self.assertIn("' LIMITED '", sql)


class ReceiptEvidenceTests(SimpleTestCase):
    """Premium receipts prove a relationship the policy table lost.

    5,956 of 16,952 register clients have no policy row at all, while 31,975 receipts
    exist. Telling an RM "no policies" about a customer with 133 receipts is wrong.
    """

    class _WithReceipts(_Warehouse):
        def execute(self, sql, params=None):
            if 'hfbi_receipt_data' in sql:
                self.receipt_sql = sql
                return [{'receipts': 133, 'paid': 3020025.0, 'risknotes': ['RN1', 'RN2', '']}]
            if 'rpt_c360_customer_policies_summary' in sql and 'count(' not in sql.lower():
                return []            # the policy extract lost them
            return super().execute(sql, params)

    def test_receipts_are_reported_when_no_policy_survived(self):
        gw = TrinoWarehouse(self._WithReceipts(bank_nid='', bank_mobile=''))
        out = gw.get_bancassurance(str(RAJAA_BANK), None)
        self.assertIsNotNone(out)
        self.assertEqual(out['policies'], [])
        self.assertEqual(out['receipts']['receipts'], 133)
        self.assertIn('premium receipts', out['match_note'])
        self.assertIn('not from the relationship', out['match_note'])

    def test_blank_risknotes_are_dropped(self):
        gw = TrinoWarehouse(self._WithReceipts(bank_nid='', bank_mobile=''))
        out = gw.get_bancassurance(str(RAJAA_BANK), None)
        self.assertEqual(out['receipts']['risknotes'], ['RN1', 'RN2'])

    def test_premiums_paid_are_given_but_no_dates(self):
        """The sign of receipt_amount is the posting era (2025 negative, 2026 positive,
        none reversed - checked 2026-10-07), so the premium paid is the absolute
        amount. receipt_date is NULL on every row, so no date is claimed."""
        conn = self._WithReceipts(bank_nid='', bank_mobile='')
        out = TrinoWarehouse(conn).get_bancassurance(str(RAJAA_BANK), None)
        rc = out['receipts']
        self.assertTrue(rc['amounts_available'])
        self.assertEqual(rc['amount_paid'], 3020025)
        self.assertFalse(rc['dates_available'])
        self.assertIn('KES 3,020,025 paid', out['match_note'])
        self.assertIn('ABS(', conn.receipt_sql)          # sign is the posting era
        self.assertIn('SELECT DISTINCT', conn.receipt_sql)  # a receipt loaded twice counts once


class NoPolicyRecordPanelTests(SimpleTestCase):
    """150707 (2026-10-07): two premium receipts for KES 90,000 and no policy record.
    The tab said 'Annual premium in force KES 0 / Policies 0' as live fact."""

    class _Gw:
        def get_customer(self, cust_id):
            return {'cust_id': cust_id}

        def get_bancassurance(self, cust_id, period):
            return {'policies': [], 'active': 0, 'expired': 0, 'unnumbered': 0,
                    'phone_matched': 0, 'name_matched': 0, 'claims': None,
                    'receipts': {'receipts': 2, 'risknotes': ['209607'], 'amount_paid': 90000,
                                 'amounts_available': True, 'dates_available': False},
                    'match_note': 'No policy record survives ...'}

    def test_premiums_paid_lead_and_nothing_is_claimed_as_zero(self):
        from c360.services.domains import build_bancassurance
        from c360.warehouse.periods import resolve_period
        out = build_bancassurance(self._Gw(), '150707', resolve_period('30D', as_of=date(2026, 10, 6)))
        m = {x['label']: x for x in out['metrics']}
        self.assertEqual(m['Premiums paid']['value'], 90000)
        self.assertTrue(m['Premiums paid'].get('lead'))
        for label in ('Annual premium in force', 'Sum insured in force', 'Policies'):
            self.assertIsNone(m[label]['value'], label)
            self.assertEqual(m[label]['status'], 'to_source', label)


class OverviewPremiumInForceTests(SimpleTestCase):
    def test_overview_counts_only_policies_in_force(self):
        import inspect
        from c360.services import overview
        src = inspect.getsource(overview.build_customer_overview)
        self.assertNotIn("sum(p['premium'] for p in banc['policies'])", src)
        self.assertIn("'Annual premium in force'", src)
