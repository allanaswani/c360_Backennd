"""Diagnose why the CURRENT RM (retail_allocated_portfolio) isn't showing.

The live code path deliberately swallows every allocation error and falls back to the
account-opening officer, so a misconfiguration is invisible in the app. This command
runs the SAME path with the guards off and prints exactly what happens, step by step.

    python manage.py diagnose_rm            # uses a sampled customer from the table
    python manage.py diagnose_rm 39002      # test a specific CBS customer id

Read-only. Safe to run on production.
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from c360.warehouse.connector import PostgresDBAPIConnector
from c360.warehouse.factory import get_gateway


class Command(BaseCommand):
    help = 'Diagnose the current-RM (retail_allocated_portfolio) lookup end-to-end.'

    def add_arguments(self, parser):
        parser.add_argument('cust_id', nargs='?', default=None)

    def _line(self, label, value):
        self.stdout.write(f'  {label:<28} {value}')

    def handle(self, *args, **options):
        w = self.stdout.write
        w(self.style.MIGRATE_HEADING('1. Configuration'))
        self._line('DATA_MODE', settings.C360.get('DATA_MODE'))
        pg = settings.C360.get('postgres_config') or {}
        self._line('PG_HOST', pg.get('host') or '(empty)')
        self._line('PG_PORT', pg.get('port'))
        self._line('PG_DBNAME', pg.get('dbname') or '(empty)')
        self._line('PG_USER', pg.get('user') or '(empty)')
        self._line('PG_PASSWORD set?', 'yes' if pg.get('password') else 'NO')
        if not pg.get('host'):
            w(self.style.ERROR('\nPG_HOST is empty -> no allocation source. Set PG_* in the '
                               'env file and RECREATE the container (docker rm + run, not restart).'))
            return

        w(self.style.MIGRATE_HEADING('\n2. Raw Postgres connection'))
        conn = PostgresDBAPIConnector(pg)
        try:
            r = conn.execute('SELECT current_database() AS db, version() AS v')
            self._line('connected to', r[0]['db'])
            self._line('server', str(r[0]['v'])[:60])
        except Exception as e:
            w(self.style.ERROR(f'  CONNECT FAILED -> {type(e).__name__}: {e}'))
            w(self.style.WARNING('  (import error = psycopg2 not installed -> rebuild the image; '
                                 'timeout = server can\'t reach 128.2.1.25; auth/permission = creds '
                                 'or pg_hba on the DB.)'))
            return

        w(self.style.MIGRATE_HEADING('\n3. Locate the allocation table'))
        try:
            tabs = conn.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_name ILIKE %s ORDER BY table_name", ('%allocat%',))
            self._line('tables matching %allocat%', [f"{t['table_schema']}.{t['table_name']}" for t in tabs] or 'NONE')
        except Exception as e:
            w(self.style.ERROR(f'  table lookup failed -> {type(e).__name__}: {e}'))
            return

        w(self.style.MIGRATE_HEADING('\n4. What the gateway auto-detected'))
        gw = get_gateway()
        meta = None
        if hasattr(gw, '_probe_alloc_schema'):
            try:
                meta = gw._probe_alloc_schema()
            except Exception as e:
                w(self.style.ERROR(f'  probe raised -> {type(e).__name__}: {e}'))
        self._line('resolved schema', meta or 'None (auto-detect found nothing usable)')
        if not meta:
            w(self.style.WARNING('  -> set PG_ALLOC_TABLE / PG_ALLOC_CUST_COL / PG_ALLOC_NAME_COL '
                                 'explicitly if the table/columns are named differently.'))
            return

        table, cust, name, code = meta['table'], meta['cust_col'], meta['name_col'], meta['code_col']

        w(self.style.MIGRATE_HEADING('\n5. Table contents'))
        try:
            counts = conn.execute(
                f"SELECT COUNT(*) AS rows, COUNT(DISTINCT {cust}) AS custs, "
                f"COUNT({name}) AS with_name FROM {table}")[0]
            self._line('total rows', f"{counts['rows']:,}")
            self._line('distinct customers', f"{counts['custs']:,}")
            self._line(f'rows with {name}', f"{counts['with_name']:,}")
            sample = conn.execute(
                f"SELECT {cust} AS cid, {name} AS rm, {code} AS code FROM {table} "
                f"WHERE {name} IS NOT NULL LIMIT 5")
            w('  sample rows (cust_id / rm_name / sales_code):')
            for s in sample:
                w(f"    {str(s['cid']).strip():<16} {str(s['rm']).strip():<28} {s['code']}")
        except Exception as e:
            w(self.style.ERROR(f'  content query failed -> {type(e).__name__}: {e}'))
            return

        w(self.style.MIGRATE_HEADING('\n6. End-to-end lookup for one customer'))
        cid = options['cust_id']
        if not cid and sample:
            cid = str(sample[0]['cid']).strip()
            self._line('(no id given, using sample)', cid)
        self._line('customer id tested', cid)
        # (a) the exact live path
        got = {}
        try:
            got = gw.get_current_rm([cid])
        except Exception as e:
            w(self.style.ERROR(f'  get_current_rm raised -> {type(e).__name__}: {e}'))
        self._line('get_current_rm() result', got.get(str(cid)) or 'EMPTY -> would fall back to onboarding officer')
        # (b) does that id exist in the table at all (id-format check)?
        try:
            raw = conn.execute(
                f"SELECT {cust} AS cid, {name} AS rm FROM {table} "
                f"WHERE TRIM(CAST({cust} AS text)) = %s", (str(cid),))
            self._line('raw match on id', raw or 'NO ROW with that exact id text')
            if not raw:
                near = conn.execute(
                    f"SELECT {cust} AS cid FROM {table} "
                    f"WHERE CAST({cust} AS text) LIKE %s LIMIT 3", (f"%{cid}%",))
                self._line('ids containing that number', [str(n['cid']).strip() for n in near] or 'none')
                w(self.style.WARNING('  -> if the stored id looks different (padding/decimals/prefix), '
                                     'that is the mismatch; tell me the format and I fix the join.'))
        except Exception as e:
            w(self.style.ERROR(f'  raw id check failed -> {type(e).__name__}: {e}'))

        w(self.style.SUCCESS('\nDone. Paste this whole output back.'))
