import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChromium } from './_browser.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const siteArg = process.argv.indexOf('--site-root');
const SITE_ROOT = siteArg >= 0
  ? path.resolve(ROOT, process.argv[siteArg + 1] || '')
  : ROOT;
const source = fs.readFileSync(path.join(SITE_ROOT, 'macro.html'), 'utf8');
const cboEnvelope = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'cbo_baseline_latest.json'), 'utf8'));
const fiscalEnvelope = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'macro_fiscal_stress.json'), 'utf8'));
const cbo = cboEnvelope.data;
const fiscal = fiscalEnvelope.data;
const requestedPaths = [];
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml' };

const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  requestedPaths.push(rel);
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
  if (ok) {
    passed++;
    console.log(`PASS ${name}${detail ? `  ${detail}` : ''}`);
  } else {
    failed++;
    console.log(`FAIL ${name}  ${detail}`);
  }
}

function expectedPct(value, suffix = '%') {
  return value == null ? '--' : Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: 'auto',
  }) + suffix;
}

async function openPage(browser, routeSetup) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
  page.on('pageerror', error => errors.push(error.message));
  if (routeSetup) await routeSetup(page);
  await page.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => typeof Chart !== 'undefined'
    && !/正在加载/.test(document.getElementById('cboStatus')?.textContent || ''), null,
  { timeout: 30000 });
  await page.waitForTimeout(500);
  return { page, errors };
}

function chartState() {
  const state = id => {
    const chart = Chart.getChart(document.getElementById(id));
    return chart ? {
      labels: Array.from(chart.data.labels),
      datasets: chart.data.datasets.map(dataset => ({
        label: dataset.label,
        sourceField: dataset.sourceField,
        sourceKind: dataset.sourceKind,
        data: Array.from(dataset.data),
        borderDash: Array.from(dataset.borderDash || []),
      })),
      markerStart: chart.options.plugins.cboProjectionMarker?.startYear,
      markerPlugin: chart.config.plugins.some(plugin => plugin.id === 'cboProjectionMarker'),
    } : null;
  };
  return {
    debt: state('cboDebtChart'),
    balance: state('cboBalanceChart'),
    receipts: state('cboReceiptsOutlaysChart'),
    panel: !!document.getElementById('cboBaselinePanel'),
    title: document.querySelector('#cboBaselinePanel .card-title')?.textContent || '',
    subtitle: document.querySelector('#cboBaselinePanel .card-sub')?.textContent || '',
    status: document.getElementById('cboStatus')?.textContent || '',
    meta: {
      publication: document.getElementById('cboPublicationDate')?.textContent,
      horizon: document.getElementById('cboProjectionHorizon')?.textContent,
      actual: document.getElementById('cboActualThrough')?.textContent,
      vintage: document.getElementById('cboVintageId')?.textContent,
      source: document.getElementById('cboSourceLink')?.href,
    },
    kpis: {
      debt: document.getElementById('cboTerminalDebt')?.textContent,
      interest: document.getElementById('cboTerminalInterest')?.textContent,
      primary: document.getElementById('cboTerminalPrimary')?.textContent,
      change: document.getElementById('cboTerminalDebtChange')?.textContent,
    },
    trajectory: document.getElementById('cboTrajectoryNote')?.textContent || '',
    method: document.getElementById('cboMethodNote')?.textContent || '',
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
  };
}

const browser = await launchChromium();
const baseline = await openPage(browser);
const state = await baseline.page.evaluate(chartState);
const vintage = cbo.vintage;
const summary = cbo.summary;
const projectionRows = cbo.annual.filter(row => row.kind === 'projection');
const actualRows = fiscal.quarterly.filter(row => {
  const match = /^(\d{4})-Q4$/.exec(row.quarter || '');
  return match && Number(match[1]) <= vintage.actual_through_year
    && row.public_debt_gdp_pct != null;
});
const labels = [...new Set([
  ...actualRows.map(row => row.quarter.slice(0, 4)),
  ...cbo.annual.map(row => String(row.year)),
])].sort();
const actualByYear = new Map(actualRows.map(row => [row.quarter.slice(0, 4),
  row.public_debt_gdp_pct]));
const cboByYear = new Map(projectionRows.map(row => [String(row.year),
  row.debt_held_by_public_pct_gdp]));

check('cbo_baseline_latest.json 被真实请求',
  requestedPaths.includes('data/derived/cbo_baseline_latest.json'), requestedPaths.join(','));
check('CBO latest 是 strict annual schema v0 envelope', cboEnvelope.schema_version === 0
  && cboEnvelope.source === 'cbo_budget_baseline' && cboEnvelope.freq === 'annual'
  && cboEnvelope.date_field === 'year');
