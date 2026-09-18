import os, django, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
def p(x): sys.stdout.write(str(x).encode('ascii','replace').decode('ascii')+'\n'); sys.stdout.flush()
def q(l, sql, params=None, n=15):
    p('\n--- ' + l)
    try: rows = t.execute(sql, params) if params else t.execute(sql)
    except Exception as e: p('  ERR ' + type(e).__name__ + ' ' + str(e)[:200]); return []
    for r in rows[:n]: p('  ' + str(r))
    if not rows: p('  (none)')
    return rows

q('rpt_c360_insurance_policies_summary columns',
  "SELECT column_name FROM delta.information_schema.columns WHERE table_schema='gold_db' "
  "AND table_name='rpt_c360_insurance_policies_summary' ORDER BY ordinal_position", n=30)

q('ALL insurance clients matching KIMANI (any first name)',
  "SELECT TRIM(client_no) client_no, TRIM(name) name FROM delta.gold_db.hfbi_customer_data "
  "WHERE UPPER(name) LIKE '%KIMANI%' ORDER BY 2 LIMIT 40", n=40)

q('ALL insurance clients matching VTN or VENTURES',
  "SELECT TRIM(client_no) client_no, TRIM(name) name FROM delta.gold_db.hfbi_customer_data "
  "WHERE UPPER(name) LIKE '%VTN%' OR UPPER(name) LIKE '%VENTURE%' ORDER BY 2 LIMIT 25", n=25)

q('ALL insurance clients named SUSAN + KARIUKI, and do they have policies?',
  "SELECT TRIM(h.client_no) client_no, TRIM(h.name) name, "
  "(SELECT count(*) FROM delta.gold_db.rpt_c360_customer_policies_summary s "
  "  WHERE TRIM(s.policy_client_no) = TRIM(h.client_no)) policy_rows "
  "FROM delta.gold_db.hfbi_customer_data h "
  "WHERE UPPER(h.name) LIKE '%SUSAN%' AND UPPER(h.name) LIKE '%KARIUKI%' LIMIT 20", n=20)
