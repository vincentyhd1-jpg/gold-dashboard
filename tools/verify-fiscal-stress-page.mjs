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

function expectedFiscalGap(value) {
  return value == null ? '--' : Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'always',
  }) + '% GDP';
}

function expectedMagnitude(value) {
  return value == null ? '--' : Math.abs(Number(value)).toLocaleString('zh-CN', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  }) + '% GDP';
}

function expectedDecision(condition) {
  if (condition === 'gap_positive') return '稳定条件不满足';
  if (condition === 'stabilizing_condition_met') return '稳定条件满足';
  return '数据不足，暂不判断';
}

async function hoverFiscalQuarter(targetPage, quarter, chartId = 'fiscalPrimaryChart') {
  await targetPage.locator(`#${chartId}`).scrollIntoViewIfNeeded();
  await targetPage.waitForTimeout(100);
  const point = await targetPage.evaluate(({ targetQuarter, targetChartId }) => {
    const canvas = document.getElementById(targetChartId);
    const chart = Chart.getChart(canvas);
    const index = chart.data.labels.indexOf(targetQuarter);
    if (index < 0) return null;
    const element = chart.getDatasetMeta(0).data[index];
    if (!element || element.skip) return null;
    const props = element.getProps(['x', 'y'], true);
    const rect = canvas.getBoundingClientRect();
    return { x: rect.left + props.x, y: rect.top + props.y };
  }, { targetQuarter: quarter, targetChartId: chartId });
  if (!point) return { text: '', active: false };
  await targetPage.mouse.move(point.x, point.y);
  await targetPage.waitForFunction(({ targetQuarter, targetChartId }) => {
    const chart = Chart.getChart(document.getElementById(targetChartId));
    return chart?.tooltip?.opacity > 0
      && (chart.tooltip.title || []).join(' ').includes(targetQuarter);
  }, { targetQuarter: quarter, targetChartId: chartId }, { timeout: 10000 });
  return targetPage.evaluate(targetChartId => {
    const tooltip = Chart.getChart(document.getElementById(targetChartId)).tooltip;
    const lines = [
      ...(tooltip.title || []),
      ...(tooltip.beforeBody || []),
      ...(tooltip.body || []).flatMap(item => item.lines || []),
      ...(tooltip.afterBody || []),
    ];
    return { text: lines.join(' | '), active: tooltip.opacity > 0 };
  }, chartId);
}

const browser = await launchChromium();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
const errors = [];
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
page.on('pageerror', error => errors.push(error.message));
await page.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
try {
  await page.waitForFunction(() => typeof Chart !== 'undefined'
    && !/正在加载/.test(document.getElementById('fiscalStatus')?.textContent || ''), null,
  { timeout: 30000 });
} catch (error) {
  console.error(`Fiscal page did not finish loading: ${errors.join(' | ') || error.message}`);
  throw error;
}
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
        borderDash: Array.from(dataset.borderDash || []),
        borderWidth: dataset.borderWidth,
        presentationOnly: dataset.presentationOnly === true,
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
    gap: chartState('fiscalGapChart'),
    trajectory: document.getElementById('fiscalTrajectory')?.textContent || '',
    decision: {
      label: document.getElementById('fiscalDecisionLabel')?.textContent || '',
      title: document.getElementById('fiscalDecisionTitle')?.textContent || '',
      explanation: document.getElementById('fiscalDecisionExplanation')?.textContent || '',
      metricLabel: document.getElementById('fiscalDecisionMetricLabel')?.textContent || '',
      metric: document.getElementById('fiscalDecisionMetric')?.textContent || '',
      gap: document.getElementById('fiscalDecisionGap')?.textContent || '',
      actual: document.getElementById('fiscalDecisionActual')?.textContent || '',
      required: document.getElementById('fiscalDecisionRequired')?.textContent || '',
      caveat: document.getElementById('fiscalDecisionCaveat')?.textContent || '',
      className: document.getElementById('fiscalTrajectory')?.className || '',
    },
    criterionNote: document.getElementById('fiscalPrimaryCriterionNote')?.textContent || '',
    gapTitle: document.getElementById('fiscalGapChartTitle')?.textContent || '',
    gapNote: document.getElementById('fiscalGapChartNote')?.textContent || '',
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
check('页面存在 Fiscal Gap 判决区', state.decision.label === 'Fiscal Gap 判决',
  state.decision.label);
