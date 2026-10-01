"""Whizz, Properties and Bancassurance tabs: the rules added 2026-10-01.

Each rule here came from a check against the live warehouse:

* Whizz: o_final_acc_amount is signed and includes the charge, so money received,
  money sent and charges are separable; a non-movement (amount 0) is not a
  transaction.
* Properties: rpt_c360_customer_property.perc_paid is 0 on 9,686 of 9,708 units, so
  paid-to-date comes from the payment register, and a unit with no payment on
  record is "not known", never "0% paid".
* Bancassurance: the policy book repeats a policy at every annual renewal, so the
  premium a customer pays now is the premium on policies in force, not the sum of
  every renewal ever written.
"""
from datetime import date, timedelta

from django.test import SimpleTestCase

from c360.services import domains
from c360.warehouse.periods import resolve_period

PERIOD = resolve_period('30D', as_of=date(2026, 9, 30))


class _Gateway:
    def __init__(self, **data):
        self.data = data

    def get_customer(self, cust_id):
        return {'cust_id': cust_id}

    def get_facilities(self, cust_id):
        return None

    def get_whizz(self, cust_id, period):
        return self.data.get('whizz')

    def get_properties(self, cust_id):
        return self.data.get('properties')

    def get_bancassurance(self, cust_id, period):
        return self.data.get('bancassurance')


def _tile(out, label):
    return next((m for m in out['metrics'] if m['label'] == label), None)


def _chart(out, cid):
    return next((c for c in out['charts'] if c['id'] == cid), None)


def _table(out, tid):
    return next((t for t in out['tables'] if t['id'] == tid), None)


WHIZZ = {
    'status': 'Transacting', 'registered_since': None,
    'txn_count': 3, 'txn_value': 470_000, 'money_in': 200_000, 'money_out': 270_152, 'charges': 152,
    'services_used': 2,
    'activity': [
        {'period': '2026-09-02', 'count': 1, 'value': 200_000, 'in': 200_000, 'out': 0},
        {'period': '2026-09-15', 'count': 2, 'value': 270_000, 'in': 0, 'out': 270_152},
    ],
    'categories': [{'label': 'Send to M-Pesa', 'count': 2, 'value': 270_000},
                   {'label': 'M-Pesa to account', 'count': 1, 'value': 200_000}],
    'recent': [{'date': '2026-09-15', 'description': 'Send to M-Pesa', 'amount': -135_076, 'currency': 'KES'}],
}


class WhizzTests(SimpleTestCase):
    def test_received_sent_and_charges_are_separate_tiles(self):
        out = domains.build_whizz(_Gateway(whizz=WHIZZ), '1', PERIOD)
        self.assertEqual(_tile(out, 'Received via Whizz')['value'], 200_000)
        self.assertEqual(_tile(out, 'Sent via Whizz')['value'], 270_152)
        self.assertEqual(_tile(out, 'Charges paid')['value'], 152)
        self.assertIsNone(_tile(out, 'Value moved'))

    def test_flow_chart_has_every_week_including_quiet_ones(self):
        out = domains.build_whizz(_Gateway(whizz=WHIZZ), '1', PERIOD)
        flow = _chart(out, 'flow')
        self.assertEqual(flow['seriesNames'], ['Received', 'Sent'])
        self.assertEqual(sum(b['a'] for b in flow['data']), 200_000)
        self.assertEqual(sum(b['b'] for b in flow['data']), 270_152)
        self.assertTrue(any(b['a'] == 0 and b['b'] == 0 for b in flow['data']))

    def test_service_donut_is_by_count_not_a_copy_of_the_value_bars(self):
        out = domains.build_whizz(_Gateway(whizz=WHIZZ), '1', PERIOD)
        self.assertEqual(_chart(out, 'service_mix')['fmt'], 'count')
        self.assertEqual({d['value'] for d in _chart(out, 'service_mix')['data']}, {2, 1})

    def test_transactions_table_totals_signed_amounts(self):
        out = domains.build_whizz(_Gateway(whizz=WHIZZ), '1', PERIOD)
        self.assertTrue(_table(out, 'recent')['flow'])

    def test_without_direction_the_single_value_view_is_kept(self):
        legacy = {k: v for k, v in WHIZZ.items() if k not in ('money_in', 'money_out', 'charges')}
        out = domains.build_whizz(_Gateway(whizz=legacy), '1', PERIOD)
        self.assertIsNotNone(_tile(out, 'Value moved'))
        self.assertIsNone(_chart(out, 'flow'))
        self.assertFalse(_table(out, 'recent')['flow'])


def _unit(uid, value, paid=None, project='Kahua Residence', mortgage=False):
    row = {'unit_id': uid, 'unit': f'C{uid}', 'project': project, 'value': value, 'mortgage': mortgage}
    if paid is None:
        row['paid_pct'] = None
    else:
        row.update(paid=paid, outstanding=max(value - paid, 0), paid_pct=min(paid / value, 1),
                   last_payment='2026-08-01', payments=2)
    return row


