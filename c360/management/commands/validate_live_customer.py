"""Validate the live TrinoWarehouse code path for one real customer.

Runs identity + value through the actual gateway + service code (not ad-hoc SQL),
and cross-checks the aggregated value against a standalone ground-truth sum to prove
there is no duplicate fan-out. Read-only.

    python manage.py validate_live_customer            # default sample customer
    python manage.py validate_live_customer 121669
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from c360.services.customer import build_customer_header
from c360.warehouse.connector import TrinoDBAPIConnector
from c360.warehouse.trino.trino_gateway import TrinoWarehouse


class Command(BaseCommand):
    help = 'Validate live identity + value for one customer (dedup-safe, read-only).'

    def add_arguments(self, parser):
        parser.add_argument('cust_id', nargs='?', default='121669')

    def handle(self, *args, **options):
        cid = options['cust_id']
        cfg = settings.C360['trino_config']
        if not cfg.get('host'):
            self.stdout.write(self.style.ERROR('No TRINO_HOST configured.'))
            return
        conn = TrinoDBAPIConnector(cfg)
        gw = TrinoWarehouse(conn)

        cust = gw.get_customer(cid)
        if not cust:
            self.stdout.write(self.style.ERROR(f'Customer {cid} not found in dim_customer.'))
            return
        val = gw.get_relationship_value(cid)
        deps = gw.get_deposit_accounts(cid)
        loans = gw.get_loan_accounts(cid)

        self.stdout.write(self.style.SUCCESS(f'\nIdentity (dim_customer):'))
        for k in ('cust_id', 'name', 'segment', 'branch', 'relationship_since', 'active'):
            self.stdout.write(f'  {k:20} {cust.get(k)}')
        self.stdout.write(self.style.SUCCESS('\nValue (pre-aggregated, dedup-safe):'))
        self.stdout.write(f'  deposits             {val["deposits"]:>16,}   ({len(deps)} accounts)')
        self.stdout.write(f'  loans                {val["loans"]:>16,}   ({len(loans)} accounts)')
        self.stdout.write(f'  relationship_value   {val["relationship_value"]:>16,}')

        # Cross-check: ground-truth standalone sums must equal the gateway values.
        d = f"DATE '{gw.as_of_date().isoformat()}'"
        gt_dep = conn.execute(f"SELECT COALESCE(SUM(book_balance),0) v, COUNT(*) c FROM delta.gold_db.eom_deposits WHERE eom_date={d} AND cust_id=?", (int(cid),))[0]
        gt_loan = conn.execute(f"SELECT COALESCE(SUM(gross_total),0) v, COUNT(*) c FROM delta.gold_db.eom_loans WHERE eom_date={d} AND cust_id=?", (int(cid),))[0]
        dep_ok = round(float(gt_dep['v'])) == val['deposits'] and gt_dep['c'] == len(deps)
        loan_ok = round(float(gt_loan['v'])) == val['loans'] and gt_loan['c'] == len(loans)
        self.stdout.write(self.style.SUCCESS('\nDedup cross-check vs standalone ground truth:'))
        self.stdout.write(f'  deposits match: {dep_ok}   loans match: {loan_ok}')

        # Prove the service layer builds a header from the same one row.
        header = build_customer_header(gw, cid)
        rs = header['risk']['relationship_since']
        self.stdout.write(self.style.SUCCESS('\nService header (Contract A):'))
        self.stdout.write(f'  name={header["identity"]["name"]["value"]!r} status={header["identity"]["name"]["status"]}')
        self.stdout.write(f'  relationship_since={rs["value"]!r} status={rs["status"]}  (risk_class status={header["risk"]["risk_class"]["status"]})')

        if dep_ok and loan_ok:
            self.stdout.write(self.style.SUCCESS('\nOK — live identity + value validated, no duplicate fan-out.'))
        else:
            self.stdout.write(self.style.ERROR('\nMISMATCH — investigate before trusting live numbers.'))
