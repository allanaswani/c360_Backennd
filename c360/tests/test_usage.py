"""Daily per-user usage rollup (c360/usage.py) and the push command's refusal to send
without its settings."""
from datetime import datetime, timezone as tz
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from c360 import usage
from c360.models import AuditEvent


def ev(ts, user='rm.one', kind='api', route='/api/customers/<str:cust_id>/', status=200,
       target='101', session='s1'):
    return {'ts': ts, 'username': user, 'kind': kind, 'route': route, 'method': 'GET',
            'status': status, 'target': target, 'session': session}


class RollupTests(SimpleTestCase):
    def test_days_are_cut_in_nairobi_time(self):
        # 22:30 UTC on the 29th is 01:30 on the 30th in Nairobi.
        rows = usage.rollup([ev(datetime(2026, 9, 29, 22, 30, tzinfo=tz.utc))])
        self.assertEqual(rows[0]['day'], '2026-09-30')

    def test_counts_customers_searches_and_features(self):
        t = datetime(2026, 9, 30, 8, 0, tzinfo=tz.utc)
        rows = usage.rollup([
            ev(t, target='101'), ev(t, target='101'), ev(t, target='202'),
            ev(t, route='/api/customers/', target=''),
            ev(t, route='/api/customers/<str:cust_id>/insights/'),
            ev(t, route='/api/observability/export/', target=''),
            ev(t, route='/api/customers/<str:cust_id>/', status=404, target='999'),
            ev(t, kind='nav', route='/customers/101'),
            ev(t, user='', target='303'),                       # anonymous: skipped
        ])
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['customer_views'], 3)                # the 404 is not a view
        self.assertEqual(r['distinct_customers'], 2)
        self.assertEqual(r['searches'], 1)
        self.assertEqual(r['exports'], 1)
        self.assertEqual(r['page_views'], 1)
        self.assertEqual(r['features']['insights'], 1)
        self.assertEqual(r['features']['customer_profile'], 3)
        self.assertEqual(r['sessions'], 1)

    def test_usernames_are_case_folded(self):
        t = datetime(2026, 9, 30, 8, 0, tzinfo=tz.utc)
        rows = usage.rollup([ev(t, user='Jane.Doe'), ev(t, user='jane.doe')])
        self.assertEqual([r['username'] for r in rows], ['jane.doe'])

    def test_feature_map_exact_vs_prefix(self):
        self.assertEqual(usage.feature_for('/api/customers/'), 'search')
        self.assertEqual(usage.feature_for('/api/customers/<str:cust_id>/'), 'customer_profile')
        self.assertEqual(usage.feature_for('/api/customers/<str:cust_id>/domains/hfcb/'), 'core_banking')
        self.assertEqual(usage.feature_for('/api/customers/<str:cust_id>/domains/<str:domain>/'), 'domains')
        self.assertIsNone(usage.feature_for('/api/meta/'))


class PushCommandTests(TestCase):
    def test_dry_run_reads_the_trail(self):
        AuditEvent.objects.create(username='rm.one', kind='api',
                                  route='/api/customers/<str:cust_id>/', status=200, target='5')
        out = StringIO()
        call_command('push_usage', '--dry-run', stdout=out)
        self.assertIn('1 user-day rows, 1 users', out.getvalue())

    def test_refuses_to_send_without_settings(self):
        with self.assertRaises(CommandError):
            call_command('push_usage', '--url', '', '--token', '', stdout=StringIO())
