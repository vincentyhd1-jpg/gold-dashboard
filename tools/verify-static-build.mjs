import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = path.join(ROOT, 'dist');
const CONFIG = path.join(ROOT, 'wrangler.jsonc');
const ROOT_PAGES = ['index.html', 'macro.html', 'term-3d.html'];
const PUBLIC_DIRS = ['assets', 'css', 'js'];
const REQUIRED_PATHS = [
  ...ROOT_PAGES,
  'assets/favicon.svg',
  'assets/js/cbo-scenario-engine.js',
  'css/chart.css',
  'js/data-helpers.js',
  'data/cot.json',
  'data/derived/macro_debt.json',
  'data/treasury_mts_fiscal.json',
  'data/derived/macro_fiscal_stress.json',
  'data/derived/fiscal_risk_monitor.json',
  'data/derived/cbo_baseline_latest.json',
  'data/derived/cbo_scenario_basis.json',
];
const FORBIDDEN_PATHS = [
  'AGENTS.md', 'CLAUDE.md', 'README.md',
  'fetch_fred.py', 'fetch_treasury_debt.py', 'fetch_treasury_fiscal.py',
  'fetch_cbo_baseline.py', 'derive_cbo_scenario_basis.py', 'tools', 'docs', '.github', '.git',
  'derive_fiscal_risk_monitor.py',
  'data/quarantine', 'data/cbo',
];

let passed = 0;
let failed = 0;

function check(name, ok, detail = '') {
  if (ok) {
    passed++;
    console.log(`PASS ${name}${detail ? `  ${detail}` : ''}`);
  } else {
    failed++;
    console.log(`FAIL ${name}${detail ? `  ${detail}` : ''}`);
  }
}

function slash(relativePath) {
  return relativePath.split(path.sep).join('/');
}

function listFiles(root) {
  if (!fs.existsSync(root)) return [];
  const files = [];
  const visit = directory => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        files.push(`${slash(path.relative(root, absolute))}::SYMLINK`);
      } else if (entry.isDirectory()) {
        visit(absolute);
      } else if (entry.isFile()) {
        files.push(slash(path.relative(root, absolute)));
      }
    }
  };
  visit(root);
  return files.sort();
}