check('最新 complete quarter 判决来自 trajectory_condition',
  state.decision.title === expectedDecision(fiscal.latest.trajectory_condition),
  `${state.decision.title}/${fiscal.latest.trajectory_condition}`);
check('fiscal_gap <= 0 显示稳定条件满足',
  fiscal.latest.trajectory_condition !== 'stabilizing_condition_met'
    || (state.decision.title === '稳定条件满足'
      && state.decision.className.includes('fiscal-decision-met')),
  JSON.stringify(state.decision));
check('负 gap 显示当前稳定缓冲及带符号 Fiscal Gap',
  fiscal.latest.trajectory_condition !== 'stabilizing_condition_met'
    || (state.decision.metricLabel === '当前稳定缓冲'
      && state.decision.metric === expectedMagnitude(fiscal.latest.fiscal_gap_pct_gdp)
      && state.decision.gap === expectedFiscalGap(fiscal.latest.fiscal_gap_pct_gdp)),
  JSON.stringify(state.decision));
check('stock-flow residual 作为非零诊断展示',
  state.decision.caveat.includes(expectedPercent(fiscal.latest.stock_flow_residual_pct_gdp, ' pp'))
    && Math.abs(fiscal.latest.stock_flow_residual_pct_gdp) > 0,
  state.decision.caveat);
check('稳定条件满足不等于实际债务率下降的 caveat 保留',
  state.decision.caveat.includes('稳定条件满足 ≠ 实际债务/GDP 当期必然下降'),
  state.decision.caveat);
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
check('初级余额图只读实际与 p* 派生字段并追加 presentation reference',
  state.primary?.datasets.map(item => item.sourceField).join(',')
    === 'primary_balance_gdp_pct,stabilizing_primary_balance_pct_gdp,presentation_zero_reference');
check('p* dataset 标记为判据线且使用独立虚线样式',
  state.primary?.datasets[1]?.label.includes('判据线')
    && state.primary.datasets[1].borderDash.length > 0
    && state.primary.datasets[1].borderWidth > state.primary.datasets[0].borderWidth,
  JSON.stringify(state.primary?.datasets[1]));
check('0% GDP 只命名为参考线而非判据线',
  state.primary?.datasets[2]?.label === '0% GDP 参考线'
    && !state.primary.datasets[2].label.includes('判据线')
    && state.primary.datasets[2].presentationOnly
    && state.primary.datasets[2].data.every(value => value === 0),
  JSON.stringify(state.primary?.datasets[2]));
check('图表副标题明确 actual >= p* 才是动态判据',
  state.criterionNote.includes('实际初级余额 ≥ p*')
    && state.criterionNote.includes('Fiscal Gap ≤ 0')
    && !state.criterionNote.includes('0% GDP 判据线'), state.criterionNote);
check('Fiscal Gap 独立图表位于说明区且标题/口径文案正确',
  state.gapTitle.includes('Fiscal Gap（% GDP）')
    && state.gapTitle.includes('稳定债务所需初级余额 p* − 实际初级余额')
    && state.gapNote.includes('Fiscal Gap = p* − 实际初级余额')
    && state.gapNote.includes('小于等于 0')
    && state.gapNote.includes('大于 0'),
  `${state.gapTitle} | ${state.gapNote}`);
check('Fiscal Gap 图只读派生 gap 并追加 0% presentation criterion',
  state.gap?.datasets.map(item => item.sourceField).join(',')
    === 'fiscal_gap_pct_gdp,presentation_fiscal_gap_zero_criterion');
check('Fiscal Gap 0% GDP 判据线命名与虚线样式正确',
  state.gap?.datasets[1]?.label === '判据线（0% GDP）'
    && state.gap.datasets[1].presentationOnly
    && state.gap.datasets[1].borderDash.length > 0
    && state.gap.datasets[1].data.every(value => value === 0),
  JSON.stringify(state.gap?.datasets[1]));
check('三张财政历史图 observation 数与派生季度数一致', state.rates?.labels.length === fiscal.quarterly.length
  && state.primary?.labels.length === fiscal.quarterly.length
  && state.gap?.labels.length === fiscal.quarterly.length
  && state.rates.datasets.every(dataset => dataset.data.length === fiscal.quarterly.length)
  && state.primary.datasets.every(dataset => dataset.data.length === fiscal.quarterly.length)
  && state.gap.datasets.every(dataset => dataset.data.length === fiscal.quarterly.length));
