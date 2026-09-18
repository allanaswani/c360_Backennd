""""No receipts on file" is a different claim from "zero receipts".

The list showed clients with large premiums and RECEIPTS 0 - Siritamu Hotel Limited
at KES 346K across 5 policies, Fred Kisiangani at KES 222K across 11. That reads as
"this client has never paid", which is alarming and is not what the data says.

The obvious move was to loosen the name match. Measuring first said don't:

* Receipts reach 7,757 of 16,952 register clients. The feed covers 46% and no more.
* 8,709 of the 10,996 clients holding policies have no receipt name match at all.
* Of 9,733 unmatched register names, a prefix rule would recover 578. Six percent.

Six percent is not worth a rule that lets JOHN KAMAU absorb JOHN KAMAU MWANGI's
premium payments. Siritamu is the proof the match is not the problem: searching the
receipts for SIRITAMU returns nothing, under any spelling.

So the count stays exact and the ABSENCE becomes visible.
"""
from django.test import SimpleTestCase

from c360 import insurance_register as ins_reg


class ReceiptAbsenceTests(SimpleTestCase):
    def test_absence_is_null_not_zero(self):
        """None means the feed has no row for this client. 0 would mean it has rows
        summing to nothing, which never happens."""
        c = ins_reg.shape_client({'client_no': 'S011', 'name': 'Siritamu Hotel Limited'},
                                 policies={'total': 5, 'active': 0, 'premium': 346_000},
                                 receipts={'receipts': None})
        self.assertIsNone(c['insurance']['receipts'])

    def test_a_real_count_is_preserved_exactly(self):
        c = ins_reg.shape_client({'client_no': 'J021', 'name': 'Jacca Consulting Group Limited'},
                                 policies={'total': 1, 'active': 1, 'premium': 85_422},
                                 receipts={'receipts': 1})
        self.assertEqual(c['insurance']['receipts'], 1)

    def test_a_genuine_zero_is_still_zero(self):
        c = ins_reg.shape_client({'client_no': 'X', 'name': 'Test Client'},
                                 receipts={'receipts': 0})
        self.assertEqual(c['insurance']['receipts'], 0)

    def test_no_receipts_argument_at_all_is_absence(self):
        c = ins_reg.shape_client({'client_no': 'X', 'name': 'Test Client'})
        self.assertIsNone(c['insurance']['receipts'])

    def test_premium_and_receipts_are_independent(self):
        """A client can hold real policies and real premium while the receipts feed
        has nothing for them. That is Siritamu, and it is not a contradiction."""
        c = ins_reg.shape_client({'client_no': 'S011', 'name': 'Siritamu Hotel Limited'},
                                 policies={'total': 5, 'active': 0, 'premium': 346_000},
                                 receipts={'receipts': None})
        self.assertEqual(c['insurance']['premium'], 346_000)
        self.assertEqual(c['insurance']['policies'], 5)
        self.assertIsNone(c['insurance']['receipts'])


class CoverageReachTests(SimpleTestCase):
    """The page states how far the receipts feed reaches, because without it a blank
    column reads as 'never paid' rather than 'the feed stops short'."""

    class _PG:
        def execute(self, sql, params=None):
            s = sql.lower()
            if 'hfbi_customer_data' in s and 'count(distinct' in s:
                return [{'n': 16952}]
            if 'not in' in s:
                return [{'n': 452}]
            if 'join d on d.idno' in s:
                return [{'n': 4238}]
            if 'join r on r.k = h.k' in s:
                return [{'n': 7757}]
            return []

    def test_the_note_states_the_reach(self):
        from c360.warehouse.trino.trino_gateway import TrinoWarehouse
        from django.core.cache import cache
        cache.delete('c360:ins:coverage')
        out = TrinoWarehouse(self._PG()).insurance_client_coverage()
        self.assertEqual(out['receipts_reach'], 7757)
        self.assertIn('7,757', out['note'])
        self.assertIn('does not cover the rest', out['note'])

    def test_a_failing_reach_query_does_not_break_the_panel(self):
        from c360.warehouse.trino.trino_gateway import TrinoWarehouse
        from django.core.cache import cache

        class _Partial(self._PG):
            def execute(self, sql, params=None):
                if 'join r on r.k = h.k' in sql.lower():
                    raise RuntimeError('too slow')
                return super().execute(sql, params)

        cache.delete('c360:ins:coverage')
        out = TrinoWarehouse(_Partial()).insurance_client_coverage()
        self.assertEqual(out['total'], 16952)
        self.assertIsNone(out['receipts_reach'])
        self.assertNotIn('does not cover the rest', out['note'])
