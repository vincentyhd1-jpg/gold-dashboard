import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChromium } from './_browser.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const siteArg = process.argv.indexOf('--site-root');
const SITE_ROOT = siteArg >= 0 ? path.resolve(ROOT, process.argv[siteArg + 1] || '') : ROOT;
const source = fs.readFileSync(path.join(SITE_ROOT, 'macro.html'), 'utf8');
const envelope = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'cbo_scenario_basis.json'), 'utf8'));
const basis = envelope.data;
const requested = [];
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml' };

const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  requested.push(rel);
  const file = path.join(SITE_ROOT, rel);
  if (!file.startsWith(SITE_ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end('not found');
    return;
  }
  res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;

let passed = 0;
let failed = 0;
function check(name, ok, detail = '') {
  if (ok) { passed++; console.log(`PASS ${name}${detail ? `  ${detail}` : ''}`); }
  else { failed++; console.log(`FAIL ${name}  ${detail}`); }
}

async function openPage(browser, routeSetup, viewport = { width: 1440, height: 1000 }) {
  const page = await browser.newPage({ viewport });
  const errors = [];
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('pageerror', error => errors.push(error.message));
  if (routeSetup) await routeSetup(page);
  await page.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => typeof Chart !== 'undefined'
    && !/正在加载/.test(document.getElementById('scenarioStatus')?.textContent || ''), null,
  { timeout: 30000 });
  await page.waitForTimeout(400);
  return { page, errors };
}

function scenarioState() {
  const chart = Chart.getChart(document.getElementById('cboScenarioChart'));
  return {
    panel: !!document.getElementById('cboScenarioPanel'),
    title: document.querySelector('#cboScenarioPanel .card-title')?.textContent || '',
    subtitle: document.querySelector('#cboScenarioPanel .card-sub')?.textContent || '',
    disclaimer: document.getElementById('scenarioDisclaimer')?.textContent || '',
    controls: ['scenarioStartYear', 'scenarioGrowthShock', 'scenarioPrimaryShock',
      'scenarioInterestShock', 'scenarioReset'].map(id => !!document.getElementById(id)),
    values: {
      start: document.getElementById('scenarioStartYear')?.value,
      growth: document.getElementById('scenarioGrowthShock')?.value,
      primary: document.getElementById('scenarioPrimaryShock')?.value,
      interest: document.getElementById('scenarioInterestShock')?.value,
    },
    labels: chart ? Array.from(chart.data.labels) : [],
    datasets: chart ? chart.data.datasets.map(dataset => ({
      label: dataset.label, sourceField: dataset.sourceField, sourceKind: dataset.sourceKind,
      data: Array.from(dataset.data), borderDash: Array.from(dataset.borderDash || []),
    })) : [],
    kpis: ['scenarioTerminalDebt', 'scenarioBaselineDebt', 'scenarioDebtDifference',
      'scenarioPeakDebt', 'scenarioTerminalBalance'].map(id =>
      document.getElementById(id)?.textContent || ''),
    status: document.getElementById('scenarioStatus')?.textContent || '',
    controlStatus: document.getElementById('scenarioControlStatus')?.textContent || '',
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  };
}

const browser = await launchChromium();
const baseline = await openPage(browser);
let state = await baseline.page.evaluate(scenarioState);
const projection = basis.annual.filter(row => row.kind === 'projection');
check('cbo_scenario_basis.json 被真实请求',
  requested.includes('data/derived/cbo_scenario_basis.json'), requested.join(','));
check('scenario engine asset 被真实请求',
  requested.includes('assets/js/cbo-scenario-engine.js'), requested.join(','));
check('Scenario Lab 独立面板与标题存在', state.panel
  && state.title === 'CBO 财政情景实验室 / Fiscal Scenario Lab'
  && state.subtitle.includes('官方 baseline 始终只读'));
check('四个 controls 与 Reset 全部存在', state.controls.every(Boolean));
check('默认 controls 是 2026 与三项零冲击', state.values.start === '2026'
  && state.values.growth === '0' && state.values.primary === '0'
  && state.values.interest === '0');
check('zero shock UI 逐年精确等于 CBO baseline',
  JSON.stringify(state.datasets[0]?.data) === JSON.stringify(
    projection.map(row => row.baseline_debt_pct_gdp))
  && JSON.stringify(state.datasets[1]?.data) === JSON.stringify(
    projection.map(row => row.baseline_debt_pct_gdp)));
check('baseline 与 scenario 标签/来源不能混写',
  state.datasets[0]?.label === 'CBO Baseline'
  && state.datasets[0]?.sourceKind === 'cbo_official_baseline'
  && state.datasets[1]?.label === 'User Scenario / 用户情景'
  && state.datasets[1]?.sourceKind === 'synthetic_user_scenario'
  && !/CBO (?:Forecast|Projection)/.test(state.datasets[1]?.label || ''));
check('CBO baseline 虚线与 User Scenario 实线视觉区分',
  state.datasets[0]?.borderDash.length > 0 && state.datasets[1]?.borderDash.length === 0);
check('zero shock 五项 KPI 完整且 difference 为 0.00 pp',
  state.kpis.every(value => value && value !== '--') && state.kpis[2] === '0.00 pp',
  state.kpis.join(' | '));
check('免责声明锁定 deterministic/not CBO/probability/no crisis-year 语义',
  state.disclaimer.includes('确定性敏感性分析')
  && state.disclaimer.includes('不是 CBO 预测')
  && state.disclaimer.includes('不是发生概率估计')
  && state.disclaimer.includes('不代表债务危机、违约或所谓“失控年份”'));
check('页面未引入 effective_r / forward Fiscal Gap / 概率模型',
  !/CboScenarioEngine[\s\S]{0,500}effective_r/.test(source)
  && !/scenario[^\n]*(?:fiscal_gap|probability|crisis_year)/i.test(source));

