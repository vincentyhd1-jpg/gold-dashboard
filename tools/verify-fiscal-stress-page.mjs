import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChromium } from './_browser.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fiscalPath = path.join(ROOT, 'data', 'derived', 'macro_fiscal_stress.json');
const source = fs.readFileSync(path.join(ROOT, 'macro.html'), 'utf8');
const envelope = JSON.parse(fs.readFileSync(fiscalPath, 'utf8'));
const fiscal = envelope.data;
const requestedPaths = [];
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml' };

const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  requestedPaths.push(rel);
  const file = path.join(ROOT, rel);
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
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
    console.log(`FAIL ${name}  ${detail}`);
  }
}

function expectedPercent(value, suffix = '%') {
  return value == null ? '未知' : Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'auto',
  }) + suffix;
}

const browser = await launchChromium();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
page.on('pageerror', error => errors.push(error.message));
await page.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForFunction(() => typeof Chart !== 'undefined'
  && !/正在加载/.test(document.getElementById('fiscalStatus')?.textContent || ''), null,
{ timeout: 30000 });
await page.waitForTimeout(800);

const state = await page.evaluate(() => {
  const chartState = id => {
    const chart = Chart.getChart(document.getElementById(id));
    return chart ? {
      labels: Array.from(chart.data.labels),
      datasets: chart.data.datasets.map(dataset => ({
        label: dataset.label,
        sourceField: dataset.sourceField,
        data: Array.from(dataset.data),
      })),
      yTitle: chart.options.scales.y.title.text,
    } : null;
  };
  const ids = ['fiscalPublicDebtGdp', 'fiscalEffectiveR', 'fiscalNominalG',
    'fiscalRMinusG', 'fiscalPrimaryBalance', 'fiscalStabilizingPrimary',
    'fiscalGap', 'fiscalInterestGdp', 'fiscalInterestReceipts'];
  const panel = document.getElementById('fiscalStressPanel');
  return {
    kpis: Object.fromEntries(ids.map(id => [id, document.getElementById(id)?.textContent])),
    rates: chartState('fiscalRatesChart'),
    primary: chartState('fiscalPrimaryChart'),
    trajectory: document.getElementById('fiscalTrajectory')?.textContent || '',
    method: document.getElementById('fiscalMethodNote')?.textContent || '',
    status: document.getElementById('fiscalStatus')?.textContent || '',
    asOf: {
      complete: document.getElementById('fiscalCompleteAsOf')?.textContent,
      mts: document.getElementById('fiscalMtsAsOf')?.textContent,
      debt: document.getElementById('fiscalDebtAsOf')?.textContent,
      gdp: document.getElementById('fiscalGdpAsOf')?.textContent,
    },
    panelExists: !!panel,
    kpiCount: panel?.querySelectorAll('.fiscal-kpi').length || 0,
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  };
});

check('macro_fiscal_stress.json 被真实请求', requestedPaths.includes('data/derived/macro_fiscal_stress.json'),
  requestedPaths.filter(item => item.includes('fiscal')).join(','));
check('财政可持续性面板与九项 KPI 存在', state.panelExists && state.kpiCount === 9,
  `kpis=${state.kpiCount}`);
const fields = [
  ['fiscalPublicDebtGdp', 'public_debt_gdp_pct', '%'],
  ['fiscalEffectiveR', 'effective_r_pct', '%'],
  ['fiscalNominalG', 'nominal_g_pct', '%'],
  ['fiscalRMinusG', 'r_minus_g_pct_points', ' pp'],
  ['fiscalPrimaryBalance', 'primary_balance_gdp_pct', '%'],
  ['fiscalStabilizingPrimary', 'stabilizing_primary_balance_pct_gdp', '%'],
  ['fiscalGap', 'fiscal_gap_pct_gdp', '%'],
  ['fiscalInterestGdp', 'net_interest_gdp_pct', '%'],
  ['fiscalInterestReceipts', 'net_interest_receipts_pct', '%'],
];
for (const [id, field, suffix] of fields) {
  check(`KPI ${field} 逐字段匹配派生 latest`,
    state.kpis[id] === expectedPercent(fiscal.latest[field], suffix),
    `${state.kpis[id]} != ${expectedPercent(fiscal.latest[field], suffix)}`);
}
check('状态文字对应派生 trajectory_condition',
  fiscal.latest.trajectory_condition === 'gap_positive'
    ? state.trajectory.includes('不满足稳定算术条件')
    : fiscal.latest.trajectory_condition === 'stabilizing_condition_met'
      ? state.trajectory.includes('当前简化债务动力学，稳定条件暂时满足')
      : state.trajectory.includes('数据不足'), state.trajectory);
