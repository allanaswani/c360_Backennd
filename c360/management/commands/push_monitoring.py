"""Push Customer 360's health, change audit and usage to the portfolio tool.

    python manage.py push_monitoring              # the hourly cron job
    python manage.py push_monitoring --dry-run    # show what would be sent, send nothing
    python manage.py push_monitoring --audit-hours 72   # re-send a longer audit window

The portfolio is where monitoring, audit and alerts live. It already accepts all of
this on ``observability/ingest/`` (``tables``, ``audit_events``, ``usage_days``) and
shows it on its Data health and Audit trail pages and in its alert mails, labelled
"Customer 360". Until 2026-10-01 only usage was sent, so the portfolio had never seen
a Customer 360 table check or change.

* tables: every row of the Customer 360 health check - warehouse tables, the date
  each source is current to, the reporting Postgres, and the deployment checks
  (build, LAN proxy hosts, sign-in key). Re-sending replaces each row.
* audit_events: who changed which account, role, book or recommendation outcome,
  with before/after values; and who OPENED which customer or statement or exported
  a report (the access trail a bank audit asks for). Each carries its own id, so
  re-sending is ignored. Sign-ins are not sent: the request log cannot name the
  person at that moment, and an "anonymous signed in" line would audit nothing.
* usage_days: as push_usage (yesterday and today).

Each part is sent on its own, so a failure in one (say, the warehouse is down and
the health check cannot run) does not stop the others - and the deployment rows,
which need no warehouse, are still reported.

Settings (env): C360_PORTFOLIO_INGEST_URL, C360_PORTFOLIO_INGEST_TOKEN.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from ... import changes as change_audit
from ... import deployment, usage
from ...warehouse.factory import get_gateway

BATCH = 500                     # the ingest endpoint's per-request cap

#: Customer 360 check status -> the portfolio's table-health vocabulary.
STATUS_MAP = {'ok': 'ok', 'warn': 'warning', 'empty': 'empty', 'stale': 'stale',
              'error': 'error', 'unknown': 'unknown'}


def table_rows(checks: list[dict]) -> list[dict]:
    """Health-check rows in the portfolio's ``tables`` shape."""
    out = []
    for c in checks:
        key = c.get('key')
        if not key:
            continue
        status = STATUS_MAP.get(str(c.get('status') or 'unknown'), 'unknown')
        value = c.get('value')
        # Only a row count is a row count: a Freshness value is days behind, a Service
        # value an error count and a Deployment value a flag - none is "N rows".
        counts = (c.get('group') not in ('Freshness', 'Deployment', 'Service')
                  and isinstance(value, (int, float)))
        out.append({
            'table': str(key),
            'label': f"{c.get('label') or key} ({c.get('group') or 'Customer 360'})",
            'status': status,
            'rows': int(value) if counts else 0,
            'rows_are_estimate': False,
            'last_seen': f"{c['as_of']}T00:00:00+03:00" if c.get('as_of') else None,
            'age_days': c.get('age_days'),
            'error': '' if status == 'ok' else str(c.get('detail') or '')[:300],
        })
    return out


def audit_events(hours: int) -> list[dict]:
    """The change audit for the last ``hours``, in the portfolio's event shape."""
    since = timezone.now() - timedelta(hours=hours)
    out = []
    for e in change_audit.feed(since=since, limit=BATCH, with_changes=True):
        if e.get('no_op'):
            continue
        out.append({
            'id': f"c360:{e['id']}",
            'action': e.get('action') or 'updated',
            'model_label': e.get('model_label') or '',
            'object_id': str(e.get('object_id') or ''),
            'object_label': e.get('object_label') or '',
            'username': e.get('username') or 'system',
            'reason': e.get('reason') or '',
            'occurred_at': e.get('when'),
            'changes': [{'field': ch.get('label') or ch.get('field'), 'old': ch.get('old'), 'new': ch.get('new')}
                        for ch in e.get('changes') or []],
        })
    return out


#: Request-log routes that are an access worth auditing -> (action, record type).
#: Both spellings: rows before 9 Sep 2026 carry ':cust_id', later ones Django's own
#: '<str:cust_id>'; one request writes one row, so nothing is counted twice.
ACCESS_ROUTES = {
    '/api/customers/<str:cust_id>/': ('viewed', 'Customer'),
    '/api/customers/:cust_id/': ('viewed', 'Customer'),
    '/api/customers/<str:cust_id>/statement/': ('viewed', 'Customer statement'),
    '/api/customers/:cust_id/statement/': ('viewed', 'Customer statement'),
    '/api/observability/export/': ('exported', 'Report'),
}


