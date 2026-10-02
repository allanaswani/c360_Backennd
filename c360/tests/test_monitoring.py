"""Deployment checks and the push of Customer 360's health to the portfolio.

Every deployment check here is something that broke the app on 2026-10-01 without
showing on the health page: an old backend still running, the LAN proxy hosts
missing from DJANGO_ALLOWED_HOSTS (a login loop for every LAN user), and the
settings the portfolio sign-in and monitoring feed need.
"""
import os
from unittest import mock

from django.test import SimpleTestCase, override_settings

from c360 import deployment
from c360.api.views import _annotate_drops
from c360.management.commands.push_monitoring import table_rows


def _check(rows, key):
    return next(r for r in rows if r['key'] == key)


class DeploymentCheckTests(SimpleTestCase):
    @override_settings(ALLOWED_HOSTS=['ceo.hfcb.co.ke', '128.2.1.25', 'localhost', '127.0.0.1'])
    def test_missing_proxy_hosts_is_an_error_that_names_them(self):
        row = _check(deployment.checks(), 'lan_proxy_hosts')
        self.assertEqual(row['status'], 'error')
        self.assertIn('172.17.0.1, host.docker.internal', row['detail'])
        self.assertIn('recreate', row['detail'])

    @override_settings(ALLOWED_HOSTS=['ceo.hfcb.co.ke', '127.0.0.1', '172.17.0.1', 'host.docker.internal'])
    def test_both_proxy_hosts_present_is_ok(self):
        self.assertEqual(_check(deployment.checks(), 'lan_proxy_hosts')['status'], 'ok')

    def test_build_commit_is_reported_when_stamped(self):
        with mock.patch.dict(os.environ, {'C360_BUILD': '9f6c573'}):
            row = _check(deployment.checks(), 'build')
        self.assertEqual(row['status'], 'ok')
        self.assertIn('9f6c573', row['detail'])
        self.assertIsNone(row['value'], 'a text value would break the row-count trend')

    def test_an_unstamped_build_says_how_to_stamp_it(self):
        with mock.patch.dict(os.environ, {'C360_BUILD': 'unknown'}):
            row = _check(deployment.checks(), 'build')
        self.assertEqual(row['status'], 'unknown')
        self.assertIn('--build-arg C360_BUILD', row['detail'])

    def test_sign_in_key_and_feed(self):
        with mock.patch.dict(os.environ, {'C360_JWT_SIGNING_KEY': '', 'C360_PORTFOLIO_INGEST_URL': '',
                                          'C360_PORTFOLIO_INGEST_TOKEN': ''}):
            rows = deployment.checks()
        self.assertEqual(_check(rows, 'portfolio_key')['status'], 'error')
        self.assertEqual(_check(rows, 'portfolio_feed')['status'], 'empty')
        with mock.patch.dict(os.environ, {'C360_JWT_SIGNING_KEY': 'k', 'C360_PORTFOLIO_INGEST_URL': 'http://x/',
                                          'C360_PORTFOLIO_INGEST_TOKEN': 't'}):
            rows = deployment.checks()
        self.assertEqual(_check(rows, 'portfolio_key')['status'], 'ok')
        self.assertEqual(_check(rows, 'portfolio_feed')['status'], 'ok')


class PushShapeTests(SimpleTestCase):
    def test_rows_map_to_the_portfolio_table_shape(self):
        rows = table_rows([
            {'key': 'hfdi_payments', 'label': 'Property payment register', 'group': 'Domains',
             'status': 'ok', 'value': 17395, 'detail': '17,395 rows'},
            {'key': 'fresh_ledger', 'label': 'Transactions', 'group': 'Freshness', 'status': 'stale',
             'value': 5, 'as_of': '2026-09-26', 'age_days': 5, 'detail': 'current to 2026-09-26'},
            {'key': 'lan_proxy_hosts', 'label': 'LAN access', 'group': 'Deployment', 'status': 'error',
             'value': 0, 'detail': 'missing 172.17.0.1'},
            {'key': 'x', 'label': 'X', 'group': 'Credit', 'status': 'warn', 'value': 10, 'detail': 'down 40%'},
        ])
        pay, fresh, lan, warn = rows
        self.assertEqual((pay['status'], pay['rows'], pay['error']), ('ok', 17395, ''))
        self.assertEqual(pay['label'], 'Property payment register (Domains)')
        self.assertEqual(fresh['rows'], 0, 'days behind is not a row count')
        self.assertEqual(fresh['age_days'], 5)
        self.assertTrue(fresh['last_seen'].startswith('2026-09-26'))
        self.assertEqual(fresh['status'], 'stale')
        self.assertEqual((lan['status'], lan['rows']), ('error', 0))
        self.assertIn('missing 172.17.0.1', lan['error'])
        self.assertEqual(warn['status'], 'warning', "the portfolio's word for it")


