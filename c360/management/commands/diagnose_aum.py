"""Explain an 'AUM looks wrong' complaint by putting the two sources side by side.

Customer 360 shows TWO different money figures that people conflate:
  * Relationship value / Net position  -> LIVE core-banking balances (Trino
    eom_deposits + eom_loans, at the current snapshot date).
  * AUM / Profitability / NPL          -> the reporting Postgres
    ``customer_allocation_base`` -- a periodic, CSV-uploaded management snapshot
    with NO date column, so its vintage can't be read from the data.

They come from different feeds with different vintages and (possibly) different
definitions, so they are NOT expected to match to the shilling. This command makes
the gap visible so you can tell WHICH kind of 'wrong' you have:

  * AUM ~= deposits + loans in the base, but both differ a lot from LIVE
      -> the snapshot is STALE; re-upload the allocation-base CSV in the portfolio.
  * AUM is wildly different from deposits + loans in the SAME base row
      -> AUM is a different measure (or a scale/units problem in the CSV).

Read-only; safe on production.

    python manage.py diagnose_aum 39002        # one customer, snapshot vs live
    python manage.py diagnose_aum              # whole-book totals only
"""
from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from c360.warehouse.connector import PostgresDBAPIConnector
from c360.warehouse.factory import get_gateway


def _kes(n) -> str:
    try:
        return f'KES {float(n):,.0f}'
    except (TypeError, ValueError):
        return str(n)


class Command(BaseCommand):
    help = 'Compare the allocation-base AUM snapshot against the live deposit/loan balances.'

    def add_arguments(self, parser):
        parser.add_argument('cust_id', nargs='?', default=None,
                            help='Optional customer id to compare snapshot AUM vs live balances.')

    def handle(self, *args, **options):
        w = self.stdout.write
        pg = settings.C360.get('postgres_config') or {}
        if not pg.get('host'):
            w(self.style.ERROR('PG_HOST empty -- set PG_* in /etc/hf/c360.env and recreate the container first.'))
            return
        conn = PostgresDBAPIConnector(pg)
        try:
            conn.execute('SELECT 1')
        except Exception as e:
            w(self.style.ERROR(f'CONNECT FAILED -> {type(e).__name__}: {e}'))
            return

        # --- book-level: does AUM track deposits+loans, or is it a different measure? ---
        w(self.style.MIGRATE_HEADING('\nWhole-book totals (customer_allocation_base)'))
        try:
            row = conn.execute(
                """SELECT COUNT(*) AS n,
                          COALESCE(SUM(aum_cust_id), 0)                        AS aum,
                          COALESCE(SUM(COALESCE(deposit, 0)), 0)               AS dep,
                          COALESCE(SUM(COALESCE(loans, 0)), 0)                 AS loan,
                          COUNT(deposit)                                       AS dep_filled,
                          COUNT(loans)                                         AS loan_filled
                   FROM customer_allocation_base""")[0]
        except Exception as e:
            w(self.style.ERROR(f'  read failed -> {type(e).__name__}: {str(e)[:120]}'))
            return
        aum, dep, loan = float(row['aum']), float(row['dep']), float(row['loan'])
        dep_loan = dep + loan
        w(f'  customers          : {int(row["n"]):,}')
        w(f'  SUM(aum_cust_id)   : {_kes(aum)}')
        w(f'  SUM(deposit)       : {_kes(dep)}   ({int(row["dep_filled"]):,} rows filled)')
        w(f'  SUM(loans)         : {_kes(loan)}   ({int(row["loan_filled"]):,} rows filled)')
        w(f'  deposit + loans    : {_kes(dep_loan)}')
        if dep_loan:
            w(f'  AUM / (dep+loans)  : {aum / dep_loan:.2f}x   '
              f'(near 1.00 => AUM ~ balances; far from 1 => different measure or scale)')

        cid = options['cust_id']
        if not cid:
            w('\nPass a customer id to compare that customer\'s snapshot AUM against the LIVE balance,')
            w('e.g.  python manage.py diagnose_aum 39002')
            return

        # --- one customer: the allocation-base snapshot row ---
        w(self.style.MIGRATE_HEADING(f'\nCustomer {cid} -- allocation-base snapshot'))
        try:
            rows = conn.execute(
                """SELECT customer_name, aum_cust_id, deposit, loans, main_segment, rm_name, source
                   FROM customer_allocation_base
                   WHERE TRIM(CAST(cust_id AS text)) = %s LIMIT 1""", (str(cid).strip(),))
        except Exception as e:
            w(self.style.ERROR(f'  read failed -> {type(e).__name__}: {str(e)[:120]}'))
            rows = []
        if not rows:
            w('  (not in customer_allocation_base -- C360 would badge AUM "not sourced" for this one)')
        else:
            r = rows[0]
            snap_aum = r.get('aum_cust_id')
            snap_dl = float(r.get('deposit') or 0) + float(r.get('loans') or 0)
            w(f'  name               : {r.get("customer_name")}')
            w(f'  rm_name / segment  : {r.get("rm_name")} / {r.get("main_segment")}')
            w(f'  aum_cust_id (AUM)  : {_kes(snap_aum)}')
            w(f'  deposit            : {_kes(r.get("deposit"))}')
            w(f'  loans              : {_kes(r.get("loans"))}')
            w(f'  deposit + loans    : {_kes(snap_dl)}')
            w(f'  source column      : {r.get("source")}')

        # --- one customer: the LIVE core-banking balance C360 shows at the top ---
        w(self.style.MIGRATE_HEADING(f'\nCustomer {cid} -- LIVE balances (what "Relationship value" shows)'))
        try:
            live = get_gateway().get_relationship_value(str(cid))
            w(f'  live deposits      : {_kes(live.get("deposits"))}')
            w(f'  live loans         : {_kes(live.get("loans"))}')
            w(f'  relationship value : {_kes(live.get("relationship_value"))}')
        except Exception as e:
            w(self.style.WARNING(f'  live lookup unavailable ({type(e).__name__}: {str(e)[:80]})'))

        w(self.style.MIGRATE_HEADING('\nRead it like this'))
        w('  AUM ~ live relationship value  -> figures agree; the complaint is a misread.')
        w('  AUM far from live value        -> the allocation-base CSV is STALE; re-upload it')
        w('                                    in the portfolio (Customer Allocation Base upload).')
        w('  AUM far from its OWN dep+loans  -> AUM is a different measure or a units/scale issue.')
