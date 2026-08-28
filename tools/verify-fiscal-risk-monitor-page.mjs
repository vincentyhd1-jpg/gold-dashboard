import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChromium } from './_browser.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const siteArg = process.argv.indexOf('--site-root');
const SITE_ROOT = siteArg >= 0
  ? path.resolve(ROOT, process.argv[siteArg + 1] || '') : ROOT;
const source = fs.readFileSync(path.join(SITE_ROOT, 'macro.html'), 'utf8');
const monitorEnvelope = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'fiscal_risk_monitor.json'), 'utf8'));
const ratesEnvelope = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'macro_rates.json'), 'utf8'));
const cboEnvelope = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'cbo_baseline_latest.json'), 'utf8'));
const monitor = monitorEnvelope.data;
const rates = ratesEnvelope.data;
const cbo = cboEnvelope.data;
const requestedPaths = [];
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml' };

const server = http.createServer((req, res) => {
  const relative = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  requestedPaths.push(relative);
  const file = path.join(SITE_ROOT, relative);
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
  if (ok) {
    passed++;
    console.log(`PASS ${name}${detail ? `  ${detail}` : ''}`);
  } else {
    failed++;
    console.log(`FAIL ${name}${detail ? `  ${detail}` : ''}`);
  }
}

function formatValue(value, suffix = '%') {
  return value == null ? '--' : Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'auto',
  }) + suffix;
}

function formatYoy(value) {
  return value == null ? '同比：未知' : '同比：' + Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'always',
  }) + ' pp';
}

function latestObservation(field) {
  return [...rates.ust].reverse().find(row => row[field] != null
    && Number.isFinite(Number(row[field])));
}

async function openPage(browser, setupRoutes) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('pageerror', error => errors.push(error.message));
  if (setupRoutes) await setupRoutes(page);
  await page.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => typeof Chart !== 'undefined'
    && !/正在加载/.test(document.getElementById('riskMonitorStatus')?.textContent || ''),
  null, { timeout: 30000 });
  await page.waitForTimeout(400);
  return { page, errors };
}

function collectState() {
  const stateOfChart = id => {
    const chart = Chart.getChart(document.getElementById(id));
    return chart ? {
      labels: Array.from(chart.data.labels),
      datasets: chart.data.datasets.map(dataset => ({
        label: dataset.label,
        sourceField: dataset.sourceField,
        sourceKind: dataset.sourceKind,
        presentationOnly: dataset.presentationOnly === true,
        data: Array.from(dataset.data),
      })),
    } : null;
  };
  const ids = ['riskDebtGdp', 'riskFiscalGap', 'riskRMinusG', 'riskPrimaryBalance',
    'riskInterestGdp', 'riskInterestReceipts', 'riskDebtGdpYoy', 'riskFiscalGapYoy',
    'riskRMinusGYoy', 'riskPrimaryBalanceYoy', 'riskInterestGdpYoy',
    'riskInterestReceiptsYoy', 'riskDebtCondition', 'riskGapCondition',
    'riskRMinusGCondition', 'riskPrimaryCondition'];
  return {
    panel: !!document.getElementById('fiscalRiskMonitorPanel'),
    title: document.querySelector('#fiscalRiskMonitorPanel .card-title')?.textContent || '',
    subtitle: document.getElementById('riskMonitorSubtitle')?.textContent || '',
    disclaimer: document.getElementById('riskMonitorDisclaimer')?.textContent || '',
    marketDisclaimer: document.getElementById('riskMarketDisclaimer')?.textContent || '',
    status: document.getElementById('riskMonitorStatus')?.textContent || '',
    values: Object.fromEntries(ids.map(id => [id, document.getElementById(id)?.textContent])),
    charts: {
      debt: stateOfChart('riskDebtChart'),
      gap: stateOfChart('riskGapChart'),
      rMinusG: stateOfChart('riskRMinusGChart'),
      interestReceipts: stateOfChart('riskInterestReceiptsChart'),
    },
    market: {
      dgs2: document.getElementById('riskDgs2')?.textContent,
      dgs2Date: document.getElementById('riskDgs2Date')?.textContent,
      dgs10: document.getElementById('riskDgs10')?.textContent,
      dgs10Date: document.getElementById('riskDgs10Date')?.textContent,
      dgs30: document.getElementById('riskDgs30')?.textContent,
      dgs30Date: document.getElementById('riskDgs30Date')?.textContent,
    },
    cbo: {
      title: document.querySelector('#riskCboContext .risk-monitor-context-title')?.textContent,
      y2026: ['riskCbo2026Debt', 'riskCbo2026Primary', 'riskCbo2026Interest']
        .map(id => document.getElementById(id)?.textContent),
      y2036: ['riskCbo2036Debt', 'riskCbo2036Primary', 'riskCbo2036Interest']
        .map(id => document.getElementById(id)?.textContent),
      publication: document.getElementById('riskCboPublication')?.textContent,
      vintage: document.getElementById('riskCboVintage')?.textContent,
    },
    asof: {
      complete: document.getElementById('riskCompleteAsOf')?.textContent,
      observed: document.getElementById('riskObservedAsOf')?.textContent,
      lag: document.getElementById('riskCompleteLag')?.textContent,
    },
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  };
}

