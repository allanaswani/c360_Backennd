"""Is 'Account opened 01 Jan 2016' a real date or a core-migration placeholder?

Customer 360 reads the account-opening date straight from
``delta.gold_db.dim_customer.account_opening_date`` (falling back to
``cust_open_date``) -- it does NOT invent it. But if a huge share of customers all
carry ONE date, that date is almost certainly a migration default: when accounts
were loaded into the current core system, older accounts that didn't carry their
true opening date were bulk-stamped with the migration go-live date. (The sibling
marker is ``fk_bankemployeeid = MIG_CIS`` -- a 'migrated from CIS' placeholder.)

This command prints the most common opening dates so you can SEE whether one date
dominates, instead of guessing. Read-only; safe on production.

    python manage.py diagnose_open_dates
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from c360.warehouse.connector import TrinoDBAPIConnector


class Command(BaseCommand):
    help = 'Show the distribution of dim_customer.account_opening_date (spot migration-default dates).'

    def handle(self, *args, **options):
        w = self.stdout.write
        cfg = settings.C360.get('trino_config') or {}
        if not cfg.get('host'):
            w(self.style.ERROR('No TRINO_HOST configured -- this only runs against the live warehouse.'))
            return
        conn = TrinoDBAPIConnector(cfg)
        try:
            total = conn.execute('SELECT COUNT(*) AS n FROM delta.gold_db.dim_customer')[0]['n']
        except Exception as e:
            w(self.style.ERROR(f'CONNECT/READ FAILED -> {type(e).__name__}: {str(e)[:120]}'))
            return
        total = int(total or 0)
        w(self.style.MIGRATE_HEADING('\ndim_customer.account_opening_date'))
        w(f'  total customers: {total:,}')
        if not total:
            return

        try:
            nulls = conn.execute(
                'SELECT COUNT(*) AS n FROM delta.gold_db.dim_customer '
                'WHERE account_opening_date IS NULL')[0]['n']
            w(f'  null / missing : {int(nulls or 0):,}')
        except Exception:
            pass

        try:
            rows = conn.execute(
                """SELECT CAST(account_opening_date AS varchar) AS d, COUNT(*) AS n
                   FROM delta.gold_db.dim_customer
                   GROUP BY CAST(account_opening_date AS varchar)
                   ORDER BY n DESC LIMIT 12""")
        except Exception as e:
            w(self.style.ERROR(f'  distribution read failed -> {type(e).__name__}: {str(e)[:120]}'))
            return

        w(self.style.MIGRATE_HEADING('\nMost common opening dates (a big single-date cluster = migration default)'))
        for r in rows:
            n = int(r.get('n') or 0)
            share = 100.0 * n / total
            flag = '  <-- dominant; likely a migration/placeholder date' if share >= 10 else ''
            w(f'  {str(r.get("d")):>12}   {n:>8,}   {share:5.1f}%{flag}')

        top = rows[0] if rows else None
        if top and (100.0 * int(top.get('n') or 0) / total) >= 10:
            w(self.style.WARNING(
                f'\n  {int(top["n"]):,} customers ({100.0*int(top["n"])/total:.1f}%) share {top["d"]}. '
                'That is not a real opening date for all of them -- it is a bulk migration stamp. '
                'Treat it as "on/before migration", not a precise open date.'))
        else:
            w('\n  No single date dominates -- the opening dates look like genuine per-customer values.')
