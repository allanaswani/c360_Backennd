"""Observability backbone — collector math, buffered audit, the instrumentation
middleware, the admin dashboard/audit APIs, the telemetry beacon and /metrics."""
from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from c360 import observability as obs
from c360.models import AuditEvent


class CollectorMathTests(TestCase):
    def test_percentile_nearest_rank(self):
        vals = list(range(1, 101))            # 1..100
        self.assertEqual(obs.percentile(vals, 0.50), 51)
        self.assertEqual(obs.percentile(vals, 0.95), 95)   # nearest-rank: idx round(0.95*99)=94 -> 95
        self.assertEqual(obs.percentile([], 0.95), 0)
        self.assertEqual(obs.percentile([42], 0.99), 42)

    def test_bucket_rollup(self):
        from datetime import datetime, timezone
        b = obs._Bucket()
        for ms, st in [(10, 200), (20, 200), (500, 500), (30, 404)]:
            b.add(st, ms)
        r = b.rollup(datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(r['count'], 4)
        self.assertEqual(r['errors'], 1)         # one 5xx
        self.assertEqual(r['client_errors'], 1)  # one 4xx
        self.assertEqual(r['max_ms'], 500)
        self.assertEqual(r['by_status'], {'2xx': 2, '5xx': 1, '4xx': 1})


class AuditBufferTests(TestCase):
    def test_cap_and_drain(self):
        buf = obs._AuditBuffer(cap=3)
        for i in range(5):
            buf.record(kind='api', route=f'/r/{i}')
        items, dropped = buf.drain()
        self.assertEqual(len(items), 3)
        self.assertEqual(dropped, 2)
        # drained clean
        again, _ = buf.drain()
        self.assertEqual(again, [])


class MiddlewareTests(TestCase):
    def setUp(self):
        self.c = APIClient()
        obs.audit_buffer.drain()   # isolate from other tests' events

    def test_request_records_metric_and_buffers_audit(self):
        before = obs.collector.live()[3]         # cumulative total
        r = self.c.get('/api/meta/')
        self.assertEqual(r.status_code, 200)
        self.assertGreater(obs.collector.live()[3], before)
        events, _ = obs.audit_buffer.drain()
        meta_events = [e for e in events if 'meta' in e['route']]
        self.assertTrue(meta_events)
        e = meta_events[0]
        self.assertEqual(e['kind'], 'api')
        self.assertEqual(e['method'], 'GET')
        self.assertEqual(e['status'], 200)
        self.assertIsInstance(e['duration_ms'], int)
        # normalised route, not a raw path with ids
        self.assertTrue(e['route'].startswith('/'))

    def test_normalised_route_hides_ids(self):
        obs.audit_buffer.drain()
        self.c.get('/api/customers/HF-100238/')
        events, _ = obs.audit_buffer.drain()
        cust = [e for e in events if 'customers' in e['route']]
        self.assertTrue(cust)
        # the id is captured as target, but the route is the bounded pattern
        self.assertIn('cust_id', cust[0]['route'])
        self.assertNotIn('HF-100238', cust[0]['route'])
        self.assertEqual(cust[0]['target'], 'HF-100238')


class ObservabilityApiTests(TestCase):
    def setUp(self):
        self.c = APIClient()

    def test_overview_admin_only(self):
        self.assertEqual(self.c.get('/api/observability/overview/').status_code, 403)
        r = self.c.get('/api/observability/overview/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        self.assertIn('summary', r.json())
        self.assertIn('series', r.json())

    def test_audit_list_admin_only_and_returns_flushed(self):
        obs.audit_buffer.drain()
        self.c.get('/api/meta/')                 # buffers an event
        obs._flush_once(30, 90, prune=False)     # persist buffered events
        self.assertEqual(self.c.get('/api/observability/audit/').status_code, 403)
        r = self.c.get('/api/observability/audit/', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()['count'], 1)
        self.assertTrue(AuditEvent.objects.exists())

    def test_metrics_endpoint(self):
        r = self.c.get('/metrics')
        self.assertEqual(r.status_code, 200)
        self.assertIn('c360_up', r.content.decode())
        self.assertIn('text/plain', r['Content-Type'])

    def test_telemetry_collect_buffers_clicks(self):
        obs.audit_buffer.drain()
        r = self.c.post('/api/telemetry/collect/',
                        {'events': [{'kind': 'click', 'route': '/customers', 'target': 'nbp-accept'},
                                    {'kind': 'nav', 'path': '/portfolio'}]}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['accepted'], 2)
        events, _ = obs.audit_buffer.drain()
        kinds = {e['kind'] for e in events}
        self.assertEqual(kinds, {'click', 'nav'})


class UptimeFromHeartbeatsTests(TestCase):
    """Uptime must be a measurement, not an inference from traffic.

    The board divided minutes-that-recorded-a-request by minutes-in-window, so a
    healthy instance nobody happened to be using reported single-digit uptime in
    red on an operations screen."""

    def setUp(self):
        self.c = APIClient()

    def test_quiet_but_alive_is_not_an_outage(self):
        from datetime import timedelta
        from django.utils import timezone
        from c360.models import AppHeartbeat
        now = timezone.now().replace(second=0, microsecond=0)
        # Alive for every minute of the last hour, and not one request served.
        for i in range(60):
            AppHeartbeat.objects.create(minute=now - timedelta(minutes=i))

        r = self.c.get('/api/observability/overview/?window=60', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        summary = r.json()['summary']
        self.assertEqual(summary['requests'], 0)
        self.assertGreaterEqual(summary['uptime_pct'], 99)

    def test_uptime_is_null_when_nothing_is_recording_it(self):
        """Better to say it isn't measured than to invent 0% or a cheerful 100%."""
        r = self.c.get('/api/observability/overview/?window=60', HTTP_X_C360_ADMIN='1')
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()['summary']['uptime_pct'])

    def test_short_history_is_measured_over_what_exists(self):
        """A deploy three minutes old has no evidence about the preceding hour;
        dividing by the full window would report it as a near-total outage."""
        from datetime import timedelta
        from django.utils import timezone
        from c360.models import AppHeartbeat
        now = timezone.now().replace(second=0, microsecond=0)
        for i in range(3):
            AppHeartbeat.objects.create(minute=now - timedelta(minutes=i))

        summary = self.c.get('/api/observability/overview/?window=60',
                             HTTP_X_C360_ADMIN='1').json()['summary']
        self.assertEqual(summary['uptime_pct'], 100.0)
        self.assertLessEqual(summary['uptime_measured_minutes'], 5)

    def test_flusher_stamps_a_heartbeat(self):
        from c360 import observability as obs
        from c360.models import AppHeartbeat
        obs._flush_once(retain_metric_days=30, retain_audit_days=90, prune=False)
        self.assertGreater(AppHeartbeat.objects.count(), 0)
