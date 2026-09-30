"""Push daily per-user Customer 360 usage to the portfolio tool.

    python manage.py push_usage              # yesterday + today (the cron job)
    python manage.py push_usage --days 90    # backfill everything the audit trail holds
    python manage.py push_usage --dry-run    # show what would be sent, send nothing

Posts ``{"usage_days": [...]}`` to the portfolio backend's ``observability/ingest/``,
authenticated with the service token issued for "Customer 360" in the portfolio's
Administration > Other systems screen. Rows are keyed on (day, username) on the
receiving side, so re-sending a day replaces it - running this every hour is safe and
keeps "today" current. See c360/usage.py.

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

from ... import usage

BATCH = 500          # the ingest endpoint's per-request cap


class Command(BaseCommand):
    help = 'Push daily per-user Customer 360 usage to the portfolio tool.'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=2,
                            help='How many Nairobi days to send, ending today (default 2).')
        parser.add_argument('--dry-run', action='store_true', help='Build the rows, send nothing.')
        parser.add_argument('--url', default=os.environ.get('C360_PORTFOLIO_INGEST_URL', ''))
        parser.add_argument('--token', default=os.environ.get('C360_PORTFOLIO_INGEST_TOKEN', ''))

    def handle(self, *args, **opts):
        days = max(1, min(int(opts['days']), 400))
        today = usage.local_day(timezone.now())
        first = today - timedelta(days=days - 1)
        rows = usage.rollup_days(first, today)
        users = len({r['username'] for r in rows})
        self.stdout.write(f'{len(rows)} user-day rows, {users} users, {first} to {today} (Nairobi).')

        if opts['dry_run']:
            for r in rows[-10:]:
                self.stdout.write(f"  {r['day']} {r['username']}: {r['customer_views']} customers opened, "
                                  f"{r['searches']} searches, {r['active_minutes']} active min")
            return
        if not opts['url'] or not opts['token']:
            raise CommandError('C360_PORTFOLIO_INGEST_URL and C360_PORTFOLIO_INGEST_TOKEN must be set '
                               '(or pass --url / --token). Nothing was sent.')

        stored = 0
        for i in range(0, len(rows), BATCH):
            body = json.dumps({'usage_days': rows[i:i + BATCH]}).encode('utf-8')
            req = urllib.request.Request(
                opts['url'], data=body, method='POST',
                headers={'Content-Type': 'application/json',
                         'X-Observability-Token': opts['token']})
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    out = json.loads(resp.read().decode('utf-8') or '{}')
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode('utf-8', 'replace')[:300]
                raise CommandError(f'Portfolio ingest refused the batch ({exc.code}): {detail}')
            except urllib.error.URLError as exc:
                raise CommandError(f'Could not reach the portfolio ingest at {opts["url"]}: {exc.reason}')
            stored += int(out.get('usage_days_stored') or 0)
        self.stdout.write(self.style.SUCCESS(f'Sent {len(rows)} rows; the portfolio stored {stored}.'))
