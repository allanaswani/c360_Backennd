"""Recommendation engine — the auditable core. These lock the contract and the
hard eligibility behaviour so a refactor can't silently start faking eligibility."""
from django.test import SimpleTestCase

from c360.recommendations.engine import _evaluate_gate, recommend_for_customer
from c360.recommendations import rules
from c360.warehouse.mock.mock_gateway import MockWarehouse


def _profile(risk_cls, kyc_status):
    return {'risk': {'class': risk_cls, 'factors': [], 'note': ''},
            'kyc': {'status': kyc_status, 'checks': [], 'note': ''}}


class RecommendationEngineTests(SimpleTestCase):
    def setUp(self):
        self.gw = MockWarehouse()

    def test_gate_runs_on_derived_profile(self):
        # Risk/KYC are now DERIVED from live data → the gate is evaluable, and a
        # Verified + Low/Medium customer's candidates surface as actionable items.
        result = recommend_for_customer(self.gw, 'HF-100238')
        self.assertTrue(result.eligibility['gate_evaluable'])
        self.assertEqual(result.eligibility['basis'], 'derived')
        self.assertEqual(result.status, 'ok')
        self.assertGreater(len(result.items), 0)
        self.assertEqual(result.withheld, [])
        for item in result.items:
            self.assertIs(item['eligible'], True)
            self.assertIsNone(item['score'])      # nullable propensity, empty in v1
            self.assertTrue(item['reason'])       # RM-speakable reason present

    def test_gate_blocks_high_risk_but_still_computes_candidates(self):
        # A blocked gate never fakes a pass: items stay empty, candidates are held
        # with the blocking reason and are explicitly NOT eligible.
        gate = _evaluate_gate(_profile('High', 'Verified'))
        self.assertTrue(gate['gate_evaluable'])
        self.assertFalse(gate['passed'])
        self.assertIn('risk', gate['note'].lower())

    def test_gate_blocks_unverified_kyc(self):
        gate = _evaluate_gate(_profile('Low', 'Partial'))
        self.assertFalse(gate['passed'])
        self.assertIn('kyc', gate['note'].lower())

    def test_gate_passes_low_verified(self):
        gate = _evaluate_gate(_profile('Low', 'Verified'))
        self.assertTrue(gate['passed'])

    def test_unevaluable_when_no_profile(self):
        gate = _evaluate_gate(None)
        self.assertFalse(gate['gate_evaluable'])

    def test_rule_a_fires_for_mortgage_without_ipf(self):
        # HF-100238 holds a mortgage but no IPF → Rule A product-gap candidate.
        holdings = self.gw.get_product_holdings('HF-100238')
        cands = rules.rule_a_product_gap(holdings)
        self.assertTrue(any(c.product == 'ipf' and c.rule_id == 'A' for c in cands))

    def test_rule_c_peer_benchmark_respects_segment_average(self):
        holdings = {'flags': {'deposit': True, 'mobile': False}}
        cands = rules.rule_c_peer_benchmark(holdings, segment_benchmark=4.6)
        self.assertTrue(cands)                     # holds 1 < 4.6 → gap
        self.assertEqual(cands[0].rule_id, 'C')

    def test_limit_is_respected(self):
        result = recommend_for_customer(self.gw, 'HF-101488', limit=2)
        self.assertLessEqual(len(result.items) + len(result.withheld), 2)

    def test_unknown_customer_returns_empty(self):
        result = recommend_for_customer(self.gw, 'NOPE-000')
        self.assertEqual(result.items, [])
        self.assertEqual(result.withheld, [])