check('stock-flow residual 作为非零诊断展示',
  state.trajectory.includes(expectedPercent(fiscal.latest.stock_flow_residual_pct_gdp, ' pp'))
    && Math.abs(fiscal.latest.stock_flow_residual_pct_gdp) > 0,
  state.trajectory);
check('完整历史边界约 2016 明示且无预测年份', state.method.includes('不提供预测年份')
  && source.includes('完整一致历史约从 2016-Q1 开始'));
check('无压力颜色或阈值评级', fiscal.meta.stress_level === 'unscored'
  && fiscal.meta.threshold_version === null
  && !/GREEN|YELLOW|ORANGE|RED|绿色|黄色|橙色|红色/.test(
    documentText(state)));
check('债务/MTS/GDP/完整季度 as-of 独立展示',
  state.asOf.complete === fiscal.meta.complete_through
    && state.asOf.mts === fiscal.meta.mts_as_of
    && state.asOf.debt === fiscal.meta.daily_public_debt_as_of
    && state.asOf.gdp === `${fiscal.meta.gdp_as_of.slice(0, 4)}-Q${Math.floor((Number(fiscal.meta.gdp_as_of.slice(5, 7)) - 1) / 3) + 1}`,
  JSON.stringify(state.asOf));
check('r/g/r-g 图只读三个派生字段', state.rates?.datasets.map(item => item.sourceField).join(',')
  === 'effective_r_pct,nominal_g_pct,r_minus_g_pct_points');
check('初级余额图只读实际与 p* 派生字段', state.primary?.datasets.map(item => item.sourceField).join(',')
  === 'primary_balance_gdp_pct,stabilizing_primary_balance_pct_gdp');
check('两图 observation 数与派生季度数一致', state.rates?.labels.length === fiscal.quarterly.length
  && state.primary?.labels.length === fiscal.quarterly.length
  && state.rates.datasets.every(dataset => dataset.data.length === fiscal.quarterly.length)
  && state.primary.datasets.every(dataset => dataset.data.length === fiscal.quarterly.length));
check('图表逐点保留派生 null/数值而不补点',
  JSON.stringify(state.rates.datasets[0].data)
    === JSON.stringify(fiscal.quarterly.map(row => row.effective_r_pct))
  && JSON.stringify(state.primary.datasets[0].data)
    === JSON.stringify(fiscal.quarterly.map(row => row.primary_balance_gdp_pct)));
check('前端没有 fiscal gap / p* / r / g 算术重算',
  !/latest\.(?:stabilizing_primary_balance_pct_gdp|effective_r_pct|nominal_g_pct)\s*[-+*/]/.test(source)
  && !/fiscalGap\s*=/.test(source)
  && source.includes("['fiscalGap', 'fiscal_gap_pct_gdp', '%']"));
check('页面无 console/page errors', errors.length === 0, errors.join(' | '));
check('桌面页面无横向溢出', state.scrollWidth <= state.innerWidth + 1,
  `${state.scrollWidth}/${state.innerWidth}`);

// Unknown contract: intercept only the fiscal envelope. Null must remain 未知,
// never 0/0.00, while the page and other modules stay alive.
const unknownPage = await browser.newPage({ viewport: { width: 1200, height: 800 } });
const unknownPayload = structuredClone(envelope);
for (const [, field] of fields) unknownPayload.data.latest[field] = null;
unknownPayload.data.latest.calculation_status = 'incomplete';
unknownPayload.data.latest.trajectory_condition = 'unknown';
await unknownPage.route('**/data/derived/macro_fiscal_stress.json?*', route => route.fulfill({
  status: 200, contentType: 'application/json', body: JSON.stringify(unknownPayload),
}));
await unknownPage.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
await unknownPage.waitForFunction(() => !/正在加载/.test(
  document.getElementById('fiscalStatus')?.textContent || ''), null, { timeout: 30000 });
