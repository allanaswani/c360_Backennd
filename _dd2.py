import os, django
os.environ.setdefault('DJANGO_SETTINGS_MODULE','config.settings')
django.setup()
from c360.warehouse.factory import get_gateway
t = get_gateway()._t
rows = t.execute(
  "SELECT application_name, source_table_name, data_warehouse_gold_table_name, "
  "business_description FROM delta.gold_db.data_dictionary "
  "ORDER BY application_name, data_warehouse_gold_table_name")
apps = {}
for r in rows:
    apps.setdefault((r['application_name'] or '?'), []).append(r)
for app, rs in apps.items():
    print(f'\n===== {app} ({len(rs)} tables) =====', flush=True)
    for r in rs:
        desc = (r['business_description'] or '')[:95]
        print(f"  {r['data_warehouse_gold_table_name']:<42} | {desc}", flush=True)