check('图表逐点保留派生 null/数值而不补点',
  JSON.stringify(state.rates.datasets[0].data)
    === JSON.stringify(fiscal.quarterly.map(row => row.effective_r_pct))
  && JSON.stringify(state.primary.datasets[0].data)
    === JSON.stringify(fiscal.quarterly.map(row => row.primary_balance_gdp_pct))
  && JSON.stringify(state.primary.datasets[1].data)
    === JSON.stringify(fiscal.quarterly.map(row => row.stabilizing_primary_balance_pct_gdp))
  && JSON.stringify(state.gap.labels)
    === JSON.stringify(fiscal.quarterly.map(row => row.quarter))
  && JSON.stringify(state.gap.datasets[0].data)
    === JSON.stringify(fiscal.quarterly.map(row => row.fiscal_gap_pct_gdp)));
check('前端没有 fiscal gap / p* / r / g 算术重算',
  !/latest\.(?:stabilizing_primary_balance_pct_gdp|effective_r_pct|nominal_g_pct)\s*[-+*/]/.test(source)
  && !/(?:fiscalGap|fiscal_gap_pct_gdp)\s*=(?!=)/.test(source)
  && !/stabilizing_primary_balance_pct_gdp\s*-\s*(?:latest\.)?primary_balance_gdp_pct/.test(source)
  && !/rows\.map\(row\s*=>\s*row\.stabilizing_primary_balance_pct_gdp\s*-\s*row\.primary_balance_gdp_pct\)/.test(source)
  && source.includes("['fiscalGap', 'fiscal_gap_pct_gdp', '%']"));
check('页面无 console/page errors', errors.length === 0, errors.join(' | '));
check('桌面页面无横向溢出', state.scrollWidth <= state.innerWidth + 1,
  `${state.scrollWidth}/${state.innerWidth}`);

const latestTooltip = await hoverFiscalQuarter(page, fiscal.latest.quarter);
check('最新季度真实 hover tooltip 包含 actual / p* / Fiscal Gap / 判决',
  latestTooltip.active
    && latestTooltip.text.includes(`季度：${fiscal.latest.quarter}`)
    && latestTooltip.text.includes('实际初级余额')
    && latestTooltip.text.includes('稳定债务所需初级余额 p*（判据线）')
    && latestTooltip.text.includes(`Fiscal Gap：${expectedFiscalGap(fiscal.latest.fiscal_gap_pct_gdp)}`)
    && latestTooltip.text.includes(`判决：${expectedDecision(fiscal.latest.trajectory_condition)}`),
  latestTooltip.text);

const latestGapTooltip = await hoverFiscalQuarter(page, fiscal.latest.quarter, 'fiscalGapChart');
check('Fiscal Gap 图最新季度真实 hover 显示 gap / actual / p* / 稳定判决',
  latestGapTooltip.active
    && latestGapTooltip.text.includes(`季度：${fiscal.latest.quarter}`)
    && latestGapTooltip.text.includes(`Fiscal Gap：${expectedFiscalGap(fiscal.latest.fiscal_gap_pct_gdp)}`)
    && latestGapTooltip.text.includes('实际初级余额')
    && latestGapTooltip.text.includes('稳定所需 p*')
    && latestGapTooltip.text.includes('判决：稳定条件满足'),
  latestGapTooltip.text);

const positiveRow = [...fiscal.quarterly].reverse().find(row =>
  row.fiscal_gap_pct_gdp > 0 && row.trajectory_condition === 'gap_positive');
check('真实历史包含正 Fiscal Gap 季度供判决护栏', !!positiveRow,
  positiveRow?.quarter || 'none');
