import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { launchChromium } from './_browser.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const HTML_PATH = path.join(ROOT, 'macro.html');
const DATA_PATH = path.join(ROOT, 'data', 'derived', 'official_reserve_composition.json');
const source = fs.readFileSync(HTML_PATH, 'utf8');
const envelope = JSON.parse(fs.readFileSync(DATA_PATH, 'utf8'));
const rows = envelope?.data?.observations;
const methodology = envelope?.data?.methodology;

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

check('official reserve strict envelope', envelope.schema_version === 0
  && envelope.source === 'derived_official_reserve_composition'
  && envelope.freq === 'quarterly' && envelope.date_field === 'period');
check('common quarterly coverage is exact', Array.isArray(rows) && rows.length > 0
  && envelope.coverage.count === rows.length
  && envelope.coverage.first === rows[0].period
  && envelope.coverage.last === rows.at(-1).period
  && rows.every((row, index) => index === 0 || row.period > rows[index - 1].period));
check('gold share uses common denominator', rows.every(row =>
  Math.abs(row.official_gold_share_pct
    - row.official_gold_value_usd / row.total_official_reserve_assets_usd * 100) < 1e-10));
check('UST share uses same common denominator', rows.every(row =>
  Math.abs(row.foreign_official_ust_share_pct
    - row.foreign_official_ust_value_usd / row.total_official_reserve_assets_usd * 100) < 1e-10));
check('TIC/FRED source is explicit and COFER is excluded',
  envelope.data.sources.foreign_official_ust === 'TIC/FRED FORTREASPOS99990'
  && methodology.common_denominator === 'Total Official Reserve Assets'
  && methodology.cofer_usd_share_is_not_ust_share === true);
check('quarter alignment has no fill or interpolation',
  methodology.quarterly_ust_rule === 'March/June/September/December observations only'
  && methodology.no_forward_fill === true && methodology.no_interpolation === true
  && rows.every(row => /-(03|06|09|12)-01$/.test(row.ust_source_date)));
check('new card title and four canonical labels exist',
  source.includes('全球官方储备构成：黄金 vs 外国官方机构持有美债')
  && source.includes('全球央行持有黄金占全球官方储备比例')
  && source.includes('外国官方机构持有美债占全球官方储备比例')
  && source.includes('全球央行持有黄金金额')
  && source.includes("lineDataset('外国官方机构持有美债额'"));
check('old gold-vs-debt card is no longer exposed',
  !source.includes('全球黄金总市值 vs 美债总额')
  && !source.includes("loadJson('data/derived/gold_vs_debt.json"));
check('dual-axis unit contract is explicit',
  source.includes("text: '% of Total Official Reserve Assets'")
  && source.includes("text: 'USD tn'")
  && source.includes("yAxisID: 'yAmount'"));
check('methodology note states common denominator and TIC scope',
  source.includes('不使用 COFER USD share')
  && source.includes('WGC 国别报告覆盖随 vintage 变化')
  && source.includes('外国官方机构按 TIC 定义')
  && source.includes('不前值填充、不插值'));

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.css': 'text/css; charset=utf-8',
  '.png': 'image/png', '.svg': 'image/svg+xml', '.ico': 'image/x-icon' };
