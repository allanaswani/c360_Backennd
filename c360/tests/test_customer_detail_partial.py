"""A slow warehouse must not fail the whole customer page (10-12 Nov 2026: the
relationship-value read hit EXCEEDED_TIME_LIMIT and every open was a 500). The page
comes back with its header and no value, and that partial page is not cached."""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from c360.services import tabcache
from c360.tests.test_sso import _pin_mock
from c360.warehouse.mock import seed


class PartialCustomerDetailTests(TestCase):
    def setUp(self):
        _pin_mock()
        tabcache._cache.clear()
        self.c = APIClient()
        self.cid = next(iter(seed.CUSTOMER_INDEX))

    def _get(self):
        return self.c.get(f'/api/customers/{self.cid}/', HTTP_X_C360_ADMIN='1')

    def test_value_timeout_returns_the_header_not_a_500(self):
        with mock.patch('c360.api.views.build_value_summary', side_effect=RuntimeError('EXCEEDED_TIME_LIMIT')):
            r = self._get()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['header']['cust_id'], self.cid)
        self.assertIsNone(body['value_summary'])
        self.assertEqual(body['unavailable'], ['value_summary'])
        self.assertIsNone(body['header']['summary'])

    def test_the_partial_page_is_not_cached(self):
        with mock.patch('c360.api.views.build_value_summary', side_effect=RuntimeError('down')):
            self._get()
        body = self._get().json()
        self.assertIsNotNone(body['value_summary'])
        self.assertNotIn('unavailable', body)