const unknownState = await unknownPage.evaluate(() => ({
  values: Array.from(document.querySelectorAll('.fiscal-kpi-value')).map(node => node.textContent),
  trajectory: document.getElementById('fiscalTrajectory')?.textContent || '',
  debtAlive: !!Chart.getChart(document.getElementById('debtOverviewChart')),
}));
check('null KPI 全部显示未知且不变成 0', unknownState.values.length === 9
  && unknownState.values.every(value => value === '未知')
  && !unknownState.values.some(value => /^0(?:\.00)?/.test(value)), unknownState.values.join(','));
check('incomplete trajectory 显示不可知', unknownState.trajectory.includes('数据不足'));
check('fiscal unknown 不影响债务模块', unknownState.debtAlive);
await unknownPage.close();

// Fiscal fetch fails: rates/CPI/debt stay alive.
const fiscalFailPage = await browser.newPage();
await fiscalFailPage.route('**/data/derived/macro_fiscal_stress.json?*', route => route.abort());
await fiscalFailPage.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
await fiscalFailPage.waitForFunction(() => !/正在加载/.test(
  document.getElementById('fiscalStatus')?.textContent || '')
  && !/正在加载/.test(document.getElementById('debtStatus')?.textContent || ''), null,
{ timeout: 30000 });
const fiscalFailState = await fiscalFailPage.evaluate(() => ({
  fiscalError: document.getElementById('fiscalStatus')?.classList.contains('err'),
  ust: !!Chart.getChart(document.getElementById('ustChart')),
  cpi: !!Chart.getChart(document.getElementById('cpiChart')),
  debt: !!Chart.getChart(document.getElementById('debtOverviewChart')),
}));
check('fiscal 数据失败不影响 rates/CPI/debt', fiscalFailState.fiscalError
  && fiscalFailState.ust && fiscalFailState.cpi && fiscalFailState.debt,
JSON.stringify(fiscalFailState));
await fiscalFailPage.close();

// All prior macro inputs fail: fiscal remains independently readable.
const otherFailPage = await browser.newPage();
for (const pattern of ['**/data/derived/macro_rates.json?*',
  '**/data/derived/macro_cpi.json?*', '**/data/derived/macro_debt.json?*',
  '**/data/treasury_debt_daily.json?*']) {
  await otherFailPage.route(pattern, route => route.abort());
}
await otherFailPage.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
await otherFailPage.waitForFunction(() => !/正在加载/.test(
  document.getElementById('fiscalStatus')?.textContent || ''), null, { timeout: 30000 });
const otherFailState = await otherFailPage.evaluate(() => ({
  fiscalError: document.getElementById('fiscalStatus')?.classList.contains('err'),
  fiscalRates: !!Chart.getChart(document.getElementById('fiscalRatesChart')),
  fiscalPrimary: !!Chart.getChart(document.getElementById('fiscalPrimaryChart')),
}));
check('rates/CPI/debt 失败不影响 fiscal', !otherFailState.fiscalError
  && otherFailState.fiscalRates && otherFailState.fiscalPrimary,
JSON.stringify(otherFailState));
await otherFailPage.close();

const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true,
  hasTouch: true });
await mobile.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
await mobile.waitForFunction(() => !/正在加载/.test(
  document.getElementById('fiscalStatus')?.textContent || ''), null, { timeout: 30000 });
const mobileState = await mobile.evaluate(() => ({
  scrollWidth: document.documentElement.scrollWidth,
  innerWidth: window.innerWidth,
  panelWidth: document.getElementById('fiscalStressPanel')?.getBoundingClientRect().width,
  chartCount: document.querySelectorAll('#fiscalStressPanel canvas').length,
}));
check('移动端 fiscal 面板无横向溢出且两图存活',
  mobileState.scrollWidth <= mobileState.innerWidth + 1
    && mobileState.panelWidth <= mobileState.innerWidth
    && mobileState.chartCount === 2, JSON.stringify(mobileState));
await mobile.close();

await browser.close();
server.close();
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;

function documentText(value) {
  return `${value.trajectory} ${value.method} ${value.status}`;
}
