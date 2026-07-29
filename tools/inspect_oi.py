import json
with open('data/oi.json') as f:
    records = json.load(f)
print('Total records:', len(records))
print('Date range:', records[0]['date'], 'to', records[-1]['date'])
for r in records:
    months = r.get('months', [])
    has_chg = months and 'oi_chg' in months[0]
    chg_count = sum(1 for m in months if m.get('oi_chg') is not None)
    print(' ', r['date'], ' ', len(months), 'months  has_oi_chg:', has_chg, '  chg_count:', chg_count)
