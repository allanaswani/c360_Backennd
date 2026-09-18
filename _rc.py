import os, django, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
def p(x): sys.stdout.write(str(x).encode('ascii','replace').decode('ascii')+'\n'); sys.stdout.flush()
def q(l, sql, params=None, n=35):
    p('\n--- ' + l)
    try: rows = t.execute(sql, params) if params else t.execute(sql)
    except Exception as e: p('  ERR ' + type(e).__name__ + ' ' + str(e)[:220]); return []
    for r in rows[:n]: p('  ' + str(r))
    if not rows: p('  (none)')
    return rows

q('hfbi_receipt_data columns',
  "SELECT column_name, data_type FROM delta.information_schema.columns WHERE table_schema='gold_db' "
  "AND table_name='hfbi_receipt_data' ORDER BY ordinal_position", n=40)

q('Distinct clients in receipts, and how many match the customer register by name',
  "WITH r AS (SELECT DISTINCT UPPER(REGEXP_REPLACE(TRIM(receipt_client),'[^A-Za-z0-9]','')) k "
  "             FROM delta.gold_db.hfbi_receipt_data WHERE TRIM(COALESCE(receipt_client,''))<>''), "
  "     h AS (SELECT DISTINCT UPPER(REGEXP_REPLACE(TRIM(name),'[^A-Za-z0-9]','')) k "
  "             FROM delta.gold_db.hfbi_customer_data) "
  "SELECT count(*) receipt_clients, count(CASE WHEN h.k IS NOT NULL THEN 1 END) in_register "
  "  FROM r LEFT JOIN h ON h.k = r.k")