let browser;
try {
  browser = await launchChromium();
} catch (error) {
  server.close();
  console.error(error.stack || error.message);
  process.exitCode = 1;
  throw error;
}

const baseline = await openPage(browser);
const state = await baseline.page.evaluate(collectState);
const latest = monitor.latest_complete;
check('fiscal_risk_monitor.json 被真实请求',
  requestedPaths.includes('data/derived/fiscal_risk_monitor.json'));
check('C18C panel 与标题存在', state.panel
  && state.title.includes('U.S. Fiscal Risk Monitor'));
check('副标题锁定多指标描述性监测且不声称概率/危机年份',
  state.subtitle.includes('多指标描述性监测')
  && state.subtitle.includes('不提供风险评分') && state.subtitle.includes('危机概率'));

const kpis = [
  ['riskDebtGdp', 'riskDebtGdpYoy', 'public_debt_gdp_pct', 'debt_gdp_yoy_change_pp', '%'],
  ['riskFiscalGap', 'riskFiscalGapYoy', 'fiscal_gap_pct_gdp', 'fiscal_gap_yoy_change_pp', '% GDP'],
  ['riskRMinusG', 'riskRMinusGYoy', 'r_minus_g_pct_points', 'r_minus_g_yoy_change_pp', ' pp'],
  ['riskPrimaryBalance', 'riskPrimaryBalanceYoy', 'primary_balance_gdp_pct', 'primary_balance_yoy_change_pp', '% GDP'],
  ['riskInterestGdp', 'riskInterestGdpYoy', 'net_interest_gdp_pct', 'net_interest_gdp_yoy_change_pp', '% GDP'],
  ['riskInterestReceipts', 'riskInterestReceiptsYoy', 'net_interest_receipts_pct', 'net_interest_receipts_yoy_change_pp', '%'],
];
for (const [valueId, yoyId, valueField, yoyField, suffix] of kpis) {
  check(`${valueField} KPI 直接读取 latest complete`,
    state.values[valueId] === formatValue(latest[valueField], suffix), state.values[valueId]);
  check(`${yoyField} KPI 直接读取派生 YoY`,
    state.values[yoyId] === formatYoy(latest[yoyField]), state.values[yoyId]);
}
check('历史/观测/滞后截至时间分开显示',
  state.asof.complete === monitor.latest_complete_quarter
  && state.asof.observed === monitor.latest_observed_quarter
  && state.asof.lag === `${monitor.complete_lag_quarters} 个季度`);
check('四项 descriptive condition 直接表达派生状态且不评级',
  state.values.riskDebtCondition.includes('较四个季度前上升')
  && state.values.riskGapCondition.includes('当前稳定算术条件满足')
  && state.values.riskGapCondition.includes('不代表观测债务率必然下降')
  && state.values.riskRMinusGCondition.includes('下行帮助')
  && state.values.riskRMinusGCondition.includes('all else equal')
  && state.values.riskPrimaryCondition.includes('deficit'));

const chartContracts = [
  [state.charts.debt, 'public_debt_gdp_pct'],
  [state.charts.gap, 'fiscal_gap_pct_gdp'],
  [state.charts.rMinusG, 'r_minus_g_pct_points'],
  [state.charts.interestReceipts, 'net_interest_receipts_pct'],
];
for (const [chart, field] of chartContracts) {
  check(`${field} 图使用季度源字段且 observation 数未扩张`,
    JSON.stringify(chart?.labels) === JSON.stringify(monitor.quarterly.map(row => row.quarter))
    && chart?.datasets[0]?.sourceField === field
    && chart.datasets[0].sourceKind === 'c17_derived_historical'
    && JSON.stringify(chart.datasets[0].data)
      === JSON.stringify(monitor.quarterly.map(row => row[field])));
}
check('Fiscal Gap 与 r-g 只有 0 数学边界',
  state.charts.gap.datasets.length === 2
  && state.charts.gap.datasets[1].presentationOnly
  && state.charts.gap.datasets[1].data.every(value => value === 0)
  && state.charts.rMinusG.datasets.length === 2
  && state.charts.rMinusG.datasets[1].presentationOnly
  && state.charts.rMinusG.datasets[1].data.every(value => value === 0)
  && state.charts.debt.datasets.length === 1
  && state.charts.interestReceipts.datasets.length === 1);

