"""Customer insights: product mix, facilities, activity cross-sell, profile, revenue.

Every case below is a behaviour found on live data on 2026-09-30 - the wallet filed
under "current account", foreign-currency current accounts filed under "overdraft",
repaid agreements still in the snapshot, placeholder limits, government accounts
pitched an overdraft. Offline: pure functions plus fake gateways and connectors.
"""
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase

from c360 import activity as A
from c360 import customer_profile as CP
from c360 import facilities as F
from c360 import products as P
from c360.recommendations import engine
from c360.recommendations.rules import Candidate
from c360.services import insights
from c360.warehouse.gateway import WarehouseGateway
from c360.warehouse.trino.trino_gateway import TrinoWarehouse


class ProductMixTests(SimpleTestCase):
    def test_wallet_is_not_a_current_account(self):
        mix = P.summarise([{'level2': 'CURRENT ACCOUNT', 'product': 'VIRTUAL ACCOUNT MOBILE', 'balance': 50}], [])
        head = {h['key']: h for h in mix['headline']}
        self.assertFalse(head['current']['held'])
        self.assertTrue(head['virtual_wallet']['held'])

    def test_salary_account_is_a_current_account(self):
        # MALIPO SALARY ACCOUNT carries neither 'current' nor 'saving' in its name.
        mix = P.summarise([{'level2': 'CURRENT ACCOUNT', 'product': 'MALIPO SALARY ACCOUNT', 'balance': 9}], [])
        self.assertTrue({h['key']: h for h in mix['headline']}['current']['held'])

    def test_fcy_current_account_answers_has_current_account(self):
        mix = P.summarise([{'level2': 'OVERDRAFT', 'product': 'CURRENT USD', 'balance': 4}], [])
        head = {h['key']: h for h in mix['headline']}
        self.assertTrue(head['current']['held'])
        self.assertEqual(mix['deposits'][0]['label'], 'Foreign-currency current account')

    def test_internal_memo_products_are_never_holdings(self):
        mix = P.summarise([{'level2': 'DUMMY', 'product': 'MEMO PRODUCT-CASH', 'balance': 1e6}], [])
        self.assertEqual(mix['deposits'], [])
        self.assertEqual(mix['held_keys'], [])

    def test_loan_types_and_mortgage_flag(self):
        mix = P.summarise([], [
            {'level2': 'PURCHASE MORTGAGES', 'product': 'PURCHASE OWNER OCCUPIER', 'balance': 4_000_000},
            {'level2': 'HIDDEN ACCOUNT', 'product': 'OVERDRAFT-CORPORATE', 'balance': 10},
            {'level2': 'NOT A TREE NODE', 'product': 'X', 'balance': 5},
        ])
        self.assertEqual(mix['loan_types'], ['Purchase mortgage', 'Overdraft'])
        self.assertTrue(mix['holds_mortgage'])

    def test_accounts_in_same_category_are_summed(self):
        mix = P.summarise([
            {'level2': 'SAVINGS ACCOUNT', 'product': 'FANAKA SAVING ACCOUNT', 'balance': 100},
            {'level2': 'SAVINGS ACCOUNT', 'product': 'SAVINGS EURO', 'balance': 50},
        ], [])
        g = mix['deposits'][0]
        self.assertEqual((g['accounts'], g['balance']), (2, 150))
        self.assertEqual(g['products'], ['Fanaka Saving Account', 'Savings Euro'])


