import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = fs.readFileSync(path.join(ROOT, 'macro.html'), 'utf8');
const raw = JSON.parse(fs.readFileSync(path.join(
  ROOT, 'data', 'wgc_official_reserves.json'), 'utf8'));
const envelope = JSON.parse(fs.readFileSync(path.join(
  ROOT, 'data', 'derived', 'official_reserve_composition.json'), 'utf8'));
const rawRows = raw?.data || [];
const rows = envelope?.data?.observations || [];
const method = envelope?.data?.methodology || {};
const sources = envelope?.data?.sources || {};
const aggregateNames = new Set(['WLD', 'WORLD', 'GLOBAL', 'EUR', 'EUU', 'EMU',
  'IMF', 'ECB', 'BIS', 'G7', 'G20']);

let passed = 0;
let failed = 0;
function check(name, ok) {
  console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`);
  if (ok) passed++; else failed++;
}
function sortedUniqueStrings(values) {
  return Array.isArray(values) && values.length > 0
    && values.every(value => typeof value === 'string')
    && values.every((value, index) => index === 0 || value > values[index - 1]);
}
function intersection(...lists) {
  return [...new Set(lists[0])].filter(value => lists.slice(1)
    .every(list => new Set(list).has(value))).sort();
}
function periodFromDate(date) {
  const [year, month] = date.split('-').map(Number);
  return `${year}-Q${month / 3}`;
}

check('strict official reserve envelopes', raw.schema_version === 0
  && raw.source === 'world_gold_council_official_reserves'
  && raw.freq === 'quarterly' && raw.date_field === 'date'
  && envelope.schema_version === 0
  && envelope.source === 'derived_official_reserve_composition'
  && envelope.freq === 'quarterly' && envelope.date_field === 'period');

check('WGC source has no World region or institution aggregate series', rawRows.length > 0
  && raw.info.includes('source_provided_world_global_aggregate=false')
  && raw.info.includes('world_region_institution_aggregate_series=none_present_and_forbidden')
  && rawRows.every(row => Object.values(row.reporting_entities || {}).flat()
    .every(name => /^[A-Z]{3}$/.test(name) && !aggregateNames.has(name))));

check('gold numerator and denominator use identical entity intersection', rawRows.length > 0
  && rawRows.every(row => {
    const entities = row.reporting_entities || {};
    if (!['gold_reserves', 'gold_reserves_tonnes', 'total_reserves', 'matched']
      .every(field => sortedUniqueStrings(entities[field]))) return false;
    const expected = intersection(entities.gold_reserves,
      entities.gold_reserves_tonnes, entities.total_reserves);
    return JSON.stringify(entities.matched) === JSON.stringify(expected)
      && row.gold_reporting_entities_count === entities.gold_reserves.length
      && row.gold_tonnes_reporting_entities_count === entities.gold_reserves_tonnes.length
      && row.total_reserves_reporting_entities_count === entities.total_reserves.length
      && row.matched_reporting_entities_count === entities.matched.length;
  }));

const rawByPeriod = new Map(rawRows.map(row => [periodFromDate(row.date), row]));
check('derived gold sample is coupled to current matched WGC aggregation', rows.length > 0
  && rows.every(row => {
    const upstream = rawByPeriod.get(row.period);
    return upstream
      && row.official_gold_value_usd === Math.round(
        upstream.official_gold_value_usd_mn * 1e6)
      && row.total_official_reserve_assets_usd === Math.round(
        upstream.total_official_reserve_assets_usd_mn * 1e6)
      && row.matched_reporting_entities_count === upstream.matched_reporting_entities_count
      && JSON.stringify(row.matched_reporting_entities)
        === JSON.stringify(upstream.reporting_entities.matched);
  }));

check('Gold share uses matched reporting-sample denominator', rows.length > 0
  && rows.every(row => Math.abs(row.official_gold_share_pct
    - row.official_gold_value_usd / row.total_official_reserve_assets_usd * 100) < 1e-10)
  && method.gold_numerator_denominator_same_entity_universe === true
  && method.denominator_source_provided_global_aggregate === false);

check('global TIC numerator is not divided by partial WGC denominator', rows.length > 0
  && rows.every(row => !Object.hasOwn(row, 'foreign_official_ust_share_pct'))
  && method.ust_ratio_status === 'not_produced_unmatched_statistical_universe'
  && method.tic_numerator_denominator_same_entity_universe === false
  && source.includes('与 WGC 匹配报告样本不一致，因此本图不生产美债占比'));

check('dynamic reporting sample is not labeled Global Total Official Reserves',
  method.denominator_scope === 'quarter-specific matched WGC reporting-entity sample'
  && method.denominator_is_dynamic_available_country_sum === false
  && source.includes('不是 source-provided Global Total Official Reserve Assets')
  && !source.includes('全球官方储备构成：黄金 vs 外国官方机构持有美债')
  && !source.includes("text: '% of Total Official Reserve Assets'"));

check('COFER USD share is forbidden as UST share',
  method.cofer_usd_share_is_not_ust_share === true
  && source.includes('不使用 COFER USD share'));

check('TIC/FRED source and foreign-official scope are explicit',
  sources.foreign_official_ust === 'TIC/FRED FORTREASPOS99990'
  && source.includes("sourceLabel: 'TIC/FRED FORTREASPOS99990'")
  && source.includes('财政代理及国际/区域官方机构'));

check('canonical series labels do not overstate statistical universes',
  source.includes("lineDataset('匹配报告经济体官方部门黄金占样本储备比例'")
  && source.includes("lineDataset('匹配报告经济体官方部门黄金金额'")
  && source.includes("lineDataset('外国官方机构持有美债额'")
  && !source.includes("lineDataset('美元储备'")
  && !source.includes("lineDataset('全球黄金总市值'"));

check('quarter alignment forbids fill interpolation and exact-day TIC claims',
  method.no_forward_fill === true && method.no_interpolation === true
  && rows.every(row => /-(03|06|09|12)-01$/.test(row.ust_source_date))
  && new Set(rows.map(row => row.ust_source_date)).size === rows.length
  && source.includes('source period ${sourcePeriod}')
  && !source.includes('as_of ${sourceDate}'));

check('one visible card uses honest three-series dual-axis contract',
  source.includes('id="officialReservePanel"')
  && source.includes("text: '% of matched WGC reserve sample'")
  && source.includes("text: 'USD tn'")
  && (source.match(/sourceField: '(?:official_gold|foreign_official_ust)[^']*'/g) || []).length === 3
  && !source.includes("sourceField: 'foreign_official_ust_share_pct'"));

check('old card text and old artifact request absent',
  !source.includes('全球黄金总市值 vs 美债总额')
  && !source.includes("loadJson('data/derived/gold_vs_debt.json"));

console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
