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
            return [{'n': self.hfbi_name_count, 'client_no': RAJAA_CLIENT}]
        if 'from delta.gold_db.dim_customer where' in s and 'count(*) n' in s:
            return [{'n': self.bank_name_count}]
        if 'from delta.gold_db.hfbi_customer_data' in s:
            return self.register
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