class FacilitiesTests(SimpleTestCase):
    def test_placeholder_and_zero_limits_are_dropped(self):
        out = F.summarise([
            {'agreement': 'a', 'type': 'DYNAMIC LIMIT AGREEMENT', 'limit': 999_999_999_999, 'outstanding': 0},
            {'agreement': 'b', 'type': 'DYNAMIC LIMIT AGREEMENT', 'limit': 0, 'outstanding': 0},
        ])
        self.assertEqual(out['facilities'], [])
        self.assertIsNone(out['mobile'])

    def test_repaid_agreements_are_history_not_totals(self):
        out = F.summarise([
            {'agreement': 'm', 'type': 'PURCHASE OWNER OCCUPIER AGREEMENT', 'limit': 6_181_775,
             'outstanding': 4_411_838, 'issued': '2016-01-01'},
            {'agreement': 'p', 'type': 'STAFF PERSONAL LOANS AGREEMENT', 'limit': 3_600_000,
             'outstanding': 0, 'issued': '2022-10-26'},
        ])
        self.assertEqual(out['sanctioned_total'], 6_181_775)
        self.assertEqual(out['outstanding_total'], 4_411_838)
        self.assertEqual((out['active_count'], out['past_count']), (1, 1))
        self.assertEqual(out['facilities'][0]['type'], 'Purchase Owner Occupier')   # active first

    def test_mobile_loans_are_a_history_of_approved_amounts(self):
        # Customer 895 on live data: 10,000 then 3,000 then 6,000 (currently out).
        out = F.summarise([
            {'agreement': '1', 'type': 'MOBILE LOAN AGREEMENT', 'limit': 10000, 'issued': '2025-09-09', 'outstanding': 0},
            {'agreement': '2', 'type': 'MOBILE LOAN AGREEMENT', 'limit': 3000, 'issued': '2026-05-14', 'outstanding': 0},
            {'agreement': '3', 'type': 'THREE-MONTHS MOBILE LOAN AGREEMENT', 'limit': 6000, 'issued': '2026-09-18', 'outstanding': 6000},
            {'agreement': '4', 'type': 'WHIZZ SCHOOL FEES LOAN AGREEMENT', 'limit': 500, 'issued': '2024-01-02', 'outstanding': 0},
        ])
        mob = out['mobile']
        self.assertEqual(out['facilities'], [])
        self.assertEqual(mob['loans_taken'], 4)
        self.assertEqual((mob['latest_amount'], mob['latest_issued']), (6000, '2026-09-18'))
        self.assertEqual(mob['highest_amount'], 10000)
        self.assertEqual(mob['outstanding'], 6000)
        self.assertEqual([h['period'] for h in mob['history']],
                         ['2024-01-02', '2025-09-09', '2026-05-14', '2026-09-18'])


def _prof(**counts):
    p = A.empty_profile()
    for k, (n, v) in counts.items():
        p[k] = {'count': n, 'value': v}
    return p


