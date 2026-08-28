"""Diagnose WHY specific customers are hidden as "staff" (RM sees 'Customer not found').

The staff sieve (rbac/staff.py) hides any customer that trips ANY of three signals —
employer text, customer_segment, or a real fk_bankemployeeid — from everyone but a
Django superuser. That is exactly the "admin can see them, the RM can't" symptom. This
command prints, for each id, the raw dim_customer fields the rule reads and which
signal(s) fire, so a false positive is visible instead of guessed at.

    python manage.py diagnose_staff 1103163 1109779 1171146 1117638 966351

Read-only (SELECTs on dim_customer only). Safe to run on production.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from c360.rbac.staff import (
    STAFF_EMPLOYER_PATTERNS,
    STAFF_SEGMENTS,
    is_real_employee_id,
    is_staff_from_fields,
)
from c360.warehouse.factory import data_mode, get_gateway


class Command(BaseCommand):
    help = 'Show which staff signal (if any) hides each given customer from non-admins.'

    def add_arguments(self, parser):
        parser.add_argument('cust_ids', nargs='*', help='CBS customer ids to check.')
        parser.add_argument('--audit', action='store_true',
                            help='Bank-wide: how many customers each staff signal hides.')

    def handle(self, *args, **options):
        w = self.stdout.write
        if data_mode() != 'live':
            raise CommandError(f'DATA_MODE is "{data_mode()}", not live — run this on the live warehouse.')

        gw = get_gateway()
        conn = getattr(gw, '_t', None)
        if conn is None:
            raise CommandError('Live gateway exposes no Trino connector (_t).')

        if options['audit']:
            self._audit(w, conn)
            return
        if not options['cust_ids']:
            raise CommandError('Give one or more customer ids, or pass --audit.')

        w(self.style.MIGRATE_HEADING('Staff-sieve rule in effect'))
        w(f'  employer patterns (substring): {STAFF_EMPLOYER_PATTERNS}')
        w(f'  staff segments (exact):        {STAFF_SEGMENTS}')
        w('  fk_bankemployeeid: ANY non-placeholder value counts as "is an employee".\n')

        for cid in options['cust_ids']:
            cid = str(cid).strip()
            w(self.style.MIGRATE_HEADING(f'Customer {cid}'))
            try:
                cid_num = int(cid.split('.')[0])   # customer_id is numeric (decimal) in the warehouse
            except (TypeError, ValueError):
                w(self.style.WARNING(f'  "{cid}" is not a numeric id — skipped.'))
                continue
            try:
                rows = conn.execute(
                    """
                    SELECT full_name, customer_segment, employer, fk_bankemployeeid, cust_type
                    FROM delta.gold_db.dim_customer
                    WHERE customer_id = ?
                    """,
                    (cid_num,),
                )
            except Exception as e:
                w(self.style.ERROR(f'  query failed -> {type(e).__name__}: {e}'))
                continue
            if not rows:
                w(self.style.WARNING('  no dim_customer row with that id (different reason than staff).'))
                continue

            r = rows[0]
            employer = r.get('employer')
            segment = r.get('customer_segment')
            empid = r.get('fk_bankemployeeid')
            self._kv(w, 'name', r.get('full_name'))
            self._kv(w, 'cust_type', r.get('cust_type'))
            self._kv(w, 'employer', employer)
            self._kv(w, 'customer_segment', segment)
            self._kv(w, 'fk_bankemployeeid', empid)

            # Which signal(s) trip — evaluated exactly as the sieve does.
            emp_up = (str(employer).strip().upper() if employer is not None else '')
            seg_up = (str(segment).strip().upper() if segment is not None else '')
            hits = []
            emp_match = [p for p in STAFF_EMPLOYER_PATTERNS if emp_up and p in emp_up]
            if emp_match:
                hits.append(f'employer contains {emp_match}')
            if seg_up and seg_up in STAFF_SEGMENTS:
                hits.append(f'segment == {seg_up!r}')
            if is_real_employee_id(empid):
                hits.append(f'fk_bankemployeeid {str(empid).strip()!r} treated as a real employee link')

            staff = is_staff_from_fields(employer=employer, segment=segment, bank_employee_id=empid)
            assert staff == bool(hits)  # the printed reasons must match the verdict
            if staff:
                w(self.style.ERROR('  -> HIDDEN as staff. Signal(s):'))
                for h in hits:
                    w(self.style.ERROR(f'       - {h}'))
            else:
                w(self.style.SUCCESS('  -> NOT hidden by the staff sieve — the 404 has another cause.'))
            w('')

        w(self.style.SUCCESS('Done. Paste this whole output back so we pick the right fix.'))

    def _kv(self, w, label, value):
        shown = '(null)' if value is None else (repr(value) if str(value).strip() == '' else str(value))
        w(f'  {label:<20} {shown}')

    # ------------------------------------------------------------------ audit
    def _audit(self, w, conn):
        """Bank-wide blast radius: how many real customers each staff signal hides,
        and what the fk_bankemployeeid values actually look like."""
        excl = "customer_segment <> 'INTERNAL ACCOUNTS'"

        def scalar(sql):
            r = conn.execute(sql)
            return int(list(r[0].values())[0]) if r else 0

        total = scalar(f"SELECT COUNT(*) FROM delta.gold_db.dim_customer WHERE {excl}")
        w(self.style.MIGRATE_HEADING('Bank-wide staff-sieve blast radius'))
        w(f'  real customers (excl. internal): {total:,}\n')

        # fk_bankemployeeid signal: any non-blank value that isn't a known placeholder.
        placeholders = "('MIG_CIS','MIGCIS','0','NULL','NONE','NA','N/A')"
        emp_hidden = scalar(
            f"SELECT COUNT(*) FROM delta.gold_db.dim_customer WHERE {excl} "
            f"AND fk_bankemployeeid IS NOT NULL AND TRIM(fk_bankemployeeid) <> '' "
            f"AND UPPER(TRIM(fk_bankemployeeid)) NOT IN {placeholders}")
        w(self.style.ERROR(f'  hidden by fk_bankemployeeid signal: {emp_hidden:,} '
                           f'({100.0 * emp_hidden / total:.1f}% of the book)'))

        # What do those values look like? Group by leading non-digit prefix.
        w('\n  top fk_bankemployeeid prefixes among the hidden set:')
        try:
            rows = conn.execute(
                f"SELECT REGEXP_EXTRACT(UPPER(TRIM(fk_bankemployeeid)), '^[^0-9]*') AS prefix, "
                f"COUNT(*) AS n FROM delta.gold_db.dim_customer WHERE {excl} "
                f"AND fk_bankemployeeid IS NOT NULL AND TRIM(fk_bankemployeeid) <> '' "
                f"AND UPPER(TRIM(fk_bankemployeeid)) NOT IN {placeholders} "
                f"GROUP BY REGEXP_EXTRACT(UPPER(TRIM(fk_bankemployeeid)), '^[^0-9]*') "
                f"ORDER BY n DESC LIMIT 15")
            for r in rows:
                w(f"      {str(r.get('prefix') or '(numeric)'):<12} {int(r.get('n') or 0):,}")
        except Exception as e:
            w(self.style.WARNING(f'      prefix breakdown failed -> {type(e).__name__}: {e}'))

        # employer-text signal (the genuine "works for HF" marker).
        ors = ' OR '.join(f"UPPER(employer) LIKE '%{p}%'" for p in STAFF_EMPLOYER_PATTERNS)
        emp_txt = scalar(
            f"SELECT COUNT(*) FROM delta.gold_db.dim_customer WHERE {excl} AND ({ors})")
        w(self.style.SUCCESS(f'\n  hidden by employer-text signal (real HF-staff marker): {emp_txt:,}'))

        # segment signal.
        segs = ','.join(f"'{s}'" for s in STAFF_SEGMENTS)
        seg_hidden = scalar(
            f"SELECT COUNT(*) FROM delta.gold_db.dim_customer WHERE {excl} "
            f"AND UPPER(TRIM(customer_segment)) IN ({segs})")
        w(self.style.SUCCESS(f'  hidden by segment signal (STAFF/EMPLOYEE): {seg_hidden:,}'))

        # How many caught by employer/segment WOULD survive if we dropped the empid signal?
        would_survive = scalar(
            f"SELECT COUNT(*) FROM delta.gold_db.dim_customer WHERE {excl} AND (({ors}) "
            f"OR UPPER(TRIM(customer_segment)) IN ({segs}))")
        w(self.style.MIGRATE_HEADING('\nIf the fk_bankemployeeid signal is dropped'))
        w(f'  customers still protected by employer/segment: {would_survive:,}')
        w(f'  customers UN-hidden (become visible to RMs):   ~{emp_hidden:,} '
          f'(minus any that also match employer/segment)')
        w(self.style.SUCCESS('\nDone. Paste this whole output back.'))
