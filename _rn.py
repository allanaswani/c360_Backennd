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

q('Michael Njau Kimani: receipts -> risknotes -> do policies exist?',
  "SELECT count(*) receipts, count(DISTINCT TRIM(r.receipt_risknote_no)) risknotes, "
  "count(DISTINCT pd.policy_risknote_no) matched_policies "
  "FROM delta.gold_db.hfbi_receipt_data r "
  "LEFT JOIN (SELECT DISTINCT TRIM(policy_risknote_no) policy_risknote_no "
  "             FROM delta.gold_db.hfbi_policy_data) pd "
  "  ON pd.policy_risknote_no = TRIM(r.receipt_risknote_no) "
  "WHERE UPPER(TRIM(r.receipt_client)) = 'MICHAEL NJAU KIMANI'")

q('VTN Ventures: same chain',
  "SELECT count(*) receipts, count(DISTINCT TRIM(r.receipt_risknote_no)) risknotes, "
  "count(DISTINCT pd.policy_risknote_no) matched_policies "
  "FROM delta.gold_db.hfbi_receipt_data r "
  "LEFT JOIN (SELECT DISTINCT TRIM(policy_risknote_no) policy_risknote_no "
  "             FROM delta.gold_db.hfbi_policy_data) pd "
  "  ON pd.policy_risknote_no = TRIM(r.receipt_risknote_no) "
  "WHERE UPPER(TRIM(r.receipt_client)) LIKE 'VTN VENTURES%'")

q('Susan Wanjiku Kariuki: same chain',
  "SELECT count(*) receipts, count(DISTINCT TRIM(r.receipt_risknote_no)) risknotes, "
  "count(DISTINCT pd.policy_risknote_no) matched_policies "
  "FROM delta.gold_db.hfbi_receipt_data r "
  "LEFT JOIN (SELECT DISTINCT TRIM(policy_risknote_no) policy_risknote_no "
  "             FROM delta.gold_db.hfbi_policy_data) pd "
  "  ON pd.policy_risknote_no = TRIM(r.receipt_risknote_no) "
  "WHERE UPPER(TRIM(r.receipt_client)) = 'SUSAN WANJIKU KARIUKI'")

q('Overall: how often do receipts carry a risknote that resolves to a policy?',
  "SELECT count(*) receipts, "
  "count(CASE WHEN TRIM(COALESCE(receipt_risknote_no,''))<>'' THEN 1 END) with_risknote "
  "FROM delta.gold_db.hfbi_receipt_data")
