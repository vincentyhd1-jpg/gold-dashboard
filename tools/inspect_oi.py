import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_envelope import unwrap

# 经 unwrap 读：oi.json 信封化前后都取到同一份帧数组。
# 直读会在写入端信封化那天崩成 KeyError: 0（dict 上取 [0]）。
with open('data/oi.json') as f:
    records = unwrap(json.load(f))
print('Total records:', len(records))
print('Date range:', records[0]['date'], 'to', records[-1]['date'])
for r in records:
    months = r.get('months', [])
    has_chg = months and 'oi_chg' in months[0]
    chg_count = sum(1 for m in months if m.get('oi_chg') is not None)
    print(' ', r['date'], ' ', len(months), 'months  has_oi_chg:', has_chg, '  chg_count:', chg_count)
