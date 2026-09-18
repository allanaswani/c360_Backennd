import os, django, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
def p(x): sys.stdout.write(str(x).encode('ascii','replace').decode('ascii')+'\n'); sys.stdout.flush()
def q(l, sql, params=None, n=30):
    p('\n--- ' + l)
    try: rows = t.execute(sql, params) if params else t.execute(sql)
    except Exception as e: p('  ERR ' + type(e).__name__ + ' ' + str(e)[:200]); return []
    for r in rows[:n]: p('  ' + str(r))
    if not rows: p('  (none)')
    return rows

q('Every insurance client whose name contains NJAU',
  "SELECT TRIM(client_no) client_no, TRIM(name) name, "
  "(SELECT count(*) FROM delta.gold_db.rpt_c360_customer_policies_summary s "
  "  WHERE TRIM(s.policy_client_no)=TRIM(h.client_no)) pol "
  "FROM delta.gold_db.hfbi_customer_data h WHERE UPPER(h.name) LIKE '%NJAU%' ORDER BY 2", n=40)

q('Every insurance client starting MICHAEL',
  "SELECT TRIM(client_no) client_no, TRIM(name) name, "
  "(SELECT count(*) FROM delta.gold_db.rpt_c360_customer_policies_summary s "
  "  WHERE TRIM(s.policy_client_no)=TRIM(h.client_no)) pol "
  "FROM delta.gold_db.hfbi_customer_data h WHERE UPPER(h.name) LIKE 'MICHAEL%' ORDER BY 2", n=40)

q('VTN Ventures: client row + policy rows',
  "SELECT TRIM(client_no) client_no, TRIM(name) name, "
  "(SELECT count(*) FROM delta.gold_db.rpt_c360_customer_policies_summary s "
  "  WHERE TRIM(s.policy_client_no)=TRIM(h.client_no)) pol_summary, "
  "(SELECT count(*) FROM delta.gold_db.hfbi_policy_data pd "
  "  WHERE TRIM(pd.policy_client_no)=TRIM(h.client_no)) pol_raw "
  "FROM delta.gold_db.hfbi_customer_data h WHERE UPPER(h.name) LIKE 'VTN%'")

q('Susan SU356 in the RAW policy feed (not just the summary)',
  "SELECT count(*) n FROM delta.gold_db.hfbi_policy_data WHERE TRIM(policy_client_no)='SU356'")
