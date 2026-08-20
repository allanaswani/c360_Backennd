"""Probe the reporting Postgres for the richer per-customer data we could wire.

Tells us which of the portfolio's enrichment / profitability tables exist and how
populated they are, so we only build UI for fields that are actually filled in (and
badge the rest honestly as 'not sourced'). Read-only; safe on production.

    python manage.py diagnose_enrichment 134909
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from c360.warehouse.connector import PostgresDBAPIConnector

# table -> columns we care about (existence + non-null coverage + the sample row)
_TARGETS = {
    'retail_allocated_portfolio': ('cust_id', ['total_revenue', 'total_depost_balance', 'total_loans']),
    'customer_allocation_base': ('cust_id', ['aum_cust_id', 'net_after_expense', 'interest_income',
                                             'nfi', 'interest_expense', 'loan_loss', 'npl', 'main_segment']),
    'portfolio_customer_enrichment': ('cust_id', ['credit_score', 'risk_rating', 'lifetime_value_estimate',
                                                  'income_band', 'employment_status', 'industry',
                                                  'monthly_income_estimate', 'property_owner', 'preferred_channel']),
    'revenue': ('cust_id', ['income_category', 'sum_dc']),
}


class Command(BaseCommand):
    help = 'Probe reporting-Postgres enrichment/profitability tables for coverage.'

    def add_arguments(self, parser):
        parser.add_argument('cust_id', nargs='?', default=None)

    def handle(self, *args, **options):
        w = self.stdout.write
        pg = settings.C360.get('postgres_config') or {}
        if not pg.get('host'):
            w(self.style.ERROR('PG_HOST empty — set PG_* and recreate the container first.'))
            return
        conn = PostgresDBAPIConnector(pg)
        try:
            conn.execute('SELECT 1')
        except Exception as e:
            w(self.style.ERROR(f'CONNECT FAILED -> {type(e).__name__}: {e}'))
            return
        cid = options['cust_id']

        for table, (keycol, cols) in _TARGETS.items():
            w(self.style.MIGRATE_HEADING(f'\n{table}'))
            try:
                total = conn.execute(f'SELECT COUNT(*) n FROM {table}')[0]['n']
            except Exception as e:
                w(f'  (table not present / unreadable: {type(e).__name__}: {str(e)[:80]})')
                continue
            w(f'  total rows: {total:,}')
            # non-null coverage per interesting column
            try:
                sel = ', '.join(f'COUNT({c}) AS "{c}"' for c in cols)
                cov = conn.execute(f'SELECT {sel} FROM {table}')[0]
                for c in cols:
                    n = cov.get(c) or 0
                    pct = (100.0 * n / total) if total else 0
                    flag = 'OK' if pct >= 5 else 'sparse/empty'
                    w(f'    {c:<24} {n:>10,} populated ({pct:4.0f}%)  {flag}')
            except Exception as e:
                w(f'  coverage query failed: {type(e).__name__}: {str(e)[:80]}')
            # sample row for the given customer
            if cid:
                try:
                    row = conn.execute(
                        f'SELECT * FROM {table} WHERE TRIM(CAST({keycol} AS text)) = %s LIMIT 1', (str(cid),))
                    if row:
                        shown = {k: row[0][k] for k in cols if k in row[0]}
                        w(f'    cust {cid}: {shown}')
                    else:
                        w(f'    cust {cid}: no row')
                except Exception as e:
                    w(f'    cust {cid} lookup failed: {type(e).__name__}: {str(e)[:80]}')

        w(self.style.SUCCESS('\nDone. Paste this back — it tells us what we can light up.'))