class PropertiesTests(SimpleTestCase):
    def _build(self, units, payments=()):
        data = {'properties': units, 'payments': list(payments), 'payments_available': True}
        return domains.build_properties(_Gateway(properties=data), '1', PERIOD)

    def test_unit_without_payments_is_not_known_rather_than_zero(self):
        out = self._build([_unit(1, 9_000_000, paid=955_000), _unit(2, 5_000_000)])
        self.assertEqual(_tile(out, 'Paid to date')['value'], 955_000)
        self.assertEqual(_tile(out, 'Still to pay')['value'], 8_045_000)
        self.assertIn('1 has no payment on record', _tile(out, 'Paid to date')['meta'])
        meters = _chart(out, 'progress')['data']
        self.assertEqual(len(meters), 1, 'a unit with no record must not draw as 0% paid')

    def test_no_payments_anywhere_says_not_stated(self):
        out = self._build([_unit(2, 5_000_000)])
        tile = _tile(out, 'Paid to date')
        self.assertIsNone(tile['value'])
        self.assertEqual(tile['status'], domains.TO_SOURCE)
        self.assertIsNone(_chart(out, 'progress'))

    def test_payments_over_time_includes_months_with_nothing_paid(self):
        pays = [{'unit_id': 1, 'date': '2026-05-03', 'amount': 300_000, 'mode': 'EFT'},
                {'unit_id': 1, 'date': '2026-08-01', 'amount': 400_000, 'mode': 'Bank-Deposit'}]
        out = self._build([_unit(1, 9_000_000, paid=700_000)], pays)
        series = _chart(out, 'paid_over_time')
        self.assertEqual([b['label'] for b in series['data']], ['May 2026', 'Jun 2026', 'Jul 2026', 'Aug 2026'])
        self.assertEqual([b['a'] for b in series['data']], [300_000, 0, 0, 400_000])
        self.assertEqual(series['seriesNames'], ['Paid'])
        self.assertEqual({d['label'] for d in _chart(out, 'pay_mode')['data']}, {'EFT', 'Bank-Deposit'})
        rows = _table(out, 'payments')['rows']
        self.assertEqual(rows[0]['unit'], 'Kahua Residence · C1')

    def test_long_history_is_quarterly(self):
        pays = [{'unit_id': 1, 'date': '2022-01-10', 'amount': 1, 'mode': 'EFT'},
                {'unit_id': 1, 'date': '2026-08-01', 'amount': 1, 'mode': 'EFT'}]
        out = self._build([_unit(1, 9_000_000, paid=2)], pays)
        labels = [b['label'] for b in _chart(out, 'paid_over_time')['data']]
        self.assertEqual(labels[0], 'Q1 2022')
        self.assertEqual(labels[-1], 'Q3 2026')
        self.assertEqual(len(labels), 19)

    def test_one_project_and_one_slice_draw_no_donut(self):
        out = self._build([_unit(1, 9_000_000, paid=1)])
        self.assertIsNone(_chart(out, 'by_project'))
        self.assertIsNone(_chart(out, 'financed'))


def _policy(status, premium, start, end, insured=1_000_000, cover='Mortgage Insurance', insurer='Britam'):
    return {'policy': None, 'product': cover, 'cover_class': cover, 'insurer': insurer, 'premium': premium,
            'sum_insured': insured, 'status': status, 'start': start, 'end': end, 'matched_by': 'national ID'}


class BancassuranceTests(SimpleTestCase):
    def _build(self, policies, claims=None, match_note=None):
        data = {'policies': policies, 'active': 0, 'expired': 0, 'unnumbered': 0, 'phone_matched': 0,
                'name_matched': 0, 'receipts': None, 'claims': claims, 'match_note': match_note}
        return domains.build_bancassurance(_Gateway(bancassurance=data), '1', PERIOD)

    def test_premium_tiles_count_only_policies_in_force(self):
        nxt = (date.today() + timedelta(days=200)).isoformat()
        pols = [_policy('Active', 5_223, '2026-05-27', nxt, insured=0, cover='Travel Insurance', insurer='AIG'),
                _policy('Expired', 300_000, '2024-01-01', '2024-12-31'),
                _policy('Expired', 290_000, '2023-01-01', '2023-12-31')]
        out = self._build(pols)
        self.assertEqual(_tile(out, 'Annual premium in force')['value'], 5_223)
        self.assertEqual(_tile(out, 'Premium on all policies')['value'], 595_223)
        self.assertIsNone(_tile(out, 'Sum insured in force')['value'])
        self.assertIn('next renewal', _tile(out, 'Policies')['meta'])

    def test_class_and_insurer_charts(self):
        pols = [_policy('Expired', 100, '2024-01-01', '2024-12-31'),
                _policy('Expired', 50, '2024-01-01', '2024-12-31', cover='Life Insurance', insurer='Prudential')]
        out = self._build(pols)
        self.assertEqual({d['label'] for d in _chart(out, 'mix')['data']}, {'Mortgage Insurance', 'Life Insurance'})
        self.assertEqual({d['label'] for d in _chart(out, 'by_insurer')['data']}, {'Britam', 'Prudential'})
        self.assertIn('insurer', _table(out, 'policies')['columns'])

    def test_claims_are_a_table_and_the_match_note_is_shown(self):
        claims = {'total': 3, 'by_status': {'Closed': 2, 'On Progress': 1}, 'latest_incident': '2026-04-02',
                  'amounts_available': False}
        out = self._build([_policy('Expired', 100, '2024-01-01', '2024-12-31')], claims=claims,
                          match_note='Worth a check: 1 matched by phone number rather than ID.')
        rows = _table(out, 'claims')['rows']
        self.assertEqual(rows, [{'status': 'Closed', 'claims': 2}, {'status': 'On Progress', 'claims': 1}])
        self.assertIn('2026-04-02', _table(out, 'claims')['note'])
        self.assertTrue(out['note'].startswith('Worth a check'))

    def test_no_claims_no_claims_table(self):
        out = self._build([_policy('Expired', 100, '2024-01-01', '2024-12-31')])
        self.assertIsNone(_table(out, 'claims'))
