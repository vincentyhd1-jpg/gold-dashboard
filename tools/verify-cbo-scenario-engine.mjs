import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const require = createRequire(import.meta.url);
const engine = require(path.join(ROOT, 'assets', 'js', 'cbo-scenario-engine.js'));
const engineSource = fs.readFileSync(
  path.join(ROOT, 'assets', 'js', 'cbo-scenario-engine.js'), 'utf8');
const envelope = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'cbo_scenario_basis.json'), 'utf8'));
const basis = envelope.data;
let passed = 0;
let failed = 0;

function check(name, ok, detail = '') {
  if (ok) {
    passed++;
    console.log(`PASS ${name}${detail ? `  ${detail}` : ''}`);
  } else {
    failed++;
    console.log(`FAIL ${name}  ${detail}`);
  }
}

function rejects(fn) {
  try { fn(); return false; } catch { return true; }
}

const original = JSON.stringify(basis);
const zero = engine.runScenario(basis, engine.resetConfig());
const sourceRows = basis.annual.filter(row => row.kind === 'projection');
const zeroRows = zero.rows.filter(row => row.kind === 'projection');
check('scenario basis 是 strict annual schema v0 envelope', envelope.schema_version === 0
  && envelope.source === 'cbo_fiscal_scenario_basis' && envelope.freq === 'annual'
  && envelope.date_field === 'year');
check('zero shock 逐年精确复现官方 debt_bn', zeroRows.every((row, index) =>
  Object.is(row.scenarioDebtBn, sourceRows[index].baseline_debt_bn)));
check('zero shock 逐年精确复现官方 debt/GDP', zeroRows.every((row, index) =>
  Object.is(row.scenarioDebtPctGdp, sourceRows[index].baseline_debt_pct_gdp)));
check('zero shock difference 逐年严格为 0', zeroRows.every(row => row.differencePp === 0));

const primary = engine.runScenario(basis, { ...engine.resetConfig(),
  primaryBalanceShockPp: 1 });
const interest = engine.runScenario(basis, { ...engine.resetConfig(),
  netInterestSpendingShockPp: 1 });
const growth = engine.runScenario(basis, { ...engine.resetConfig(),
  growthShockPp: 1 });
const baselineTerminal = zero.summary.terminalBaselineDebtPctGdp;
check('+1pp primary balance lowers terminal debt/GDP',
  primary.summary.terminalScenarioDebtPctGdp < baselineTerminal,
  primary.summary.terminalScenarioDebtPctGdp.toFixed(6));
check('+1pp net interest spending raises terminal debt/GDP',
  interest.summary.terminalScenarioDebtPctGdp > baselineTerminal,
  interest.summary.terminalScenarioDebtPctGdp.toFixed(6));
check('+1pp nominal GDP growth lowers terminal debt/GDP',
  growth.summary.terminalScenarioDebtPctGdp < baselineTerminal,
  growth.summary.terminalScenarioDebtPctGdp.toFixed(6));

const late = engine.runScenario(basis, { ...engine.resetConfig(), startYear: 2032,
  growthShockPp: -1, primaryBalanceShockPp: -1, netInterestSpendingShockPp: 1 });
check('start year 之前逐字段等于 baseline', late.rows.filter(row => row.year < 2032)
  .every(row => row.scenarioDebtBn === row.baselineDebtBn
    && row.scenarioDebtPctGdp === row.baselineDebtPctGdp
    && row.differencePp === 0 && row.shockActive === false));
check('start year 当年开始应用 shock', late.rows.find(row => row.year === 2032)?.shockActive
  && late.rows.find(row => row.year === 2032)?.differencePp !== 0);
check('Reset 返回 exact zero config', JSON.stringify(engine.resetConfig()) === JSON.stringify({
  startYear: 2026, growthShockPp: 0, primaryBalanceShockPp: 0,
  netInterestSpendingShockPp: 0,
}));
check('相同输入 deterministic', JSON.stringify(engine.runScenario(basis, {
  startYear: 2029, growthShockPp: 0.25, primaryBalanceShockPp: -0.5,
  netInterestSpendingShockPp: 0.75,
})) === JSON.stringify(engine.runScenario(basis, {
  startYear: 2029, growthShockPp: 0.25, primaryBalanceShockPp: -0.5,
  netInterestSpendingShockPp: 0.75,
})));
check('engine 不 mutate input', JSON.stringify(basis) === original);
check('NaN/Infinity 拒绝', rejects(() => engine.runScenario(basis,
  { ...engine.resetConfig(), growthShockPp: NaN }))
  && rejects(() => engine.runScenario(basis,
    { ...engine.resetConfig(), netInterestSpendingShockPp: Infinity })));
check('非法 start year 拒绝', rejects(() => engine.runScenario(basis,
  { ...engine.resetConfig(), startYear: 2025 }))
  && rejects(() => engine.runScenario(basis,
    { ...engine.resetConfig(), startYear: 2036.5 })));
check('shock bounds guard', rejects(() => engine.runScenario(basis,
  { ...engine.resetConfig(), growthShockPp: 3.25 }))
  && rejects(() => engine.runScenario(basis,
    { ...engine.resetConfig(), primaryBalanceShockPp: -3.25 }))
  && rejects(() => engine.runScenario(basis,
    { ...engine.resetConfig(), netInterestSpendingShockPp: 2.25 })));
const missing = JSON.parse(original);
missing.annual[2].baseline_sfa_pct_gdp = null;
check('null required field 拒绝且不变成 0', rejects(() => engine.runScenario(missing,
  engine.resetConfig())));
check('basis methodology 禁止 forward gap/probability/crisis',
  basis.methodology.no_forward_fiscal_gap === true
  && basis.methodology.no_probability === true
  && basis.methodology.no_crisis_year === true
  && !JSON.stringify(zero).includes('effective_r'));
check('scenario output 不含概率、危机年份或 Fiscal Gap',
  !/(probability|crisis_year|fiscal_gap|effective_r)/i.test(JSON.stringify(zero)));
check('scenario 不覆盖 baseline 字段', primary.rows.every((row, index) =>
  row.baselineDebtBn === basis.annual[index].baseline_debt_bn
  && row.baselineDebtPctGdp === basis.annual[index].baseline_debt_pct_gdp));
check('C18B engine 不读取 C17 effective_r', !/effective_r/i.test(engineSource));

console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