check('CBO 面板标题与 conditional baseline 说明存在', state.panel
  && state.title === '美国财政基准展望（CBO Baseline）'
  && state.subtitle.includes('不是确定性结果'), `${state.title} | ${state.subtitle}`);
check('vintage metadata 完整展示', state.meta.publication === vintage.publication_date
  && state.meta.horizon === `${vintage.projection_start_year}–${vintage.projection_end_year}`
  && state.meta.actual === String(vintage.actual_through_year)
  && state.meta.vintage === vintage.vintage_id);
check('source link 只指向 cbo.gov 官方 publication',
  state.meta.source === `${vintage.source_page_url}/` || state.meta.source === vintage.source_page_url,
  state.meta.source);
check('终点 KPI 直接读取 summary', state.kpis.debt === expectedPct(
  summary.terminal_debt_held_by_public_pct_gdp)
  && state.kpis.interest === expectedPct(summary.terminal_net_interest_pct_gdp)
  && state.kpis.primary === expectedPct(summary.terminal_primary_balance_pct_gdp)
  && state.kpis.change === expectedPct(summary.debt_change_from_actual_through_pp, ' pp'),
JSON.stringify(state.kpis));
check('债务图 actual 年点与 CBO fiscal-year labels 无伪造',
  JSON.stringify(state.debt?.labels) === JSON.stringify(labels));
check('历史实线只取 C17 真实 Q4 observation',
  state.debt?.datasets[0]?.sourceField === 'public_debt_gdp_pct'
  && state.debt.datasets[0].sourceKind === 'historical_actual'
  && state.debt.datasets[0].borderDash.length === 0
  && JSON.stringify(state.debt.datasets[0].data)
    === JSON.stringify(labels.map(year => actualByYear.get(year) ?? null)));
check('CBO debt projection 只含 projection 年且逐年直接读取官方字段',
  state.debt?.datasets[1]?.sourceField === 'debt_held_by_public_pct_gdp'
  && state.debt.datasets[1].sourceKind === 'cbo_official_baseline'
  && JSON.stringify(state.debt.datasets[1].data)
    === JSON.stringify(labels.map(year => cboByYear.get(year) ?? null))
  && state.debt.datasets[1].data[labels.indexOf(String(vintage.actual_through_year))] === null);
check('actual 实线与 projection 虚线视觉区分',
  state.debt?.datasets[0]?.borderDash.length === 0
  && state.debt?.datasets[1]?.borderDash.length > 0);
check('CBO projection 垂直分隔 marker 锁定首年', state.debt?.markerPlugin
  && state.debt.markerStart === vintage.projection_start_year);
check('初级余额/净利息图严格使用 projection 年份',
  JSON.stringify(state.balance?.labels) === JSON.stringify(
    projectionRows.map(row => String(row.year)))
  && state.balance.datasets.map(item => item.sourceField).join(',')
    === 'primary_balance_pct_gdp,net_interest_pct_gdp');
check('初级余额保持 surplus-positive 符号且未转成 deficit-positive',
  JSON.stringify(state.balance?.datasets[0].data)
    === JSON.stringify(projectionRows.map(row => row.primary_balance_pct_gdp))
  && state.balance.datasets[0].data.every(value => value < 0));
check('净利息逐年直接读取官方字段', JSON.stringify(state.balance?.datasets[1].data)
  === JSON.stringify(projectionRows.map(row => row.net_interest_pct_gdp)));
check('财政收入/支出图直接读取官方字段',
  state.receipts?.datasets.map(item => item.sourceField).join(',')
    === 'receipts_pct_gdp,outlays_pct_gdp'
  && JSON.stringify(state.receipts.datasets[0].data)
    === JSON.stringify(projectionRows.map(row => row.receipts_pct_gdp))
  && JSON.stringify(state.receipts.datasets[1].data)
    === JSON.stringify(projectionRows.map(row => row.outlays_pct_gdp)));
check('描述只列 debt rising / primary deficit 年份且不作危机判定',
  summary.baseline_debt_rising_years.every(year => state.trajectory.includes(String(year)))
  && summary.baseline_primary_deficit_years.every(year => state.trajectory.includes(String(year)))
  && state.trajectory.includes('不是危机') && state.trajectory.includes('不是')
  && !Object.hasOwn(summary, 'crisis_year'));
check('CBO market yield 未冒充 C17 effective r',
  cbo.methodology.forward_fiscal_gap_available === false
  && state.method.includes('不把 CBO 市场利率当作 C17 effective r')
  && state.method.includes('不构造 forward Fiscal Gap'));
