"""Read-only connectivity + schema smoke test for the live warehouse.

Verifies the Trino credentials work and the tables Customer 360 depends on are
reachable and shaped as expected — without writing anything. Run this from any LAN
host before flipping C360_DATA_MODE=live:

    python manage.py smoke_test_connections

It reports which probes pass, so wiring live queries starts from facts, not guesses.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from c360.warehouse.connector import TrinoDBAPIConnector

# (label, sql, expectation-note). All strictly read-only.
PROBES = [
    ('auth / SELECT 1', 'SELECT 1 AS ok', 'returns 1'),
    ('as-of date', 'SELECT prev_trx_date AS d FROM delta.gold_db.bank_parameters LIMIT 1', 'last closed business date'),
    ('dim_customer (identity master)', 'SELECT COUNT(*) AS n FROM delta.gold_db.dim_customer', 'clean 1-row-per-customer'),
    ('aml_customer.risk_class populated?',
     "SELECT COUNT(*) AS n FROM delta.gold_db.aml_customer WHERE risk_class IS NOT NULL AND trim(risk_class) <> ''",
     '0 = risk still unsourced -> eligibility gate stays pending'),
    ('eom_deposits reachable', 'SELECT COUNT(*) AS n FROM delta.gold_db.eom_deposits LIMIT 1', 'deposit snapshot'),
    ('eom_loans reachable', 'SELECT COUNT(*) AS n FROM delta.gold_db.eom_loans LIMIT 1', 'loan snapshot'),
    ('hfdi_client_data (properties)', 'SELECT COUNT(*) AS n FROM delta.gold_db.hfdi_client_data', 'HFDI clients'),
    ('customers_whizz (digital)', 'SELECT COUNT(*) AS n FROM delta.gold_db.customers_whizz', 'Whizz customers'),
]


class Command(BaseCommand):
    help = 'Read-only connectivity + schema smoke test against the live Trino warehouse.'

    def handle(self, *args, **options):
        cfg = settings.C360['trino_config']
        if not cfg.get('host'):
            self.stdout.write(self.style.ERROR('No TRINO_HOST configured (backend/.env). Nothing to test.'))
            return
        self.stdout.write(f"Trino: {cfg['user']}@{cfg['host']}:{cfg['port']} "
                          f"catalog={cfg.get('catalog')} verify={cfg.get('verify')}\n")
        conn = TrinoDBAPIConnector(cfg)
        ok = 0
        for label, sql, note in PROBES:
            try:
                rows = conn.execute(sql)
                val = next(iter(rows[0].values())) if rows else '(no rows)'
                self.stdout.write(self.style.SUCCESS(f'  PASS  {label:38} = {val}   [{note}]'))
                ok += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f'  FAIL  {label:38} {type(e).__name__}: {str(e)[:110]}'))
        self.stdout.write(f'\n{ok}/{len(PROBES)} probes passed.')
