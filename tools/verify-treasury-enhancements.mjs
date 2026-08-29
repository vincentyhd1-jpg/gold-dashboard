import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChromium } from './_browser.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.png': 'image/png', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
};
const requested = [];
const server = http.createServer((req, res) => {
  const relative = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  requested.push(relative);
  const file = path.resolve(ROOT, relative);
  if (!file.startsWith(`${ROOT}${path.sep}`) || !fs.existsSync(file)
      || fs.statSync(file).isDirectory()) {
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

const source = fs.readFileSync(path.join(ROOT, 'macro.html'), 'utf8');
const derived = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'gold_vs_debt.json'), 'utf8'));
const goldRows = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'gold_price.json'), 'utf8')).data;
const debtRows = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'treasury_debt_daily.json'), 'utf8')).data;
const debtByDate = new Map(debtRows.map(row => [row.date, row.total_bn]));

check('Live Treasury card 与官方 Advanced widget source 存在',
  source.includes('id="liveTreasuryPanel"')
  && source.includes('embed-widget-advanced-chart.js'));
check('默认 TradingView symbol 为 TVC:US10Y',
  source.includes("liveTreasuryState = { symbol: 'TVC:US10Y'"));
check('2Y / 10Y / 30Y symbol contract 锁定',
  ['TVC:US02Y', 'TVC:US10Y', 'TVC:US30Y'].every(symbol =>
    source.includes(`data-live-symbol="${symbol}"`)
    && source.includes(`'${symbol}'`)));
check('分时 / 1D / 5D 是分钟 interval + 真实 range contract',
  source.includes("intraday: { label: '分时', interval: '5', range: '1D' }")
  && source.includes("oneDay: { label: '1D', interval: '15', range: '1D' }")
  && source.includes("fiveDay: { label: '5D', interval: '60', range: '5D' }"));
check('TradingView attribution 与 C17 effective_r 隔离文案存在',
  source.includes('Data / chart source: TradingView')
  && source.includes('不是 C17 中美国政府存量债务的 effective_r')
  && source.includes('不进入 C17/C18C 债务动力学或 Fiscal Gap 计算'));
check('TradingView 数据不写 derived JSON 或财政模型',
  source.includes('writesDerivedJson: false')
  && source.includes('entersFiscalCalculations: false')
  && !/fetch\([^)]*tradingview[^)]*\).*atomic|localStorage\.setItem/i.test(source));
check('Live fallback 文案与错误边界存在',
  source.includes('实时市场图暂不可用，请稍后重试。')
  && source.includes('script.onerror'));

const methodology = derived.data.methodology;
const observations = derived.data.observations;
check('gold-vs-debt strict envelope 与方法学存在',
  derived.schema_version === 0
  && derived.source === 'derived_global_gold_value_vs_us_debt'
  && derived.freq === 'weekly' && derived.date_field === 'date'
  && methodology.gold_value_is_estimate === true);
check('gold-vs-debt 一点对应一个真实周频 gold observation',
  observations.length === goldRows.length
  && observations.every((row, index) => row.date === goldRows[index].date
    && Object.is(row.gold_price_usd_oz, goldRows[index].price)));
check('gold value 公式使用固定存量与 oz 换算', observations.every(row => {
  const expected = row.gold_price_usd_oz === null ? null
    : methodology.gold_stock_tonnes
      * methodology.troy_oz_per_metric_tonne * row.gold_price_usd_oz / 1e12;
  return expected === null ? row.global_gold_value_usd_tn === null
    : Math.abs(row.global_gold_value_usd_tn - expected) <= 1e-12;
}));
check('美债明确是 Total Public Debt Outstanding 且只做 exact-date',
  methodology.debt_definition === 'Total Public Debt Outstanding'
  && methodology.debt_alignment === 'exact_gold_observation_date_only'
  && methodology.no_forward_fill === true && methodology.no_interpolation === true
  && observations.every(row => Object.is(
    row.us_total_public_debt_usd_tn,
    debtByDate.has(row.date) ? debtByDate.get(row.date) / 1000 : null)));

const browser = await launchChromium();
const MOCK_WIDGET = `(() => {
  const script = document.currentScript;
  const mount = script?.parentElement?.querySelector('.tradingview-widget-container__widget');
  if (mount) {
    const frame = document.createElement('iframe');
    frame.title = 'Mock TradingView Advanced Real-Time Chart';
    frame.src = 'about:blank';
    mount.append(frame);
  }
})();`;

