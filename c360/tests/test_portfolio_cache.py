"""The whole-book cache: one build per key at a time, and an expired entry served
while a single background rebuild runs."""
import threading
import time
from unittest import mock

from django.test import SimpleTestCase

from c360.services import portfolio_cache as pc


class PortfolioCacheTests(SimpleTestCase):
    def setUp(self):
        pc.invalidate()
        pc._refreshing.clear()

    def test_fresh_hit_is_cached(self):
        b = mock.Mock(return_value={'v': 1})
        pc.get_or_build('k', b, ttl=60)
        payload, meta = pc.get_or_build('k', b, ttl=60)
        self.assertEqual((payload, meta['cached'], b.call_count), ({'v': 1}, True, 1))

    def test_concurrent_misses_build_once(self):
        calls = []

        def slow():
            calls.append(1)
            time.sleep(0.2)
            return {'v': len(calls)}
        out = []
        ts = [threading.Thread(target=lambda: out.append(pc.get_or_build('k', slow, ttl=60)[0])) for _ in range(5)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len(calls), 1)
        self.assertEqual(out, [{'v': 1}] * 5)

    def test_expired_entry_is_served_while_one_refresh_runs(self):
        pc.get_or_build('k', lambda: {'v': 1}, ttl=60)
        started = []
        with mock.patch.object(pc, '_start', side_effect=lambda k, b: started.append(k)), \
                mock.patch.object(pc.time, 'time', return_value=time.time() + 90):
            first = pc.get_or_build('k', lambda: {'v': 2}, ttl=60)
            second = pc.get_or_build('k', lambda: {'v': 2}, ttl=60)
        self.assertEqual(first[0], {'v': 1})
        self.assertTrue(first[1]['refreshing'])
        self.assertEqual(second[0], {'v': 1})
        self.assertEqual(started, ['k'])           # one refresh, not two

    def test_refresh_replaces_entry_and_failure_keeps_old(self):
        pc.get_or_build('k', lambda: {'v': 1}, ttl=60)
        pc._refreshing.add('k')
        pc._refresh('k', mock.Mock(side_effect=RuntimeError('down')))
        self.assertEqual(pc._store['k'][1], {'v': 1})
        self.assertNotIn('k', pc._refreshing)
        pc._refresh('k', lambda: {'v': 2})
        self.assertEqual(pc._store['k'][1], {'v': 2})

    def test_very_old_entry_is_rebuilt_in_request(self):
        pc.get_or_build('k', lambda: {'v': 1}, ttl=60)
        with mock.patch.object(pc.time, 'time', return_value=time.time() + 60 * pc.STALE_FACTOR + 5):
            payload, meta = pc.get_or_build('k', lambda: {'v': 2}, ttl=60)
        self.assertEqual((payload, meta['cached']), ({'v': 2}, False))