class DropDetectionTests(SimpleTestCase):
    def test_a_source_catching_up_is_not_a_drop(self):
        report = {'checks': [{'key': 'fresh_ledger', 'group': 'Freshness', 'status': 'ok', 'value': 1,
                              'detail': 'current to 2026-09-30'}]}
        _annotate_drops(report, {'fresh_ledger': 3})
        self.assertEqual(report['checks'][0]['status'], 'ok')
        self.assertNotIn('delta_pct', report['checks'][0])

    def test_a_row_count_falling_is_still_caught(self):
        report = {'checks': [{'key': 'hfdi_payments', 'group': 'Domains', 'status': 'ok', 'value': 5000,
                              'detail': '5,000 rows'}]}
        _annotate_drops(report, {'hfdi_payments': 17395})
        self.assertEqual(report['checks'][0]['status'], 'warn')


from datetime import timedelta as _td

from django.test import TestCase
from django.utils import timezone as _tz

from c360.management.commands.push_monitoring import access_events
from c360.models import AuditEvent


def _ev(route, status=200, username='jdoe', target='1218821', minutes_ago=5, kind='api', path=''):
    return AuditEvent.objects.create(ts=_tz.now() - _td(minutes=minutes_ago), username=username, kind=kind,
                                     method='GET', route=route, path=path or route, status=status, target=target)


class ServiceCheckTests(TestCase):
    def test_no_errors_is_ok(self):
        self.assertEqual(deployment.service_checks()[0]['status'], 'ok')

    def test_a_few_errors_need_attention_and_name_the_route(self):
        for _ in range(2):
            _ev('/api/customers/<str:cust_id>/insights/', status=500)
        row = deployment.service_checks()[0]
        self.assertEqual((row['status'], row['value']), ('warn', 2))
        self.assertIn('/api/customers/<str:cust_id>/insights/ (2)', row['detail'])

    def test_five_in_an_hour_is_an_error(self):
        for _ in range(5):
            _ev('/api/book/', status=502)
        self.assertEqual(deployment.service_checks()[0]['status'], 'error')

    def test_older_errors_do_not_count(self):
        _ev('/api/book/', status=500, minutes_ago=90)
        self.assertEqual(deployment.service_checks()[0]['status'], 'ok')


class AccessTrailTests(TestCase):
    def test_customer_and_statement_views_by_a_named_person(self):
        _ev('/api/customers/<str:cust_id>/')
        _ev('/api/customers/<str:cust_id>/statement/')
        events = access_events(3)
        self.assertEqual({(e['action'], e['model_label']) for e in events},
                         {('viewed', 'Customer'), ('viewed', 'Customer statement')})
        self.assertTrue(all(e['username'] == 'jdoe' and e['object_id'] == '1218821' for e in events))
        self.assertTrue(all(e['id'].startswith('c360:access:') for e in events))

    def test_refused_anonymous_and_other_requests_are_not_access(self):
        _ev('/api/customers/<str:cust_id>/', status=403)
        _ev('/api/customers/<str:cust_id>/', username='')
        _ev('/api/customers/<str:cust_id>/insights/')
        _ev('/api/auth/login/', target='')
        self.assertEqual(access_events(3), [])

    def test_both_route_spellings_count_once_each(self):
        _ev('/api/customers/<str:cust_id>/')
        _ev('/api/customers/:cust_id/')
        self.assertEqual(len(access_events(3)), 2)