for (const field of ['dgs2', 'dgs10', 'dgs30']) {
  const observation = latestObservation(field);
  check(`${field.toUpperCase()} 直接显示各自最新真实观测和日期`,
    state.market[field] === formatValue(observation[field], '%')
    && state.market[`${field}Date`] === `截至 ${observation.date}`);
}
check('市场利率明确不替代 effective r 或进入前向动力学',
  state.marketDisclaimer.includes('Treasury 二级市场边际收益率')
  && state.marketDisclaimer.includes('不是 C17 effective r')
  && state.marketDisclaimer.includes('不替代')
  && state.marketDisclaimer.includes('不得直接代入 C17 debt dynamics')
  && state.marketDisclaimer.includes('前向 Fiscal Gap')
  && source.includes('DGS10<strong id="riskDgs10">'));

const cbo2026 = cbo.annual.find(row => row.year === 2026);
const cbo2036 = cbo.annual.find(row => row.year === 2036);
check('CBO FY2026 上下文直接读取官方三字段',
  JSON.stringify(state.cbo.y2026) === JSON.stringify([
    formatValue(cbo2026.debt_held_by_public_pct_gdp),
    formatValue(cbo2026.primary_balance_pct_gdp),
    formatValue(cbo2026.net_interest_pct_gdp),
  ]));
check('CBO FY2036 上下文直接读取官方三字段',
  JSON.stringify(state.cbo.y2036) === JSON.stringify([
    formatValue(cbo2036.debt_held_by_public_pct_gdp),
    formatValue(cbo2036.primary_balance_pct_gdp),
    formatValue(cbo2036.net_interest_pct_gdp),
  ]));
check('CBO publication 与 vintage 分开显示',
  state.cbo.publication === cbo.vintage.publication_date
  && state.cbo.vintage === cbo.vintage.vintage_id
  && state.cbo.title.includes('Federal Fiscal Year'));
check('页面没有 C18C 风险分数/动态阈值/危机判定',
  !/(?:risk_score|composite_score|risk_probability|crisis_year)/.test(source)
  && !/HIGH RISK|高风险|红灯|黄灯|绿灯/.test(
    source.slice(source.indexOf('id="fiscalRiskMonitorPanel"'),
      source.indexOf('id="cboBaselinePanel"'))));
check('C18C 前端不重算 fiscal gap / r-g / YoY / CBO ratio',
  !/(?:fiscal_gap_pct_gdp|r_minus_g_pct_points|debt_gdp_yoy_change_pp)\s*=/.test(source)
  && !/debt_held_by_public_bn\s*\/\s*(?:row\.)?nominal_gdp_bn/.test(source));
check('免责声明明确非评分/概率/危机年份且缺失不补齐',
  state.disclaimer.includes('不是综合风险评分')
  && state.disclaimer.includes('不估计危机概率或危机年份')
  && state.disclaimer.includes('不补点、不插值、不归零'));
check('baseline 无 console/page errors', baseline.errors.length === 0,
  baseline.errors.join(' | '));

await baseline.page.locator('#riskDebtChart').scrollIntoViewIfNeeded();
const hover = await baseline.page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('riskDebtChart'));
  const index = chart.data.labels.length - 2;
  const point = chart.getDatasetMeta(0).data[index].getProps(['x', 'y'], true);
  const rect = chart.canvas.getBoundingClientRect();
  return { x: rect.left + point.x, y: rect.top + point.y,
    quarter: chart.data.labels[index] };
});
await baseline.page.mouse.move(hover.x, hover.y);
await baseline.page.waitForFunction(quarter => {
  const tooltip = Chart.getChart(document.getElementById('riskDebtChart')).tooltip;
  return tooltip?.opacity > 0 && (tooltip.title || []).includes(`季度：${quarter}`);
}, hover.quarter, { timeout: 10000 });
const tooltip = await baseline.page.evaluate(() => {
  const value = Chart.getChart(document.getElementById('riskDebtChart')).tooltip;
  return [...(value.title || []), ...(value.body || []).flatMap(item => item.lines || [])]
    .join(' | ');
});
check('季度图真实 hover 显示季度与值', tooltip.includes(`季度：${hover.quarter}`)
  && tooltip.includes('公众持有债务 / GDP'), tooltip);

