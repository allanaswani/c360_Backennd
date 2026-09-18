"""The insurance panel has to fit the data that actually exists.

Reported on HFCB LIMITED (433997): the graphs are very long. They were, and worse
than long - uninformative - and the cause was in the feed rather than the chart code.

Measured on the live policy summary (58,504 rows, 2026-09-18):

* ``product`` is BLANK on every single row. All 58,504.
* ``policy_total_premium`` is populated; only 128 rows are zero.
* ``policy_sum_insured`` is zero on 12,259 rows (21%).
* The largest client holds 32 policies; 433997 holds 28.

So "Premium by product" was a one-slice donut for every customer in the bank, and
both bar charts drew one bar per policy labelled with the blank product name - 28
bars reading the same words. Premium by year turns that into eight bars that answer
a question an RM actually has.
"""
from django.test import SimpleTestCase

from c360.services.domains import build_bancassurance
from c360.warehouse.periods import resolve_period
from datetime import date


def _policy(end_year, premium=100_000, insured=1_000_000, status='expired'):
    return {'policy': None, 'product': 'Insurance policy', 'premium': premium,
            'sum_insured': insured, 'status': status.title(),
            'start': f'{end_year}-01-01', 'end': f'{end_year}-12-31',
            'matched_by': 'national ID'}


class _Gateway:
    """Only what build_bancassurance asks of a gateway."""

    def __init__(self, payload):
        self.payload = payload

    def get_customer(self, cust_id):
        return {'cust_id': cust_id, 'name': 'HFCB LIMITED'}

    def get_bancassurance(self, cust_id, period):
        return self.payload


def _build(payload):
    period = resolve_period('30D', as_of=date(2026, 9, 17))
    return build_bancassurance(_Gateway(payload), '433997', period)


#: 28 policies across eight years, exactly as 433997 holds them.
_YEARS = [2020, 2021, 2022, 2023, 2024, 2025, 2026, 2027]
MANY = {'policies': [_policy(y + (i * 0)) for i in range(4) for y in _YEARS[:7]],
        'active': 2, 'expired': 26, 'unnumbered': 18,
        'phone_matched': 0, 'name_matched': 0, 'receipts': None, 'claims': None}


class ChartShapeTests(SimpleTestCase):
    def test_no_chart_draws_one_bar_per_policy(self):
        """28 bars labelled identically is not a chart."""
        out = _build(MANY)
        for chart in out['charts']:
            self.assertLess(len(chart['data']), len(MANY['policies']), chart['id'])

    def test_premium_is_grouped_by_year(self):
        out = _build(MANY)
        by_year = [c for c in out['charts'] if c['id'] == 'by_year']
        self.assertEqual(len(by_year), 1)
        self.assertEqual([d['label'] for d in by_year[0]['data']],
                         [str(y) for y in _YEARS[:7]])

    def test_the_product_donut_is_omitted_when_the_feed_names_no_product(self):
        """product is blank on all 58,504 rows, so this chart would be a single slice
        labelled 'Insurance policy' for every customer in the bank."""
        self.assertEqual([c for c in _build(MANY)['charts'] if c['id'] == 'mix'], [])

    def test_the_product_donut_returns_if_the_feed_ever_names_products(self):
        payload = dict(MANY)
        payload['policies'] = [
            {**_policy(2025), 'product': 'Motor'},
            {**_policy(2025), 'product': 'Fire'},
        ]
        mix = [c for c in _build(payload)['charts'] if c['id'] == 'mix']
        self.assertEqual(len(mix), 1)
        self.assertEqual({d['label'] for d in mix[0]['data']}, {'Motor', 'Fire'})

    def test_a_single_year_needs_no_chart(self):
        payload = dict(MANY)
        payload['policies'] = [_policy(2025), _policy(2025)]
        self.assertEqual(_build(payload)['charts'], [])


class SumInsuredTests(SimpleTestCase):
    """Zero on 21% of the book, so a bare KES 0 would read as 'no cover' when the
    truth is 'the feed does not say'."""

    def test_partial_coverage_is_counted_not_hidden(self):
        payload = dict(MANY)
        payload['policies'] = [_policy(2024, insured=0), _policy(2025, insured=5_000_000)]
        metric = [m for m in _build(payload)['metrics'] if m['label'] == 'Sum insured'][0]
        self.assertEqual(metric['value'], 5_000_000)
        self.assertIn('1 of 2', metric['meta'])

    def test_no_sum_insured_anywhere_is_not_sourced_rather_than_zero(self):
        payload = dict(MANY)
        payload['policies'] = [_policy(2024, insured=0), _policy(2025, insured=0)]
        metric = [m for m in _build(payload)['metrics'] if m['label'] == 'Sum insured'][0]
        self.assertIsNone(metric['value'])
        self.assertEqual(metric['status'], 'to_source')

    def test_full_coverage_carries_no_caveat(self):
        payload = dict(MANY)
        payload['policies'] = [_policy(2024), _policy(2025)]
        metric = [m for m in _build(payload)['metrics'] if m['label'] == 'Sum insured'][0]
        self.assertIsNone(metric.get('meta'))


class ClaimsTests(SimpleTestCase):
    """223 claims across 11,105 clients. Rare, and that is exactly why it belongs on
    the page: an RM walking into a renewal without knowing about an open claim is the
    failure this prevents. 433997 has one, on progress, incident 2026-01-10."""

    def test_a_claim_is_surfaced_as_its_own_metric(self):
        payload = dict(MANY)
        payload['claims'] = {'total': 1, 'by_status': {'On Progress': 1},
                             'latest_incident': '2026-01-10', 'amounts_available': False}
        metric = [m for m in _build(payload)['metrics'] if m['label'] == 'Claims']
        self.assertEqual(len(metric), 1)
        self.assertEqual(metric[0]['value'], 1)
        self.assertIn('on progress', metric[0]['meta'])

    def test_no_claim_means_no_tile(self):
        self.assertEqual([m for m in _build(MANY)['metrics'] if m['label'] == 'Claims'], [])

    def test_the_payload_carries_the_detail_for_the_panel(self):
        payload = dict(MANY)
        payload['claims'] = {'total': 3, 'by_status': {'Closed': 2, 'On Progress': 1},
                             'latest_incident': '2026-01-10', 'amounts_available': False}
        out = _build(payload)
        self.assertEqual(out['claims']['total'], 3)
        # No money total: 181 of the 223 carry no estimated loss, so a sum would
        # describe a fifth of them and be read as all of them.
        self.assertFalse(out['claims']['amounts_available'])
