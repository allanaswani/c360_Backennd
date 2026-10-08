"""Properties tab (2026-10-08): no KES 0 for an unstated value, shared units said,
phone-matched units said."""
from datetime import date

from django.test import SimpleTestCase

from c360.services.domains import build_properties
from c360.warehouse.periods import resolve_period

P = resolve_period('30D', as_of=date(2026, 10, 6))


class _Gw:
    def __init__(self, props):
        self.props = props

    def get_customer(self, cust_id):
        return {'cust_id': cust_id}

    def get_properties(self, cust_id):
        return {'properties': self.props, 'payments': [], 'payments_available': True,
                'payments_from': None}


def _unit(**kw):
    base = {'unit_id': 1, 'unit': 'A1', 'project': 'Komarock', 'value': 5_000_000,
            'mortgage': False, 'paid_pct': None}
    return {**base, **kw}


class PropertyPanelTests(SimpleTestCase):
    def test_unit_with_no_value_is_not_stated_not_zero(self):
        out = build_properties(_Gw([_unit(value=None)]), '1', P)
        m = {x['label']: x for x in out['metrics']}
        self.assertIsNone(m['Property value']['value'])
        self.assertEqual(m['Property value']['status'], 'to_source')

    def test_partly_valued_book_says_how_many_are_missing(self):
        out = build_properties(_Gw([_unit(), _unit(unit_id=2, value=None)]), '1', P)
        m = {x['label']: x for x in out['metrics']}
        self.assertEqual(m['Property value']['value'], 5_000_000)
        self.assertIn('1 of 2 units carry no value', m['Property value']['meta'])

    def test_shared_unit_is_flagged(self):
        out = build_properties(_Gw([_unit(shared='1 other client')]), '1', P)
        self.assertIn('shared', out['tables'][0]['columns'])
        self.assertIn('also registered to another client', out['metrics'][0]['meta'])

    def test_phone_match_is_said(self):
        out = build_properties(_Gw([_unit(matched_by='phone')]), '1', P)
        self.assertIn('matched_by', out['tables'][0]['columns'])
        self.assertIn('by phone number', out['note'])

    def test_id_match_adds_no_note(self):
        out = build_properties(_Gw([_unit(matched_by='national ID')]), '1', P)
        self.assertIsNone(out['note'])
        self.assertNotIn('matched_by', out['tables'][0]['columns'])