const monitorFail = await openPage(browser, page => page.route(
  '**/data/derived/fiscal_risk_monitor.json?*', route => route.abort()));
const monitorFailState = await monitorFail.page.evaluate(() => ({
  monitorError: document.getElementById('riskMonitorStatus')?.classList.contains('err'),
  c17: !!Chart.getChart(document.getElementById('fiscalRatesChart')),
  cbo: !!Chart.getChart(document.getElementById('cboDebtChart')),
  scenario: !!Chart.getChart(document.getElementById('cboScenarioChart')),
}));
check('C18C source 失败不影响 C17/CBO/C18B', monitorFailState.monitorError
  && monitorFailState.c17 && monitorFailState.cbo && monitorFailState.scenario,
JSON.stringify(monitorFailState));
await monitorFail.page.close();

const ratesFail = await openPage(browser, page => page.route(
  '**/data/derived/macro_rates.json?*', route => route.abort()));
const ratesFailState = await ratesFail.page.evaluate(() => ({
  structural: !!Chart.getChart(document.getElementById('riskDebtChart')),
  status: document.getElementById('riskMonitorStatus')?.textContent || '',
  cbo: document.getElementById('riskCbo2036Debt')?.textContent,
}));
check('rates 失败保留结构监测与 CBO 背景', ratesFailState.structural
  && ratesFailState.status.includes('市场利率背景暂不可用')
  && ratesFailState.cbo !== '--', JSON.stringify(ratesFailState));
await ratesFail.page.close();

const cboFail = await openPage(browser, page => page.route(
  '**/data/derived/cbo_baseline_latest.json?*', route => route.abort()));
const cboFailState = await cboFail.page.evaluate(() => ({
  structural: !!Chart.getChart(document.getElementById('riskDebtChart')),
  status: document.getElementById('riskMonitorStatus')?.textContent || '',
  market: document.getElementById('riskDgs10')?.textContent,
}));
check('CBO 失败保留结构监测与市场背景', cboFailState.structural
  && cboFailState.status.includes('CBO baseline 背景暂不可用')
  && cboFailState.market !== '--', JSON.stringify(cboFailState));
await cboFail.page.close();

const scenarioFail = await openPage(browser, page => page.route(
  '**/data/derived/cbo_scenario_basis.json?*', route => route.abort()));
const scenarioFailState = await scenarioFail.page.evaluate(() => ({
  structural: !!Chart.getChart(document.getElementById('riskDebtChart')),
  status: document.getElementById('riskMonitorStatus')?.textContent || '',
  scenarioError: document.getElementById('scenarioStatus')?.classList.contains('err'),
}));
check('scenario basis 失败不影响 C18C', scenarioFailState.structural
  && !scenarioFailState.status.includes('失败') && scenarioFailState.scenarioError,
JSON.stringify(scenarioFailState));
await scenarioFail.page.close();

const beforeSliders = await baseline.page.evaluate(() => ({
  debt: document.getElementById('riskDebtGdp')?.textContent,
  cbo: document.getElementById('riskCbo2036Debt')?.textContent,
}));
await baseline.page.locator('#scenarioGrowthShock').fill('1');
await baseline.page.waitForTimeout(100);
const afterSliders = await baseline.page.evaluate(() => ({
  debt: document.getElementById('riskDebtGdp')?.textContent,
  cbo: document.getElementById('riskCbo2036Debt')?.textContent,
}));
check('C18B sliders 不改变 C18C', JSON.stringify(beforeSliders) === JSON.stringify(afterSliders));

const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
const mobileErrors = [];
mobile.on('console', message => { if (message.type() === 'error') mobileErrors.push(message.text()); });
mobile.on('pageerror', error => mobileErrors.push(error.message));
await mobile.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
await mobile.waitForFunction(() => !!Chart.getChart(document.getElementById('riskDebtChart')),
  null, { timeout: 30000 });
const mobileState = await mobile.evaluate(() => ({
  scrollWidth: document.documentElement.scrollWidth,
  innerWidth: window.innerWidth,
  panelWidth: document.getElementById('fiscalRiskMonitorPanel')?.getBoundingClientRect().width,
  chartCount: document.querySelectorAll('#fiscalRiskMonitorPanel canvas').length,
}));
check('移动端 C18C 无横向溢出且四图存活',
  mobileState.scrollWidth <= mobileState.innerWidth + 1
  && mobileState.panelWidth <= mobileState.innerWidth
  && mobileState.chartCount === 4 && mobileErrors.length === 0,
JSON.stringify({ ...mobileState, errors: mobileErrors }));

await mobile.close();
await baseline.page.close();
await browser.close();
server.close();
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