if (positiveRow) {
  const positiveTooltip = await hoverFiscalQuarter(page, positiveRow.quarter);
  check('正 gap 历史季度真实 hover 显示稳定条件不满足',
    positiveTooltip.active
      && positiveTooltip.text.includes(`季度：${positiveRow.quarter}`)
      && positiveTooltip.text.includes('实际初级余额')
      && positiveTooltip.text.includes('稳定债务所需初级余额 p*（判据线）')
      && positiveTooltip.text.includes(`Fiscal Gap：${expectedFiscalGap(positiveRow.fiscal_gap_pct_gdp)}`)
      && positiveTooltip.text.includes('判决：稳定条件不满足'),
    positiveTooltip.text);
  const positiveGapTooltip = await hoverFiscalQuarter(page, positiveRow.quarter, 'fiscalGapChart');
  check('Fiscal Gap 图正 gap 历史季度真实 hover 显示需要财政调整',
    positiveGapTooltip.active
      && positiveGapTooltip.text.includes(`季度：${positiveRow.quarter}`)
      && positiveGapTooltip.text.includes(`Fiscal Gap：${expectedFiscalGap(positiveRow.fiscal_gap_pct_gdp)}`)
      && positiveGapTooltip.text.includes('判决：需要财政调整'),
    positiveGapTooltip.text);
}

// A real positive-gap historical row becomes the latest fixture. The page must
// trust its derived trajectory/gap fields rather than reconstructing either one.
if (positiveRow) {
  const positivePage = await browser.newPage({ viewport: { width: 1200, height: 800 } });
  const positivePayload = structuredClone(envelope);
  positivePayload.data.latest = structuredClone(positiveRow);
  await positivePage.route('**/data/derived/macro_fiscal_stress.json?*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(positivePayload),
  }));
  await positivePage.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await positivePage.waitForFunction(() => !/正在加载/.test(
    document.getElementById('fiscalStatus')?.textContent || ''), null, { timeout: 30000 });
  const positiveDecision = await positivePage.evaluate(() => ({
    title: document.getElementById('fiscalDecisionTitle')?.textContent || '',
    metricLabel: document.getElementById('fiscalDecisionMetricLabel')?.textContent || '',
    metric: document.getElementById('fiscalDecisionMetric')?.textContent || '',
    gap: document.getElementById('fiscalDecisionGap')?.textContent || '',
    className: document.getElementById('fiscalTrajectory')?.className || '',
  }));
  check('gap_positive 历史 fixture 显示稳定条件不满足',
    positiveDecision.title === '稳定条件不满足'
      && positiveDecision.className.includes('fiscal-decision-not-met'),
    JSON.stringify(positiveDecision));
  check('正 gap 显示当前财政调整缺口',
    positiveDecision.metricLabel === '当前财政调整缺口'
      && positiveDecision.metric === expectedMagnitude(positiveRow.fiscal_gap_pct_gdp)
      && positiveDecision.gap === expectedFiscalGap(positiveRow.fiscal_gap_pct_gdp),
    JSON.stringify(positiveDecision));
  await positivePage.close();
}

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
  decisionTitle: document.getElementById('fiscalDecisionTitle')?.textContent || '',
  decisionGap: document.getElementById('fiscalDecisionGap')?.textContent || '',
  decisionMetric: document.getElementById('fiscalDecisionMetric')?.textContent || '',
  decisionClass: document.getElementById('fiscalTrajectory')?.className || '',
  debtAlive: !!Chart.getChart(document.getElementById('debtOverviewChart')),
}));
check('null KPI 全部显示未知且不变成 0', unknownState.values.length === 9
  && unknownState.values.every(value => value === '未知')
  && !unknownState.values.some(value => /^0(?:\.00)?/.test(value)), unknownState.values.join(','));
check('unknown 显示数据不足且 Fiscal Gap 保持 --',
  unknownState.decisionTitle === '数据不足，暂不判断'
    && unknownState.decisionGap === '--'
    && unknownState.decisionMetric === '--'
    && unknownState.decisionClass.includes('fiscal-decision-unknown'),
  JSON.stringify(unknownState));
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
  fiscalGap: !!Chart.getChart(document.getElementById('fiscalGapChart')),
}));
check('rates/CPI/debt 失败不影响 fiscal', !otherFailState.fiscalError
  && otherFailState.fiscalRates && otherFailState.fiscalPrimary && otherFailState.fiscalGap,
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
check('移动端 fiscal 面板无横向溢出且三图存活',
  mobileState.scrollWidth <= mobileState.innerWidth + 1
    && mobileState.panelWidth <= mobileState.innerWidth
    && mobileState.chartCount === 3, JSON.stringify(mobileState));
await mobile.close();

await browser.close();
server.close();
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;

function documentText(value) {
  return `${value.trajectory} ${value.method} ${value.status}`;
}