async function preparePage({ failWidget = false, failGold = false, mobile = false } = {}) {
  const context = await browser.newContext(mobile ? {
    viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
  } : { viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.route('https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js',
    route => failWidget ? route.abort('failed') : route.fulfill({
      status: 200, contentType: 'text/javascript', body: MOCK_WIDGET,
    }));
  if (failGold) {
    await page.route('**/data/derived/gold_vs_debt.json?*', route => route.fulfill({
      status: 500, contentType: 'application/json', body: '{}',
    }));
  }
  await page.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => typeof Chart !== 'undefined'
    && !!Chart.registry.plugins.get('zoom')
    && !/正在加载/.test(document.getElementById('status')?.textContent || ''),
  null, { timeout: 30000 });
  return { context, page, pageErrors };
}

try {
  const normal = await preparePage();
  const { page } = normal;
  await page.waitForFunction(() => !!Chart.getChart(document.getElementById('goldDebtChart'))
    && !!Chart.getChart(document.getElementById('ustChart'))
    && !!document.querySelector('#liveTreasuryWidget iframe'), null, { timeout: 15000 });

  const initial = await page.evaluate(() => {
    const gold = Chart.getChart(document.getElementById('goldDebtChart'));
    const ust = Chart.getChart(document.getElementById('ustChart'));
    return {
      goldDatasets: gold.data.datasets.map(dataset => ({
        label: dataset.label, field: dataset.sourceField,
        frequency: dataset.sourceFrequency, count: dataset.data.length,
      })),
      ust: {
        xMin: ust.scales.x.min, xMax: ust.scales.x.max,
        yMin: ust.scales.y.min, yMax: ust.scales.y.max,
        drag: ust.options.plugins.zoom.zoom.drag.enabled,
      },
      live: window.__liveTreasuryWidgetContract,
      attribution: document.querySelector('.tradingview-widget-copyright')?.textContent,
      status: document.getElementById('liveTreasuryStatus')?.textContent,
    };
  });
  check('gold-vs-debt 页面有两条真实 source dataset',
    initial.goldDatasets.length === 2
    && initial.goldDatasets.some(row => row.field === 'global_gold_value_usd_tn'
      && row.frequency === 'weekly' && row.count === observations.filter(
        item => item.global_gold_value_usd_tn !== null).length)
    && initial.goldDatasets.some(row => row.field === 'us_total_public_debt_usd_tn'
      && row.frequency === 'exact_date_daily_observation'
      && row.count === observations.filter(
        row => row.us_total_public_debt_usd_tn !== null).length),
  JSON.stringify(initial.goldDatasets));
  check('Live widget 默认用 US10Y 分时分钟级配置',
    initial.live?.symbol === 'TVC:US10Y' && initial.live?.interval === '5'
    && initial.live?.range === '1D' && initial.live?.writesDerivedJson === false
    && initial.live?.entersFiscalCalculations === false,
  JSON.stringify(initial.live));
  check('官方 TradingView attribution 保留',
    /by TradingView/.test(initial.attribution || ''));

  const canvas = page.locator('#ustChart');
  const box = await canvas.boundingBox();
  await page.mouse.move(box.x + box.width * 0.25, box.y + box.height * 0.25);
  await page.mouse.down({ button: 'left' });
  await page.mouse.move(box.x + box.width * 0.62, box.y + box.height * 0.8,
    { steps: 10 });
  await page.mouse.up({ button: 'left' });
  await page.waitForTimeout(250);
  const zoomed = await page.evaluate(() => {
    const chart = Chart.getChart(document.getElementById('ustChart'));
    const left = Math.max(0, Math.ceil(chart.scales.x.min));
    const right = Math.min(chart.data.labels.length - 1, Math.floor(chart.scales.x.max));
    const values = chart.data.datasets.flatMap(dataset => dataset.data
      .slice(left, right + 1)).filter(Number.isFinite).map(Number);
    const low = Math.min(...values);
    const high = Math.max(...values);
    const padding = Math.max((high - low) * 0.08, 0.05);
    return {
      xMin: chart.scales.x.min, xMax: chart.scales.x.max,
      yMin: chart.scales.y.min, yMax: chart.scales.y.max,
      expectedMin: low - padding, expectedMax: high + padding,
      zoomed: chart.isZoomedOrPanned(), resetDisabled:
        document.getElementById('ustResetZoom').disabled,
    };
  });
  check('真实左键拖框使 UST X 轴范围变小', zoomed.zoomed
    && zoomed.xMax - zoomed.xMin < initial.ust.xMax - initial.ust.xMin,
  JSON.stringify({ initial: initial.ust, zoomed }));
  check('UST Y 轴按选区内真实数据自动重算',
    Math.abs(zoomed.yMin - zoomed.expectedMin) < 1e-9
    && Math.abs(zoomed.yMax - zoomed.expectedMax) < 1e-9
    && !zoomed.resetDisabled, JSON.stringify(zoomed));
  await page.click('#ustResetZoom');
  const reset = await page.evaluate(() => {
    const chart = Chart.getChart(document.getElementById('ustChart'));
    return { xMin: chart.scales.x.min, xMax: chart.scales.x.max,
      yMin: chart.scales.y.min, yMax: chart.scales.y.max,
      zoomed: chart.isZoomedOrPanned(), disabled:
        document.getElementById('ustResetZoom').disabled };
  });
  check('UST Reset Zoom 恢复完整 X/Y 范围', !reset.zoomed && reset.disabled
    && reset.xMin === initial.ust.xMin && reset.xMax === initial.ust.xMax
    && Math.abs(reset.yMin - initial.ust.yMin) < 1e-9
    && Math.abs(reset.yMax - initial.ust.yMax) < 1e-9,
  JSON.stringify({ initial: initial.ust, reset }));

  for (const [selector, symbol] of [
    ['[data-live-symbol="TVC:US02Y"]', 'TVC:US02Y'],
    ['[data-live-symbol="TVC:US30Y"]', 'TVC:US30Y'],
  ]) {
    await page.click(selector);
    await page.waitForFunction(expected =>
      window.__liveTreasuryWidgetContract?.symbol === expected, symbol);
    const actual = await page.evaluate(() => window.__liveTreasuryWidgetContract?.symbol);
    check(`${symbol} tenor 切换更新真实 widget symbol`, actual === symbol, actual);
  }
  for (const [view, expected] of [
    ['oneDay', { interval: '15', range: '1D' }],
    ['fiveDay', { interval: '60', range: '5D' }],
    ['intraday', { interval: '5', range: '1D' }],
  ]) {
    await page.click(`[data-live-view="${view}"]`);
    await page.waitForFunction(value =>
      window.__liveTreasuryWidgetContract?.interval === value.interval
      && window.__liveTreasuryWidgetContract?.range === value.range, expected);
    const actual = await page.evaluate(() => window.__liveTreasuryWidgetContract);
    check(`${view} 切换改变分钟 interval / 时间范围`,
      actual.interval === expected.interval && actual.range === expected.range,
    JSON.stringify(actual));
  }
  check('正常页面无未捕获 pageerror', normal.pageErrors.length === 0,
    JSON.stringify(normal.pageErrors));
  await normal.context.close();

  const widgetFailure = await preparePage({ failWidget: true });
  await widgetFailure.page.waitForFunction(() =>
    !document.getElementById('liveTreasuryFallback')?.hidden, null, { timeout: 15000 });
  const isolated = await widgetFailure.page.evaluate(() => ({
    fallback: document.getElementById('liveTreasuryFallback')?.textContent,
    gold: !!Chart.getChart(document.getElementById('goldDebtChart')),
    ust: !!Chart.getChart(document.getElementById('ustChart')),
    fiscal: !!Chart.getChart(document.getElementById('fiscalRatesChart')),
  }));
  check('TradingView CDN 失败显示明确 fallback',
    /暂不可用/.test(isolated.fallback || ''));
  check('TradingView CDN 失败不拖垮 gold / UST / C17',
    isolated.gold && isolated.ust && isolated.fiscal, JSON.stringify(isolated));
  check('TradingView CDN 失败无未捕获 pageerror',
    widgetFailure.pageErrors.length === 0, JSON.stringify(widgetFailure.pageErrors));
  await widgetFailure.context.close();

  const goldFailure = await preparePage({ failGold: true });
  await goldFailure.page.waitForFunction(() =>
    /失败/.test(document.getElementById('goldDebtStatus')?.textContent || '')
    && !!document.querySelector('#liveTreasuryWidget iframe'),
  null, { timeout: 15000 });
  const goldIsolated = await goldFailure.page.evaluate(() => ({
    gold: !!Chart.getChart(document.getElementById('goldDebtChart')),
    ust: !!Chart.getChart(document.getElementById('ustChart')),
    live: !!document.querySelector('#liveTreasuryWidget iframe'),
    status: document.getElementById('goldDebtStatus')?.textContent,
  }));
  check('gold-vs-debt 加载失败只挂比较卡', !goldIsolated.gold
    && goldIsolated.ust && goldIsolated.live && /失败/.test(goldIsolated.status),
  JSON.stringify(goldIsolated));
  await goldFailure.context.close();

  const mobile = await preparePage({ mobile: true });
  await mobile.page.waitForFunction(() => !!Chart.getChart(
    document.getElementById('goldDebtChart')) && !!document.querySelector(
      '#liveTreasuryWidget iframe'), null, { timeout: 15000 });
  const mobileState = await mobile.page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    ustDrag: Chart.getChart(document.getElementById('ustChart'))
      ?.options.plugins.zoom.zoom.drag.enabled,
    liveWidth: document.getElementById('liveTreasuryPanel')?.getBoundingClientRect().width,
  }));
  check('移动端无横向溢出且禁用历史 UST drag zoom',
    mobileState.scrollWidth <= mobileState.innerWidth + 1
    && mobileState.ustDrag === false && mobileState.liveWidth <= mobileState.innerWidth,
  JSON.stringify(mobileState));
  check('移动端无未捕获 pageerror', mobile.pageErrors.length === 0,
    JSON.stringify(mobile.pageErrors));
  await mobile.context.close();
} catch (error) {
  check('页面验证完成', false, error.stack || error.message);
} finally {
  await browser.close();
  server.close();
}

check('页面请求 gold-vs-debt 派生产物',
  requested.includes('data/derived/gold_vs_debt.json'));
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