class ActivityRuleTests(SimpleTestCase):
    def rules(self, prof, **kw):
        kw.setdefault('held', set())
        kw.setdefault('individual', True)
        kw.setdefault('segment', 'MASS')
        return {o['rule_id']: o for o in A.opportunities(prof, **kw)}

    def test_salaried_without_loan_or_savings(self):
        out = self.rules(_prof(salary=(3, 412_000)))
        self.assertIn('T1', out)
        self.assertIn('T2', out)
        self.assertIn('KES 412,000', out['T1']['evidence'][0])

    def test_one_salary_credit_is_not_salaried(self):
        self.assertEqual(self.rules(_prof(salary=(1, 50_000))), {})

    def test_held_products_suppress_their_rules(self):
        out = self.rules(_prof(salary=(3, 1)), held={'consumer', 'savings'})
        self.assertEqual(out, {})

    def test_idle_balance_to_fixed_deposit(self):
        self.assertIn('T3', self.rules(_prof(), liquid_balance=1_500_000))
        self.assertNotIn('T3', self.rules(_prof(), liquid_balance=1_500_000, held={'term_deposit'}))

    def test_government_and_scheme_accounts_get_no_sales_pitch(self):
        prof = _prof(payments_out=(220, 3.9e9), cash_in=(857, 1e7))
        for seg in ('INSTITUTIONAL BANKING', 'SCHEME', 'PROJECT FINANCE'):
            self.assertEqual(self.rules(prof, individual=False, segment=seg, liquid_balance=1.5e9), {}, seg)

    def test_overdraft_rule_is_for_businesses_only(self):
        prof = _prof(payments_out=(6, 1e6))
        self.assertIn('T4', self.rules(prof, individual=False, segment='LARGE ENTERPRISES'))
        self.assertNotIn('T4', self.rules(prof, individual=True, segment='PRIVATE'))

    def test_whizz_loan_is_for_retail_individuals(self):
        prof = _prof(mpesa_out=(121, 17_767_240))
        self.assertIn('T5', self.rules(prof, segment='SMALL ENTERPRISES'))
        self.assertNotIn('T5', self.rules(prof, individual=True, segment='MEDIUM ENTERPRISES'))
        self.assertNotIn('T5', self.rules(prof, individual=False, segment='SMALL ENTERPRISES'))
        self.assertNotIn('T5', self.rules(prof, held={'mobile_loan'}))

    def test_cash_only_customer_to_digital(self):
        self.assertIn('T6', self.rules(_prof(cash_in=(4, 1), cash_out=(2, 1))))
        # any digital use at all means they are not "cash only"
        self.assertNotIn('T6', self.rules(_prof(cash_in=(4, 1), cash_out=(2, 1), bills=(1, 1))))

    def test_card_rule_abstains_when_card_register_unknown(self):
        prof = _prof(cash_out=(5, 100_000))
        self.assertNotIn('T7', self.rules(prof, has_active_card=None))
        self.assertNotIn('T7', self.rules(prof, has_active_card=True))
        self.assertIn('T7', self.rules(prof, has_active_card=False))

    def test_unknown_customer_type_gets_only_the_deposit_rule(self):
        prof = _prof(salary=(3, 1), mpesa_out=(20, 1), payments_out=(9, 1))
        out = self.rules(prof, individual=None, segment='MASS', liquid_balance=2e6)
        self.assertEqual(set(out), {'T3'})

    def test_term_deposit_withdrawal_is_a_watch_signal(self):
        notes = A.watch_signals(_prof(td_withdrawal=(2, 300_000)))
        self.assertEqual(len(notes), 1)
        self.assertIn('KES 300,000', notes[0])

    def test_category_sql_covers_every_category(self):
        for c in A.CATEGORIES:
            self.assertIn(f"'{c}'", A.CATEGORY_SQL)


class CustomerProfileTests(SimpleTestCase):
    def test_placeholders_hidden_and_pep_flagged(self):
        out = CP.from_categories([
            {'cat': 'CRCAMLC', 'val': 'HIGH RISK'}, {'cat': 'PROFLEVL', 'val': 'MIGRATION'},
            {'cat': 'INCLEVEL', 'val': '200001 AND ABOVE'}, {'cat': 'SECRET', 'val': 'POLITICALLY EXPOSED'},
            {'cat': 'FAMILY', 'val': 'MARRIED'},
        ])
        self.assertEqual(out['aml_risk'], 'High risk')
        self.assertIsNone(out['employment'])            # MIGRATION is a placeholder
        self.assertEqual(out['income_band'], '200001 AND ABOVE')
        self.assertTrue(out['pep'])
        self.assertNotIn('marital', out)                 # a default, deliberately not shown
        self.assertEqual(CP.aml_tone(out['aml_risk']), 'neg')

    def test_revenue_months_and_net(self):
        rev = CP.revenue([
            {'month': 1, 'category': 'Interest_Income', 'value': 300_462},
            {'month': 1, 'category': 'NFI', 'value': 1_235},
            {'month': 1, 'category': 'Interest_Expenses', 'value': 76_325},
            {'month': 2, 'category': 'Interest_Income', 'value': 100},
        ], year=2026)
        self.assertEqual(rev['months'][0]['net'], 225_372)
        self.assertEqual(rev['months'][0]['period'], '2026-01-01')
        self.assertEqual(rev['totals']['interest_income'], 300_562)
        self.assertIsNone(CP.revenue([], year=2026))


