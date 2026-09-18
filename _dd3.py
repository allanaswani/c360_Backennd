import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
rows = t.execute(
  "SELECT application_name, source_table_name, data_warehouse_gold_table_name, "
  "business_description FROM delta.gold_db.data_dictionary "
  "ORDER BY application_name")
for r in rows:
    app = r['application_name'] or '?'
    if not any(k in app.upper() for k in ('INSUR', 'HFBI', 'BANC', 'HFDI', 'PROPERT', 'CRM')):
        continue
    gold = r['data_warehouse_gold_table_name'] or '(no gold table)'
    src = r['source_table_name'] or '?'
    desc = (r['business_description'] or '')[:110]
    print(f"[{app}] {gold}  <- {src}\n      {desc}", flush=True)
print('\n--- all application names ---', flush=True)
for r in t.execute("SELECT application_name, count(*) n FROM delta.gold_db.data_dictionary GROUP BY 1 ORDER BY 1"):
    print(' ', r, flush=True)
