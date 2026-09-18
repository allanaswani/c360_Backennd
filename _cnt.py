import os, django, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
def p(x): sys.stdout.write(str(x).encode('ascii','replace').decode('ascii')+'\n'); sys.stdout.flush()
names = [r['table_name'] for r in t.execute(
  "SELECT table_name FROM delta.information_schema.tables WHERE table_schema='gold_db' "
  "AND (table_name LIKE 'hfbi%' OR table_name LIKE '%polic%' OR table_name LIKE '%insur%') ORDER BY 1")]
for n in names:
    try:
        c = t.execute(f"SELECT count(*) n FROM delta.gold_db.{n}")[0]['n']
    except Exception as e:
        c = 'ERR ' + str(e)[:40]
    p(f'{n:<48} {c}')
