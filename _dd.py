import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
def q(l, sql, n=60):
    print('\n---', l, flush=True)
    try: rows = t.execute(sql)
    except Exception as e: print('  ERR', type(e).__name__, str(e)[:200], flush=True); return []
    for r in rows[:n]: print(' ', r, flush=True)
    if not rows: print('  (none)', flush=True)
    return rows

q('data_dictionary columns',
  "SELECT column_name, data_type FROM delta.information_schema.columns "
  "WHERE table_schema='gold_db' AND table_name='data_dictionary' ORDER BY ordinal_position")
q('row count', "SELECT count(*) n FROM delta.gold_db.data_dictionary")
