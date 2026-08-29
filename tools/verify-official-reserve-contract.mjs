import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = fs.readFileSync(path.join(ROOT, 'macro.html'), 'utf8');
const envelope = JSON.parse(fs.readFileSync(path.join(
  ROOT, 'data', 'derived', 'official_reserve_composition.json'), 'utf8'));
const rows = envelope?.data?.observations || [];
const method = envelope?.data?.methodology || {};
const sources = envelope?.data?.sources || {};

let passed = 0;
let failed = 0;
function check(name, ok) {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`);
  if (ok) passed++; else failed++;
}

check('strict official reserve envelope', envelope.schema_version === 0
  && envelope.source === 'derived_official_reserve_composition'
  && envelope.freq === 'quarterly' && envelope.date_field === 'period');
check('Gold share uses Total Official Reserve Assets denominator', rows.length > 0
  && rows.every(row => Math.abs(row.official_gold_share_pct
    - row.official_gold_value_usd / row.total_official_reserve_assets_usd * 100) < 1e-10));
check('UST share uses same common denominator', rows.length > 0
  && rows.every(row => Math.abs(row.foreign_official_ust_share_pct
    - row.foreign_official_ust_value_usd / row.total_official_reserve_assets_usd * 100) < 1e-10));
check('COFER USD share is forbidden as UST share',
  method.cofer_usd_share_is_not_ust_share === true
  && source.includes('不使用 COFER USD share')
  && source.includes('WGC 国别报告覆盖随 vintage 变化'));
check('TIC/FRED source label present',
  sources.foreign_official_ust === 'TIC/FRED FORTREASPOS99990'
  && source.includes("sourceLabel: 'TIC/FRED FORTREASPOS99990'"));
check('canonical foreign official UST label',
  source.includes("lineDataset('外国官方机构持有美债额'")
  && !source.includes("lineDataset('美元储备'"));
check('official gold is not global gold market value',
  source.includes("lineDataset('全球央行持有黄金金额'")
  && !source.includes("lineDataset('全球黄金总市值'"));
check('quarter alignment forbids fill/interpolation',
  method.no_forward_fill === true && method.no_interpolation === true
  && rows.every(row => /-(03|06|09|12)-01$/.test(row.ust_source_date))
  && new Set(rows.map(row => row.ust_source_date)).size === rows.length);
check('one visible card and dual-axis four-series contract',
  source.includes('id="officialReservePanel"')
  && source.includes("text: '% of Total Official Reserve Assets'")
  && source.includes("text: 'USD tn'")
  && (source.match(/sourceField: '(?:official_gold|foreign_official_ust)[^']*'/g) || []).length === 4);
check('old card text and old artifact request absent',
  !source.includes('全球黄金总市值 vs 美债总额')
  && !source.includes("loadJson('data/derived/gold_vs_debt.json"));

console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