await baseline.page.locator('#scenarioPrimaryShock').fill('1');
await baseline.page.waitForFunction(() => {
  const chart = Chart.getChart(document.getElementById('cboScenarioChart'));
  return chart && chart.data.datasets[1].data.at(-1) < chart.data.datasets[0].data.at(-1);
});
state = await baseline.page.evaluate(scenarioState);
check('Primary +1pp 交互真实更新 chart 与 KPI',
  state.datasets[1].data.at(-1) < state.datasets[0].data.at(-1)
  && state.kpis[2].startsWith('-') && state.values.primary === '1');

await baseline.page.locator('#scenarioStartYear').selectOption('2032');
await baseline.page.locator('#scenarioGrowthShock').fill('-1');
await baseline.page.waitForTimeout(100);
state = await baseline.page.evaluate(scenarioState);
check('start year 之前 chart 完全保持 baseline', state.labels.every((year, index) =>
  Number(year) >= 2032 || state.datasets[0].data[index] === state.datasets[1].data[index]));
check('start year 起 scenario 发生变化', state.labels.some((year, index) =>
  Number(year) >= 2032 && state.datasets[0].data[index] !== state.datasets[1].data[index]));

await baseline.page.locator('#cboScenarioChart').scrollIntoViewIfNeeded();
const point = await baseline.page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('cboScenarioChart'));
  const element = chart.getDatasetMeta(1).data.at(-1);
  const props = element.getProps(['x', 'y'], true);
  const rect = chart.canvas.getBoundingClientRect();
  return { x: rect.left + props.x, y: rect.top + props.y };
});
await baseline.page.mouse.move(point.x, point.y);
await baseline.page.waitForFunction(() => {
  const tooltip = Chart.getChart(document.getElementById('cboScenarioChart')).tooltip;
  return tooltip?.opacity > 0 && (tooltip.title || []).includes('2036');
});
const tooltip = await baseline.page.evaluate(() => {
  const tip = Chart.getChart(document.getElementById('cboScenarioChart')).tooltip;
  return [...(tip.title || []), ...(tip.body || []).flatMap(item => item.lines || []),
    ...(tip.afterBody || [])].join(' | ');
});
check('真实 hover tooltip 含 year/baseline/scenario/difference', tooltip.includes('2036')
  && tooltip.includes('CBO Baseline') && tooltip.includes('User Scenario / 用户情景')
  && tooltip.includes('Difference'), tooltip);

await baseline.page.locator('#scenarioReset').click();
await baseline.page.waitForFunction(() =>
  document.getElementById('scenarioDebtDifference')?.textContent === '0.00 pp');
state = await baseline.page.evaluate(scenarioState);
check('Reset 恢复 exact zero shock 与 baseline', state.values.start === '2026'
  && state.values.growth === '0' && state.values.primary === '0'
  && state.values.interest === '0'
  && JSON.stringify(state.datasets[0].data) === JSON.stringify(state.datasets[1].data));
check('baseline 页面无 console/page errors', baseline.errors.length === 0,
  baseline.errors.join(' | '));

const cboFail = await openPage(browser, page => page.route(
  '**/data/derived/cbo_baseline_latest.json?*', route => route.abort()));
const cboFailState = await cboFail.page.evaluate(() => ({
  scenario: !!Chart.getChart(document.getElementById('cboScenarioChart')),
  fiscal: !!Chart.getChart(document.getElementById('fiscalGapChart')),
  cboError: document.getElementById('cboStatus')?.classList.contains('err'),
}));
check('CBO panel failure 不影响 Scenario Lab 或 C17', cboFailState.cboError
  && cboFailState.scenario && cboFailState.fiscal, JSON.stringify(cboFailState));
await cboFail.page.close();

const fiscalFail = await openPage(browser, page => page.route(
  '**/data/derived/macro_fiscal_stress.json?*', route => route.abort()));
const fiscalFailState = await fiscalFail.page.evaluate(() => ({
  scenario: !!Chart.getChart(document.getElementById('cboScenarioChart')),
  cbo: !!Chart.getChart(document.getElementById('cboDebtChart')),
}));
check('C17 failure 不影响 Scenario Lab 或 CBO projection', fiscalFailState.scenario
  && fiscalFailState.cbo, JSON.stringify(fiscalFailState));
await fiscalFail.page.close();

const basisFail = await openPage(browser, page => page.route(
  '**/data/derived/cbo_scenario_basis.json?*', route => route.abort()));
const basisFailState = await basisFail.page.evaluate(() => ({
  scenarioError: document.getElementById('scenarioStatus')?.classList.contains('err'),
  cbo: !!Chart.getChart(document.getElementById('cboDebtChart')),
  fiscal: !!Chart.getChart(document.getElementById('fiscalGapChart')),
}));
check('Scenario basis failure 只挂 Scenario Lab', basisFailState.scenarioError
  && basisFailState.cbo && basisFailState.fiscal, JSON.stringify(basisFailState));
await basisFail.page.close();

const mobile = await openPage(browser, null, { width: 390, height: 844 });
const mobileState = await mobile.page.evaluate(scenarioState);
check('移动端 Scenario Lab 无横向溢出', mobileState.scrollWidth <= mobileState.innerWidth + 1,
  `${mobileState.scrollWidth}/${mobileState.innerWidth}`);
check('移动端 Scenario Lab 正常建图且无 page errors', mobileState.datasets.length === 2
  && mobile.errors.length === 0, mobile.errors.join(' | '));
await mobile.page.close();

await baseline.page.close();
await browser.close();
server.close();
console.log(`page errors: ${baseline.errors.length ? baseline.errors.join(' | ') : 'none'}`);
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
