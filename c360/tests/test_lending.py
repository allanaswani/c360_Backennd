"""Lending-health shaping — delinquency severity ranking + collateral de-noising."""
from __future__ import annotations

from django.test import SimpleTestCase

from c360 import lending


class DelinquencyTests(SimpleTestCase):
    def test_worst_classification_wins(self):
        out = lending.shape_delinquency(
            [{'classification': 'SUBSTD', 'impairment': 100},
             {'classification': 'LOSS', 'impairment': 900},
             {'classification': 'DOUBTFUL', 'impairment': 500}], False, 'May')
        self.assertEqual(out['status'], 'npl')
        self.assertEqual(out['classification'], 'Loss')   # worst, not alphabetical
        self.assertEqual(out['severity'], 3)
        self.assertEqual(out['accounts'], 3)
        self.assertEqual(out['impairment'], 1500)
        self.assertEqual(out['month'], 'May')

    def test_watch_only(self):
        out = lending.shape_delinquency([], True, 'May')
        self.assertEqual(out['status'], 'watch')
        self.assertEqual(out['classification'], 'Watch')
        self.assertEqual(out['severity'], 0)

    def test_clean_customer_is_none(self):
        self.assertIsNone(lending.shape_delinquency([], False, None))

    def test_unknown_classification_ignored(self):
        # a row with an unrecognised class and no watch -> nothing to report
        self.assertIsNone(lending.shape_delinquency([{'classification': 'FOO', 'impairment': 1}], False, 'May'))


class CollateralTests(SimpleTestCase):
    def test_types_titlecased_and_sorted(self):
        out = lending.shape_collateral(
            [{'type': 'VEHICLE', 'count': 2}, {'type': 'PROPERTY', 'count': 5}])
        self.assertEqual(out['distinct'], 2)
        self.assertEqual(out['types'][0], {'type': 'Property', 'count': 5})  # sorted by count desc
        self.assertEqual(out['types'][1]['type'], 'Vehicle')

    def test_blank_type_dropped(self):
        out = lending.shape_collateral([{'type': '   ', 'count': 3}, {'type': 'BOND', 'count': 1}])
        self.assertEqual(out['distinct'], 1)
        self.assertEqual(out['types'][0]['type'], 'Bond')

    def test_no_collateral_is_none(self):
        self.assertIsNone(lending.shape_collateral([]))
