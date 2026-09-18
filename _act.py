import os, django, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
def p(x): sys.stdout.write(str(x).encode('ascii','replace').decode('ascii')+'\n'); sys.stdout.flush()
def q(l, sql, params=None, n=20):
    p('\n--- ' + l)
    try: rows = t.execute(sql, params) if params else t.execute(sql)
    except Exception as e: p('  ERR ' + type(e).__name__ + ' ' + str(e)[:220]); return []
    for r in rows[:n]: p('  ' + str(r))
    if not rows: p('  (none)')
    return rows

q('Definitive: Michael Njau Kimani anywhere in the insurance register',
  "SELECT count(*) n FROM delta.gold_db.hfbi_customer_data "
  "WHERE UPPER(name) LIKE '%MICHAEL%' AND UPPER(name) LIKE '%NJAU%'")

q('receipt/payment columns (which carry a client reference?)',
  "SELECT table_name, column_name FROM delta.information_schema.columns "
  "WHERE table_schema='gold_db' AND table_name IN ('hfbi_receipt_data','hfbi_payment_data') "
  "AND (lower(column_name) LIKE '%client%' OR lower(column_name) LIKE '%name%') ORDER BY 1,2", n=20)

q('How many insurance clients have NO policy rows at all?',
  "SELECT count(*) clients, "
  "count(CASE WHEN p.client_no IS NULL THEN 1 END) with_no_policy "
  "FROM (SELECT DISTINCT TRIM(client_no) client_no FROM delta.gold_db.hfbi_customer_data) h "
  "LEFT JOIN (SELECT DISTINCT TRIM(policy_client_no) client_no "
  "             FROM delta.gold_db.hfbi_policy_data) p ON p.client_no = h.client_no")