function hash(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function readJson(relativePath) {
  return JSON.parse(fs.readFileSync(path.join(DIST, relativePath), 'utf8'));
}

function sourceManifest() {
  const files = [...ROOT_PAGES];
  for (const directory of PUBLIC_DIRS) {
    files.push(...listFiles(path.join(ROOT, directory))
      .map(file => `${directory}/${file}`));
  }
  files.push(...listFiles(path.join(ROOT, 'data'))
    .filter(file => !file.split('/').includes('quarantine')
      && !file.startsWith('cbo/')
      && path.extname(file).toLowerCase() === '.json')
    .map(file => `data/${file}`));
  return files.sort();
}

let config;
try {
  config = JSON.parse(fs.readFileSync(CONFIG, 'utf8'));
  check('wrangler.jsonc 是可解析配置', true);
} catch (error) {
  check('wrangler.jsonc 是可解析配置', false, error.message);
}
check('wrangler name 固定为 gold-dashboard', config?.name === 'gold-dashboard',
  String(config?.name));
check('wrangler assets.directory 固定为 ./dist',
  config?.assets?.directory === './dist', String(config?.assets?.directory));
check('纯静态 assets 配置没有 Worker main',
  config && !Object.hasOwn(config, 'main'));
check('wrangler 无多余 bindings', config
  && Object.keys(config).sort().join(',') === 'assets,compatibility_date,name'
  && Object.keys(config.assets || {}).join(',') === 'directory');

check('dist 目录存在', fs.existsSync(DIST) && fs.statSync(DIST).isDirectory());
for (const required of REQUIRED_PATHS) {
  check(`dist 必需文件 ${required}`, fs.existsSync(path.join(DIST, required)));
}

const actual = listFiles(DIST);
const expected = sourceManifest();
const missing = expected.filter(file => !actual.includes(file));
const extra = actual.filter(file => !expected.includes(file));
check('dist 完整复制白名单 manifest', missing.length === 0,
  missing.length ? `missing=${missing.join(',')}` : `${expected.length} files`);
check('dist 不包含白名单外文件', extra.length === 0,
  extra.length ? `extra=${extra.join(',')}` : 'no extras');

const byteMismatches = expected.filter(file => fs.existsSync(path.join(DIST, file))
  && hash(path.join(ROOT, file)) !== hash(path.join(DIST, file)));
check('dist 文件与源文件逐字节一致', byteMismatches.length === 0,
  byteMismatches.join(','));

let cboBaseline;
let cboBasis;
try {
  cboBaseline = readJson('data/derived/cbo_baseline_latest.json');
  cboBasis = readJson('data/derived/cbo_scenario_basis.json');
  check('dist CBO baseline / scenario basis 均为合法 JSON', true);
} catch (error) {
  check('dist CBO baseline / scenario basis 均为合法 JSON', false, error.message);
}
const baselineVintage = cboBaseline?.data?.vintage;
const basisVintage = cboBasis?.data?.vintage;
check('dist CBO scenario basis vintage 对应当前 baseline',
  typeof baselineVintage?.vintage_id === 'string' && baselineVintage.vintage_id.length > 0
  && typeof baselineVintage?.publication_date === 'string'
  && baselineVintage.publication_date.length > 0
  && basisVintage?.vintage_id === baselineVintage.vintage_id
  && basisVintage?.publication_date === baselineVintage?.publication_date);
const basisUpstream = cboBasis?.derived_from;
check('dist CBO scenario basis derived_from 对应当前 baseline',
  Array.isArray(basisUpstream) && basisUpstream.length === 1
  && basisUpstream[0]?.source === cboBaseline?.source
  && basisUpstream[0]?.generated_at === cboBaseline?.generated_at
  && JSON.stringify(basisUpstream[0]?.coverage) === JSON.stringify(cboBaseline?.coverage)
  && basisUpstream[0]?.envelope === true);
const baselineRows = cboBaseline?.data?.annual;
const basisRows = cboBasis?.data?.annual;
const directFields = [
  ['debt_held_by_public_bn', 'baseline_debt_bn'],
  ['debt_held_by_public_pct_gdp', 'baseline_debt_pct_gdp'],
  ['nominal_gdp_bn', 'baseline_gdp_bn'],
  ['nominal_g_pct', 'baseline_nominal_g_pct'],
  ['primary_balance_pct_gdp', 'baseline_primary_balance_pct_gdp'],
  ['net_interest_pct_gdp', 'baseline_net_interest_pct_gdp'],
  ['overall_balance_pct_gdp', 'baseline_overall_balance_pct_gdp'],
];
const basisMatchesBaseline = Array.isArray(baselineRows) && Array.isArray(basisRows)
  && baselineRows.length === basisRows.length
  && baselineRows.every((baselineRow, index) => {
    const basisRow = basisRows[index];
    if (!basisRow || basisRow.year !== baselineRow.year || basisRow.kind !== baselineRow.kind
      || !directFields.every(([baselineField, basisField]) =>
        Object.is(basisRow[basisField], baselineRow[baselineField]))) return false;
    if (index === 0) {
      return basisRow.baseline_sfa_bn === null
        && basisRow.baseline_sfa_pct_gdp === null;
    }
    const previousDebt = baselineRows[index - 1].debt_held_by_public_bn;
    const deficit = -baselineRow.overall_balance_pct_gdp / 100
      * baselineRow.nominal_gdp_bn;
    const expectedSfaBn = baselineRow.debt_held_by_public_bn - previousDebt - deficit;
    const expectedSfaPct = expectedSfaBn / baselineRow.nominal_gdp_bn * 100;
    return Number.isFinite(basisRow.baseline_sfa_bn)
      && Number.isFinite(basisRow.baseline_sfa_pct_gdp)
      && Math.abs(basisRow.baseline_sfa_bn - expectedSfaBn) <= 1e-9
      && Math.abs(basisRow.baseline_sfa_pct_gdp - expectedSfaPct) <= 1e-12;
  });
check('dist CBO scenario basis 业务数据对应 baseline', basisMatchesBaseline);

let fiscalSource;
let fiscalMonitor;
try {
  fiscalSource = readJson('data/derived/macro_fiscal_stress.json');
  fiscalMonitor = readJson('data/derived/fiscal_risk_monitor.json');
  check('dist fiscal stress / risk monitor 均为合法 JSON', true);
} catch (error) {
  check('dist fiscal stress / risk monitor 均为合法 JSON', false, error.message);
}
const monitorUpstream = fiscalMonitor?.derived_from;
check('dist fiscal risk monitor derived_from 对应当前 fiscal stress',
  Array.isArray(monitorUpstream) && monitorUpstream.length === 1
  && monitorUpstream[0]?.source === fiscalSource?.source
  && monitorUpstream[0]?.generated_at === fiscalSource?.generated_at
  && JSON.stringify(monitorUpstream[0]?.coverage) === JSON.stringify(fiscalSource?.coverage)
  && monitorUpstream[0]?.envelope === true);
const fiscalRows = fiscalSource?.data?.quarterly;
const monitorRows = fiscalMonitor?.data?.quarterly;
const monitorYoyFields = [
  ['debt_gdp_yoy_change_pp', 'public_debt_gdp_pct'],
  ['primary_balance_yoy_change_pp', 'primary_balance_gdp_pct'],
  ['fiscal_gap_yoy_change_pp', 'fiscal_gap_pct_gdp'],
  ['r_minus_g_yoy_change_pp', 'r_minus_g_pct_points'],
  ['net_interest_gdp_yoy_change_pp', 'net_interest_gdp_pct'],
  ['net_interest_receipts_yoy_change_pp', 'net_interest_receipts_pct'],
];
const fiscalByQuarter = new Map((fiscalRows || []).map(row => [row.quarter, row]));
const monitorMatchesFiscal = Array.isArray(fiscalRows) && Array.isArray(monitorRows)
  && fiscalRows.length === monitorRows.length
  && fiscalRows.every((sourceRow, index) => {
    const row = monitorRows[index];
    if (!row || !Object.keys(sourceRow).every(field => Object.is(row[field], sourceRow[field]))) {
      return false;
    }
    const match = /^(\d{4})-Q([1-4])$/.exec(sourceRow.quarter || '');
    const prior = match
      ? fiscalByQuarter.get(`${Number(match[1]) - 1}-Q${match[2]}`) : null;
    return monitorYoyFields.every(([outputField, sourceField]) => {
      const expectedYoy = sourceRow.calculation_status === 'complete'
        && prior?.calculation_status === 'complete'
        && sourceRow[sourceField] !== null && prior[sourceField] !== null
        ? sourceRow[sourceField] - prior[sourceField] : null;
      return Object.is(row[outputField], expectedYoy);
    });
  });
check('dist fiscal risk monitor 业务数据与 YoY 对应当前 fiscal stress',
  monitorMatchesFiscal);

for (const forbidden of FORBIDDEN_PATHS) {
  check(`dist 不公开 ${forbidden}`, !fs.existsSync(path.join(DIST, forbidden)));
}
check('dist 不包含 Python/Markdown/PDF/临时文件', actual.every(file =>
  !/\.(?:py|md|pdf|tmp|bak)$/i.test(file) && !file.endsWith('::SYMLINK')),
  actual.filter(file => /\.(?:py|md|pdf|tmp|bak)$/i.test(file)
    || file.endsWith('::SYMLINK')).join(','));

console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
