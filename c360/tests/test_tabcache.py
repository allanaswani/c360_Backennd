"""The customer-tab cache: same answer within ten minutes, never across a new
warehouse day, and never a cached error or a cached "not found"."""
from unittest import mock

from django.test import SimpleTestCase

from c360.services import tabcache


class TabCacheTests(SimpleTestCase):
    def setUp(self):
        tabcache.clear()
        self.calls = 0

    def build(self):
        self.calls += 1
        return {'n': self.calls}

    def test_second_read_within_ttl_is_served_from_cache(self):
        k = tabcache.key('hfcb', '1039973', '2026-10-01')
        self.assertEqual(tabcache.get_or_build(k, self.build), {'n': 1})
        self.assertEqual(tabcache.get_or_build(k, self.build), {'n': 1})
        self.assertEqual(self.calls, 1)

    def test_a_new_warehouse_day_is_a_new_entry(self):
        tabcache.get_or_build(tabcache.key('hfcb', '1', '2026-10-01'), self.build)
        tabcache.get_or_build(tabcache.key('hfcb', '1', '2026-10-02'), self.build)
        self.assertEqual(self.calls, 2)

    def test_expired_entries_are_rebuilt(self):
        k = tabcache.key('detail', '1', '2026-10-01')
        tabcache.get_or_build(k, self.build)
        with mock.patch('c360.services.tabcache.time.time', return_value=10**10):
            tabcache.get_or_build(k, self.build)
        self.assertEqual(self.calls, 2)

    def test_errors_and_not_found_are_not_kept(self):
        k = tabcache.key('detail', '1', '2026-10-01')
        with self.assertRaises(RuntimeError):
            tabcache.get_or_build(k, mock.Mock(side_effect=RuntimeError('down')))
        self.assertIsNone(tabcache.get_or_build(k, lambda: None))
        self.assertEqual(tabcache.get_or_build(k, self.build), {'n': 1})