const requested = [];
const server = http.createServer((req, res) => {
  const relative = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  requested.push(relative);
  const file = path.resolve(ROOT, relative);
  if (!file.startsWith(`${ROOT}${path.sep}`) || !fs.existsSync(file)
      || fs.statSync(file).isDirectory()) return res.writeHead(404).end('not found');
  res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const browser = await launchChromium();

async function openPage({ failReserve = false, mobile = false } = {}) {
  const context = await browser.newContext(mobile
    ? { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true }
    : { viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const pageErrors = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  if (failReserve) await page.route('**/data/derived/official_reserve_composition.json?*', route =>
    route.fulfill({ status: 500, contentType: 'application/json', body: '{}' }));
  await page.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => typeof Chart !== 'undefined', null, { timeout: 30000 });
  return { context, page, pageErrors };
}

try {
  const normal = await openPage();
  await normal.page.waitForFunction(() => !!Chart.getChart(
    document.getElementById('officialReserveChart')), null, { timeout: 20000 });
  const state = await normal.page.evaluate(() => {
    const chart = Chart.getChart(document.getElementById('officialReserveChart'));
    const labels = chart.data.labels;
    const sampleIndex = labels.length - 1;
    const tooltip = chart.options.plugins.tooltip.callbacks;
    const sampleDataset = chart.data.datasets[3];
    const ctx = { dataset: sampleDataset, dataIndex: sampleIndex,
      parsed: { y: sampleDataset.data[sampleIndex] }, label: labels[sampleIndex] };
    return {
      title: document.querySelector('#officialReservePanel .card-title')?.textContent,
      datasets: chart.data.datasets.map(dataset => ({ label: dataset.label,
        field: dataset.sourceField, axis: dataset.yAxisID, unit: dataset.valueUnit,
        count: dataset.data.length, source: dataset.sourceLabel })),
      axes: { left: chart.options.scales.y.title.text,
        right: chart.options.scales.yAmount.title.text },
      tooltipLabel: tooltip.label(ctx), tooltipSource: tooltip.afterLabel(ctx),
      rowCount: chart.$officialReserveRows?.length,
      status: document.getElementById('officialReserveStatus')?.textContent,
      oldText: document.body.innerText.includes('全球黄金总市值 vs 美债总额'),
    };
  });
  check('browser renders one chart with four complete datasets',
    state.datasets.length === 4 && state.rowCount === rows.length
    && state.datasets.every(dataset => dataset.count === rows.length), JSON.stringify(state.datasets));
  check('browser datasets use correct distinct axes',
    state.datasets.filter(dataset => dataset.unit === '%').every(dataset => dataset.axis === 'y')
    && state.datasets.filter(dataset => dataset.unit === 'USD tn').every(dataset => dataset.axis === 'yAmount')
    && state.axes.left === '% of Total Official Reserve Assets' && state.axes.right === 'USD tn');
  check('tooltip exposes value unit, source, period and as_of',
    /\$.*tn/.test(state.tooltipLabel) && /TIC\/FRED FORTREASPOS99990/.test(state.tooltipSource)
    && /period 2025-Q4/.test(state.tooltipSource) && /as_of 2025-12-01/.test(state.tooltipSource),
  `${state.tooltipLabel} | ${state.tooltipSource}`);
  check('new title visible and old title absent',
    state.title === '全球官方储备构成：黄金 vs 外国官方机构持有美债' && !state.oldText);
  check('normal browser page has no uncaught errors', normal.pageErrors.length === 0,
    JSON.stringify(normal.pageErrors));
  await normal.context.close();

  const failure = await openPage({ failReserve: true });
  await failure.page.waitForFunction(() => /失败/.test(
    document.getElementById('officialReserveStatus')?.textContent || ''), null, { timeout: 20000 });
  const isolated = await failure.page.evaluate(() => ({
    reserve: Boolean(Chart.getChart(document.getElementById('officialReserveChart'))),
    ust: Boolean(Chart.getChart(document.getElementById('ustChart'))),
    debt: Boolean(Chart.getChart(document.getElementById('debtOverviewChart'))),
    fiscal: Boolean(Chart.getChart(document.getElementById('fiscalRatesChart'))),
    live: window.__liveTreasuryWidgetContract?.status,
  }));
  check('reserve failure is isolated from Treasury/debt/fiscal/live',
    !isolated.reserve && isolated.ust && isolated.debt && isolated.fiscal
    && isolated.live === 'unavailable', JSON.stringify(isolated));
  check('failure browser page has no uncaught errors', failure.pageErrors.length === 0,
    JSON.stringify(failure.pageErrors));
  await failure.context.close();

  const mobile = await openPage({ mobile: true });
  await mobile.page.waitForFunction(() => !!Chart.getChart(
    document.getElementById('officialReserveChart')), null, { timeout: 20000 });
  const mobileState = await mobile.page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    panelWidth: document.getElementById('officialReservePanel')?.getBoundingClientRect().width,
  }));
  check('mobile layout has no horizontal overflow',
    mobileState.scrollWidth <= mobileState.innerWidth + 1
    && mobileState.panelWidth <= mobileState.innerWidth, JSON.stringify(mobileState));
  check('mobile browser page has no uncaught errors', mobile.pageErrors.length === 0,
    JSON.stringify(mobile.pageErrors));
  await mobile.context.close();
} catch (error) {
  check('browser verification completed', false, error.stack || error.message);
} finally {
  await browser.close();
  server.close();
}

check('page requested official reserve composition artifact',
  requested.includes('data/derived/official_reserve_composition.json'));
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
