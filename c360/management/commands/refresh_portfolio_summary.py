"""Warm the portfolio overview cache.

Stands in for the nightly Celery precompute described in architecture §6: in
production this task writes the materialised summary table; here it warms the
in-process cache for the common (whole-book) scope across every period preset, so
Level 1 loads instantly without aggregating live. Run on a schedule (cron/Celery
beat) once wired.

    python manage.py refresh_portfolio_summary
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from c360.services import portfolio_cache
from c360.services.portfolio import build_portfolio_overview
from c360.warehouse.factory import data_mode, get_gateway
from c360.warehouse.periods import resolve_period


class Command(BaseCommand):
    help = 'Precompute and cache the portfolio overview (nightly-refresh stand-in).'

    def handle(self, *args, **options):
        gateway = get_gateway()
        as_of = gateway.as_of_date()
        portfolio_cache.invalidate()
        count = 0
        for token in ('7D', '30D', 'QTD', 'YTD'):
            period = resolve_period(token, as_of=as_of)
            key = f'overview:ALL:{token}:{period.start}:{period.end}:{data_mode()}'
            portfolio_cache.warm(key, lambda p=period: build_portfolio_overview(gateway, None, p))
            count += 1
        self.stdout.write(self.style.SUCCESS(f'Warmed portfolio overview for {count} periods (whole book).'))