def access_events(hours: int) -> list[dict]:
    """Who opened which customer or statement, or exported a report."""
    from ...models import AuditEvent

    since = timezone.now() - timedelta(hours=hours)
    qs = (AuditEvent.objects.filter(kind=AuditEvent.KIND_API, ts__gte=since,
                                    route__in=list(ACCESS_ROUTES))
          .order_by('-ts')[:2000])
    out = []
    for e in qs:
        # Only successful reads by a known person: a refused request opened nothing,
        # and a row with no username cannot say who did it.
        if e.status is None or not 200 <= e.status < 300 or not e.username:
            continue
        action, label = ACCESS_ROUTES[e.route]
        obj = e.target or ''
        obj_label = e.path if label == 'Report' else (f'Customer {obj}' if obj else '')
        out.append({
            'id': f'c360:access:{e.pk}',
            'action': action,
            'model_label': label,
            'object_id': str(obj)[:100],
            'object_label': obj_label[:200],
            'username': e.username,
            'reason': '',
            'occurred_at': e.ts.isoformat(),
            'changes': [],
        })
    return out


class Command(BaseCommand):
    help = "Push Customer 360 health, change audit and usage to the portfolio tool."

    def add_arguments(self, parser):
        parser.add_argument('--audit-hours', type=int, default=3,
                            help='How far back to send changes (default 3; re-sends are ignored).')
        parser.add_argument('--usage-days', type=int, default=2)
        parser.add_argument('--dry-run', action='store_true', help='Build everything, send nothing.')
        parser.add_argument('--url', default=os.environ.get('C360_PORTFOLIO_INGEST_URL', ''))
        parser.add_argument('--token', default=os.environ.get('C360_PORTFOLIO_INGEST_TOKEN', ''))

    def handle(self, *args, **opts):
        if not opts['dry_run'] and (not opts['url'] or not opts['token']):
            raise CommandError('C360_PORTFOLIO_INGEST_URL and C360_PORTFOLIO_INGEST_TOKEN must be set '
                               '(or pass --url / --token). Nothing was sent.')
        failures = []

        # 1. Health. The deployment rows need no warehouse, so they go even if the
        #    warehouse check fails.
        checks = deployment.checks()
        try:
            checks += list(get_gateway().health_report().get('checks') or [])
        except Exception as exc:                                  # noqa: BLE001
            checks.append({'key': 'warehouse_conn', 'label': 'Warehouse connection', 'group': 'System',
                           'status': 'error', 'detail': f'health check could not run: {type(exc).__name__}: {exc}'[:300]})
        tables = table_rows(checks)
        bad = [t for t in tables if t['status'] not in ('ok', 'warning')]
        self.stdout.write(f'health: {len(tables)} checks, {len(bad)} not ok'
                          + (': ' + ', '.join(f"{t['table']}={t['status']}" for t in bad) if bad else ''))

        # 2. Change audit and access trail.
        hours = max(1, int(opts['audit_hours']))
        events = audit_events(hours)
        access = access_events(hours)
        self.stdout.write(f"audit: {len(events)} change(s), {len(access)} access event(s) in the last {hours}h")
        events = events + access

        # 3. Usage.
        today = usage.local_day(timezone.now())
        days = max(1, min(int(opts['usage_days']), 400))
        rows = usage.rollup_days(today - timedelta(days=days - 1), today)
        self.stdout.write(f'usage: {len(rows)} user-day rows')

        if opts['dry_run']:
            for t in tables[:60]:
                self.stdout.write(f"  {t['status']:8} {t['table']:22} {t['label']}"
                                  + (f"  - {t['error'][:90]}" if t['error'] else ''))
            return

        for name, key, items in (('health', 'tables', tables), ('audit', 'audit_events', events),
                                 ('usage', 'usage_days', rows)):
            for i in range(0, len(items), BATCH):
                try:
                    out = self._post(opts['url'], opts['token'], {key: items[i:i + BATCH]})
                except CommandError as exc:
                    failures.append(f'{name}: {exc}')
                    break
                self.stdout.write(f'  {name}: sent {len(items[i:i + BATCH])}, portfolio replied {out}')
        if failures:
            raise CommandError('Some parts were not delivered: ' + ' | '.join(failures))
        self.stdout.write(self.style.SUCCESS('Pushed health, audit and usage to the portfolio.'))

    @staticmethod
    def _post(url: str, token: str, payload: dict) -> dict:
        body = json.dumps(payload, default=str).encode('utf-8')
        req = urllib.request.Request(url, data=body, method='POST',
                                     headers={'Content-Type': 'application/json',
                                              'X-Observability-Token': token})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode('utf-8') or '{}')
        except urllib.error.HTTPError as exc:
            raise CommandError(f'refused ({exc.code}): {exc.read().decode("utf-8", "replace")[:200]}')
        except urllib.error.URLError as exc:
            raise CommandError(f'could not reach {url}: {exc.reason}')
