import os, django, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
def p(x): sys.stdout.write((str(x).encode('ascii','replace').decode('ascii'))+'\n'); sys.stdout.flush()
def q(l, sql, params=None, n=12):
    p('\n--- ' + l)
    try: rows = t.execute(sql, params) if params else t.execute(sql)
    except Exception as e: p('  ERR ' + type(e).__name__ + ' ' + str(e)[:200]); return []
    for r in rows[:n]: p('  ' + str(r))
    if not rows: p('  (none)')
    return rows

q('hfbi_prospect_data columns',
  "SELECT column_name FROM delta.information_schema.columns "
  "WHERE table_schema='gold_db' AND table_name='hfbi_prospect_data' ORDER BY ordinal_position", n=40)
q('row count', "SELECT count(*) n FROM delta.gold_db.hfbi_prospect_data")
