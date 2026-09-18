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

q('Do VT001 / SU356 have premium RECEIPTS despite having no policy?',
  "SELECT TRIM(receipt_client) client, count(*) receipts "
  "FROM delta.gold_db.hfbi_receipt_data "
  "WHERE TRIM(receipt_client) IN ('VT001','SU356','RA386') GROUP BY 1")

q('What does receipt_client look like - client_no or a name?',
  "SELECT TRIM(receipt_client) sample, count(*) n FROM delta.gold_db.hfbi_receipt_data "
  "GROUP BY 1 ORDER BY 2 DESC LIMIT 5")

q('Receipts referencing VTN / SUSAN WANJIKU KARIUKI / MICHAEL NJAU by text',
  "SELECT TRIM(receipt_client) client, count(*) n FROM delta.gold_db.hfbi_receipt_data "
  "WHERE UPPER(receipt_client) LIKE '%VTN%' OR UPPER(receipt_client) LIKE '%NJAU%' "
  "OR (UPPER(receipt_client) LIKE '%SUSAN%' AND UPPER(receipt_client) LIKE '%KARIUKI%') "
  "GROUP BY 1 LIMIT 15")