class _Gw(WarehouseGateway):
    """Minimal gateway for the service: abstract methods stubbed, insight blocks set per test."""
    def __init__(self, **blocks):
        self.blocks = blocks
        self.calls = 0

    def _b(self, name):
        self.calls += 1
        v = self.blocks.get(name)
        if isinstance(v, Exception):
            raise v
        return v

    def get_product_mix(self, c): return self._b('products')
    def get_facilities(self, c): return self._b('facilities')
    def get_activity(self, c): return self._b('activity')
    def get_customer_profile(self, c): return self._b('profile')
    def get_revenue(self, c): return self._b('revenue')
    def has_active_card(self, c): return self.blocks.get('card')

    def as_of_date(self): return date(2026, 9, 29)
    def get_customer(self, c): return {'cust_id': c, 'segment': 'MASS', 'bio': {'customer_type': 'Individual'}}
    def search_customers(self, *a, **k): return []
    def list_customers(self, *a, **k): return []
    def get_product_holdings(self, c): return {'flags': {}, 'product_map': {}}
    def get_relationship_value(self, c): return {}
    def get_deposit_accounts(self, c): return []
    def get_loan_accounts(self, c): return []
    def deposit_loan_series(self, *a): return {}
    def disbursement_vs_balance_series(self, *a): return {}
    def transaction_series(self, *a): return []
    def channel_usage(self, *a): return []
    def recent_transactions(self, *a, **k): return []
    def portfolio_whole_book(self, *a): return None
    def portfolio_trends(self, *a): return None
    def get_risk_profile(self, c): return None
    def segment_product_benchmark(self, s): return 0.0
    def get_whizz(self, *a): return None
    def get_properties(self, c): return None
    def get_bancassurance(self, *a): return None


_MIX = P.summarise([{'level2': 'CURRENT ACCOUNT', 'product': 'TRANSACTIONAL ACCOUNT-CA', 'balance': 10}], [])
_MIX['liquid_balance'] = 10
_ACT = {'from': '2026-07-02', 'to': '2026-09-29', 'profile': _prof(salary=(3, 300_000))}


class InsightsServiceTests(SimpleTestCase):
    def setUp(self):
        insights.clear_cache()

    def test_a_failing_block_does_not_blank_the_others(self):
        gw = _Gw(products=_MIX, facilities=RuntimeError('boom'), activity=_ACT, profile=None, revenue=None)
        out = insights.build_customer_insights(gw, '1')
        self.assertEqual(out['facilities']['status'], 'unavailable')
        self.assertEqual(out['products']['status'], 'live')
        self.assertEqual(out['profile']['status'], 'none')
        rules = {o['rule_id'] for o in out['activity']['data']['opportunities']}
        self.assertEqual(rules, {'T1', 'T2'})

    def test_recent_mobile_borrower_is_not_offered_a_first_mobile_loan(self):
        # Customer 121669: 50 mobile loans, latest 2026-09-19, repaid - balance zero today.
        act = {'from': '2026-07-02', 'to': '2026-09-29', 'profile': _prof(mpesa_out=(142, 1_152_950))}
        fac = {'facilities': [], 'mobile': {'latest_issued': '2026-09-19', 'loans_taken': 50}}
        out = insights.build_customer_insights(_Gw(products=_MIX, facilities=fac, activity=act), '121669')
        self.assertNotIn('T5', {o['rule_id'] for o in out['activity']['data']['opportunities']})

        insights.clear_cache()
        old = {'facilities': [], 'mobile': {'latest_issued': '2024-01-02', 'loans_taken': 1}}
        out = insights.build_customer_insights(_Gw(products=_MIX, facilities=old, activity=act), '121669')
        self.assertIn('T5', {o['rule_id'] for o in out['activity']['data']['opportunities']})

    def test_whizz_loan_withheld_when_loan_history_unreadable(self):
        act = {'from': '2026-07-02', 'to': '2026-09-29', 'profile': _prof(mpesa_out=(142, 1))}
        out = insights.build_customer_insights(
            _Gw(products=_MIX, facilities=RuntimeError('x'), activity=act), '5')
        self.assertNotIn('T5', {o['rule_id'] for o in out['activity']['data']['opportunities']})

    def test_opportunities_withheld_without_holdings(self):
        gw = _Gw(products=RuntimeError('x'), activity=_ACT)
        act = insights.build_customer_insights(gw, '1')['activity']
        self.assertEqual(act['data']['opportunities'], [])
        self.assertIn('withheld', act['data']['opportunities_note'])

    def test_complete_answer_is_cached_failed_one_is_not(self):
        ok = _Gw(products=_MIX, activity=_ACT)
        insights.build_customer_insights(ok, '7')
        n = ok.calls
        insights.build_customer_insights(ok, '7')
        self.assertEqual(ok.calls, n)                   # served from cache

        bad = _Gw(products=RuntimeError('x'))
        insights.build_customer_insights(bad, '8')
        n = bad.calls
        insights.build_customer_insights(bad, '8')
        self.assertGreater(bad.calls, n)                # retried, not frozen