check('前端没有重算官方 CBO debt baseline',
  !/debt_held_by_public_bn\s*\/\s*(?:row\.)?nominal_gdp_bn/.test(source)
  && !/debt_held_by_public_pct_gdp\s*=/.test(source)
  && source.includes('row.debt_held_by_public_pct_gdp'));
check('基线页面无 console/page errors', baseline.errors.length === 0,
  baseline.errors.join(' | '));
check('桌面 CBO 面板无横向溢出', state.scrollWidth <= state.innerWidth + 1,
  `${state.scrollWidth}/${state.innerWidth}`);

await baseline.page.locator('#cboDebtChart').scrollIntoViewIfNeeded();
const hoverPoint = await baseline.page.evaluate(targetYear => {
  const chart = Chart.getChart(document.getElementById('cboDebtChart'));
  const index = chart.data.labels.indexOf(String(targetYear));
  const element = chart.getDatasetMeta(1).data[index];
  const props = element.getProps(['x', 'y'], true);
  const rect = chart.canvas.getBoundingClientRect();
  return { x: rect.left + props.x, y: rect.top + props.y };
}, summary.terminal_year);
await baseline.page.mouse.move(hoverPoint.x, hoverPoint.y);
await baseline.page.waitForFunction(targetYear => {
  const chart = Chart.getChart(document.getElementById('cboDebtChart'));
  return chart.tooltip?.opacity > 0
    && (chart.tooltip.title || []).includes(String(targetYear));
}, summary.terminal_year, { timeout: 10000 });
const tooltipText = await baseline.page.evaluate(() => {
  const tooltip = Chart.getChart(document.getElementById('cboDebtChart')).tooltip;
  return [...(tooltip.title || []), ...(tooltip.body || []).flatMap(item => item.lines || [])]
    .join(' | ');
});
check('2036 官方 baseline 真实 hover 显示直接 debt/GDP 值',
  tooltipText.includes('2036') && tooltipText.includes('120.21% GDP'), tooltipText);

const cboFail = await openPage(browser, page => page.route(
  '**/data/derived/cbo_baseline_latest.json?*', route => route.abort()));
const cboFailState = await cboFail.page.evaluate(() => ({
  cboError: document.getElementById('cboStatus')?.classList.contains('err'),
  fiscalRates: !!Chart.getChart(document.getElementById('fiscalRatesChart')),
  fiscalGap: !!Chart.getChart(document.getElementById('fiscalGapChart')),
}));
check('CBO 数据失败只挂 CBO 模块而不影响 C17', cboFailState.cboError
  && cboFailState.fiscalRates && cboFailState.fiscalGap, JSON.stringify(cboFailState));
await cboFail.page.close();

const fiscalFail = await openPage(browser, page => page.route(
  '**/data/derived/macro_fiscal_stress.json?*', route => route.abort()));
const fiscalFailState = await fiscalFail.page.evaluate(() => ({
  cboDebt: !!Chart.getChart(document.getElementById('cboDebtChart')),
  cboBalance: !!Chart.getChart(document.getElementById('cboBalanceChart')),
  cboReceipts: !!Chart.getChart(document.getElementById('cboReceiptsOutlaysChart')),
  status: document.getElementById('cboStatus')?.textContent || '',
}));
check('C17 历史失败不影响 CBO projection 且不伪造 actual', fiscalFailState.cboDebt
  && fiscalFailState.cboBalance && fiscalFailState.cboReceipts
  && fiscalFailState.status.includes('C17 历史暂不可用，未伪造 actual'),
JSON.stringify(fiscalFailState));
await fiscalFail.page.close();

const mobile = await browser.newPage({ viewport: { width: 390, height: 844 } });
await mobile.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
await mobile.waitForFunction(() => typeof Chart !== 'undefined'
  && !!Chart.getChart(document.getElementById('cboDebtChart')), null, { timeout: 30000 });
const mobileState = await mobile.evaluate(() => ({
  scrollWidth: document.documentElement.scrollWidth,
  innerWidth: window.innerWidth,
  panelWidth: document.getElementById('cboBaselinePanel')?.getBoundingClientRect().width,
  chartCount: document.querySelectorAll('#cboBaselinePanel canvas').length,
}));
check('移动端 CBO 面板无横向溢出且三图存活',
  mobileState.scrollWidth <= mobileState.innerWidth + 1
  && mobileState.panelWidth <= mobileState.innerWidth
  && mobileState.chartCount === 3, JSON.stringify(mobileState));
await mobile.close();

await baseline.page.close();
await browser.close();
server.close();
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