class MergeTests(SimpleTestCase):
    def _c(self, product, rule='T1'):
        return Candidate(product=product, product_name=product, domain='HFCB', reason='r',
                         rule_id=rule, base_score=0.5)

    def test_activity_leads_but_leaves_room_for_the_model(self):
        act = [self._c('unsecured'), self._c('savings', 'T2'), self._c('mobile', 'T6')]
        ml = [self._c('mortgage', 'ml.lgbm-v1')]
        out = engine._merge_activity(act, ml, 3)
        self.assertEqual([c.product for c in out], ['unsecured', 'savings', 'mortgage'])

    def test_no_duplicate_products(self):
        out = engine._merge_activity([self._c('savings')], [self._c('savings', 'A'), self._c('mobile', 'A')], 3)
        self.assertEqual([c.product for c in out], ['savings', 'mobile'])

    def test_activity_fills_all_slots_when_nothing_else(self):
        act = [self._c('a'), self._c('b'), self._c('c')]
        self.assertEqual(len(engine._merge_activity(act, [], 3)), 3)


class _Conn:
    """Fake Trino connector for the whole-book activity list."""
    def execute(self, sql, params=None):
        s = sql.lower()
        if 'bank_parameters' in s:
            return [{'d': date(2026, 9, 29)}]
        if 'max(eom_date)' in s:
            return [{'d': date(2026, 9, 29)}]
        if 'fact_dep_trx_recording' in s and 'customer_number' in s:
            base = {f'n_{c}': 0 for c in A.CATEGORIES} | {f'v_{c}': 0 for c in A.CATEGORIES}
            return [
                {**base, 'customer_number': 214177, 'cid': Decimal('214177.00'),
                 'full_name': 'SALARIED PERSON ', 'customer_segment': 'PRIVATE',
                 'account_branch_name': 'EMBU BRANCH', 'employer': None, 'fk_bankemployeeid': None,
                 'cust_type': Decimal('1'), 'n_salary': 4, 'v_salary': Decimal('3004520'),
                 'liquid': Decimal('10'), 'has_card': 1,
                 **{k: 0 for k in ('h_term_deposit', 'h_call_deposit', 'h_savings', 'h_notice',
                                   'h_consumer', 'h_vuna_hela', 'h_mobile_loan', 'h_overdraft',
                                   'h_working_capital')}},
                {**base, 'customer_number': 1221191, 'cid': Decimal('1221191.00'),
                 'full_name': 'THE NATIONAL TREASURY', 'customer_segment': 'INSTITUTIONAL BANKING',
                 'account_branch_name': 'X', 'employer': None, 'fk_bankemployeeid': None,
                 'cust_type': Decimal('2'), 'n_payments_out': 220, 'v_payments_out': Decimal('3.9e9'),
                 'liquid': Decimal('1537377504'), 'has_card': 0,
                 **{k: 0 for k in ('h_term_deposit', 'h_call_deposit', 'h_savings', 'h_notice',
                                   'h_consumer', 'h_vuna_hela', 'h_mobile_loan', 'h_overdraft',
                                   'h_working_capital')}},
            ]
        return []


class ActivityProspectsGatewayTests(SimpleTestCase):
    def test_ids_are_clean_and_institutions_excluded(self):
        gw = TrinoWarehouse(_Conn())
        out = gw.activity_prospects()
        ids = {r['cust_id'] for r in out['results']}
        self.assertEqual(ids, {'214177'})               # not '214177.00', and no Treasury
        self.assertEqual({r['rule_id'] for r in out['results']}, {'T1', 'T2'})
        self.assertEqual(out['results'][0]['name'], 'SALARIED PERSON')
