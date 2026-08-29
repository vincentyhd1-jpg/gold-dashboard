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
const goldEnvelope = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'gold_price.json'), 'utf8'));
const goldRows = goldEnvelope.data;
const debtRows = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'treasury_debt_daily.json'), 'utf8')).data;
const debtByDate = new Map(debtRows.map(row => [row.date, row.total_bn]));
const reserveDerived = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'data', 'derived', 'official_reserve_composition.json'), 'utf8'));
const reserveRows = reserveDerived.data.observations;
const upstreamPriceSource = goldEnvelope.info.find(item =>
  typeof item === 'string' && item.startsWith('price_source='))?.slice('price_source='.length);

check('UST zoom 使用 vendored Hammer 与 zoom plugin',
  source.includes('assets/vendor/hammerjs-2.0.8/hammer.min.js')
  && source.includes('assets/vendor/chartjs-plugin-zoom-2.2.0/chartjs-plugin-zoom.min.js')
  && !source.includes('cdn.jsdelivr.net/npm/hammerjs')
  && !source.includes('cdn.jsdelivr.net/npm/chartjs-plugin-zoom'));
check('hybrid pointer contract 使用 any-pointer / any-hover',
  source.includes("matchMedia('(any-pointer: fine)')")
  && source.includes("matchMedia('(any-hover: hover)')")
  && !source.includes("matchMedia('(hover: hover) and (pointer: fine)').matches;\n  const zoomEnabled"));
check('zoom plugin registry health guard 与固定 unavailable 文案存在',
  source.includes("Chart.registry?.plugins?.get('zoom')")
  && source.includes('缩放组件加载失败，请刷新重试。'));
check('UST 双击只在 chartArea 内调用共同 resetUSTZoom',
  source.includes("getElementById('ustChart').addEventListener('dblclick'")
  && source.includes('event.sourceCapabilities?.firesTouchEvents')
  && source.includes('Chart.helpers.getRelativePosition(event, ustChart)')
  && source.includes('x < area.left || x > area.right || y < area.top || y > area.bottom')
  && source.includes('  resetUSTZoom();'));

const forbiddenSymbols = ['TVC:US02Y', 'TVC:US10Y', 'TVC:US30Y',
  'CBOT:ZT1!', 'CBOT:ZN1!', 'CBOT:ZB1!'];
check('Live Treasury unavailable card 存在且未伪装为实时收益率产品',
  source.includes('id="liveTreasuryPanel"')
  && source.includes('实时美债市场（暂不可用）')
  && source.includes('当前无法可靠提供嵌入式实时 Treasury yield')
  && !source.includes('实时国债收益率'));
check('production 不再请求任何受限 TVC / CBOT symbol',
  forbiddenSymbols.every(symbol => !source.includes(symbol))
  && !source.includes('embed-widget-advanced-chart.js'));
check('Live unavailable contract 不写 JSON 或进入财政计算',
  source.includes("status: 'unavailable'")
  && source.includes('symbols: Object.freeze([])')
  && source.includes('writesDerivedJson: false')
  && source.includes('entersFiscalCalculations: false'));
check('TradingView compatibility 与 C17 effective_r 隔离说明存在',
  source.includes('Compatibility source: TradingView Advanced Chart Widget')
  && source.includes('不是 C17 effective_r')
  && source.includes('不进入 Fiscal Gap、C18B 或 C18C 计算'));

const methodology = derived.data.methodology;
const observations = derived.data.observations;
check('gold-vs-debt strict envelope 与方法学存在',
  derived.schema_version === 0
  && derived.source === 'derived_global_gold_value_vs_us_debt'
  && derived.freq === 'weekly' && derived.date_field === 'date'
  && methodology.gold_value_is_estimate === true);
check('gold price proxy metadata 对应当前 upstream source',
  methodology.gold_price_source === upstreamPriceSource
  && methodology.gold_price_is_proxy === true
  && ['GC=F', 'XAUUSD'].includes(methodology.gold_price_instrument)
  && typeof methodology.gold_price_instrument_label === 'string');
check('GC=F 不被固定描述为 spot gold',
  methodology.gold_price_instrument !== 'GC=F'
  || !/spot/i.test(methodology.gold_price_instrument_label));
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

async function preparePage({ blockZoomPlugin = false, failReserve = false, mobile = false,
  pointerMode = mobile ? 'touch' : 'hybrid', deviceScaleFactor = 1 } = {}) {
  const context = await browser.newContext(mobile ? {
    viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true,
    deviceScaleFactor,
  } : {
    viewport: { width: 1440, height: 1000 },
    hasTouch: pointerMode === 'touch', deviceScaleFactor,
  });
  const page = await context.newPage();
  const pageErrors = [];
  const externalRequests = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  page.on('request', request => {
    const url = request.url();
    if (!url.startsWith(base) && /tradingview/i.test(url)) externalRequests.push(url);
  });
  await page.addInitScript(mode => {
    const original = window.matchMedia.bind(window);
    window.matchMedia = query => {
      const overrides = {
        '(pointer: fine)': false,
        '(hover: hover)': false,
        '(any-pointer: fine)': mode === 'hybrid',
        '(any-hover: hover)': mode === 'hybrid',
      };
      if (!(query in overrides)) return original(query);
      const matches = overrides[query];
      return {
        matches, media: query, onchange: null,
        addListener() {}, removeListener() {}, addEventListener() {},
        removeEventListener() {}, dispatchEvent() { return true; },
      };
    };
  }, pointerMode);
  if (blockZoomPlugin) {
    await page.route('**/assets/vendor/chartjs-plugin-zoom-2.2.0/chartjs-plugin-zoom.min.js',
      route => route.fulfill({ status: 404, contentType: 'text/plain', body: 'blocked by guard' }));
  }
  if (failReserve) {
    await page.route('**/data/derived/official_reserve_composition.json?*', route => route.fulfill({
      status: 500, contentType: 'application/json', body: '{}',
    }));
  }
  await page.goto(`${base}/macro.html`, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(() => typeof Chart !== 'undefined',
  null, { timeout: 30000 });
  await page.waitForFunction(() => !!Chart.getChart(document.getElementById('ustChart')),
  null, { timeout: 15000 });
  return { context, page, pageErrors, externalRequests };
}

async function chartAreaPoint(page, xFraction, yFraction) {
  return page.evaluate(({ xFraction, yFraction }) => {
    const canvas = document.getElementById('ustChart');
    const chart = Chart.getChart(canvas);
    const rect = canvas.getBoundingClientRect();
    const area = chart.chartArea;
    const scaleX = rect.width / chart.width;
    const scaleY = rect.height / chart.height;
    return {
      x: rect.left + (area.left + (area.right - area.left) * xFraction) * scaleX,
      y: rect.top + (area.top + (area.bottom - area.top) * yFraction) * scaleY,
    };
  }, { xFraction, yFraction });
}

async function canvasPaddingPoint(page) {
  return page.evaluate(() => {
    const canvas = document.getElementById('ustChart');
    const chart = Chart.getChart(canvas);
    const rect = canvas.getBoundingClientRect();
    const area = chart.chartArea;
    const scaleX = rect.width / chart.width;
    const scaleY = rect.height / chart.height;
    return {
      x: rect.left + Math.max(2, area.left * 0.45) * scaleX,
      y: rect.top + ((area.top + area.bottom) / 2) * scaleY,
    };
  });
}

async function dragUST(page) {
  const start = await chartAreaPoint(page, 0.18, 0.22);
  const end = await chartAreaPoint(page, 0.66, 0.78);
  await page.mouse.move(start.x, start.y);
  await page.mouse.down({ button: 'left' });
  await page.mouse.move(end.x, end.y, { steps: 12 });
  await page.mouse.up({ button: 'left' });
  await page.waitForTimeout(200);
}

async function ustState(page) {
  return page.evaluate(() => {
    const chart = Chart.getChart(document.getElementById('ustChart'));
    const left = Math.max(0, Math.ceil(Number.isFinite(chart.scales.x.min)
      ? chart.scales.x.min : 0));
    const right = Math.min(chart.data.labels.length - 1,
      Math.floor(Number.isFinite(chart.scales.x.max)
        ? chart.scales.x.max : chart.data.labels.length - 1));
    const values = [];
    chart.data.datasets.forEach((dataset, datasetIndex) => {
      if (!chart.isDatasetVisible(datasetIndex)) return;
      for (let index = left; index <= right; index++) {
        const value = dataset.data[index];
        if (Number.isFinite(value)) values.push(Number(value));
      }
    });
    const low = values.length ? Math.min(...values) : null;
    const high = values.length ? Math.max(...values) : null;
    const padding = values.length ? Math.max((high - low) * 0.08, 0.05) : null;
    return {
      xMin: chart.scales.x.min, xMax: chart.scales.x.max,
      yMin: chart.scales.y.min, yMax: chart.scales.y.max,
      expectedMin: low === null ? null : low - padding,
      expectedMax: high === null ? null : high + padding,
      zoomed: typeof chart.isZoomedOrPanned === 'function'
        ? chart.isZoomedOrPanned() : false,
      resetDisabled: document.getElementById('ustResetZoom').disabled,
      dragEnabled: chart.options.plugins.zoom?.zoom?.drag?.enabled === true,
      hint: document.getElementById('ustZoomHint').textContent,
      health: window.__ustZoomHealth,
      dblclickEventCount: window.__c18c2DblclickEventCount ?? null,
      dpr: window.devicePixelRatio,
    };
  });
}

function sameAxisState(a, b) {
  return a.xMin === b.xMin && a.xMax === b.xMax
    && Math.abs(a.yMin - b.yMin) < 1e-9
    && Math.abs(a.yMax - b.yMax) < 1e-9;
}

try {
  const normal = await preparePage();
  const { page } = normal;
  await page.waitForFunction(() => !!Chart.getChart(document.getElementById('officialReserveChart')),
  null, { timeout: 15000 });
  const initial = await page.evaluate(() => {
    const reserve = Chart.getChart(document.getElementById('officialReserveChart'));
    return {
      reserveDatasets: reserve.data.datasets.map(dataset => ({
        label: dataset.label, field: dataset.sourceField,
        axis: dataset.yAxisID, count: dataset.data.length,
      })),
      live: window.__liveTreasuryWidgetContract,
      liveIframe: Boolean(document.querySelector('#liveTreasuryWidget iframe')),
      liveFallback: document.getElementById('liveTreasuryFallback')?.textContent,
      livePanelContent: document.getElementById('liveTreasuryPanel')?.innerHTML || '',
      reserveMethod: document.getElementById('officialReserveMethod')?.textContent,
    };
  });
  const initialUST = await ustState(page);
  check('C18C.3B 页面替换位有四条季度 official reserve dataset',
    initial.reserveDatasets.length === 4
    && initial.reserveDatasets.every(row => row.count === reserveRows.length)
    && initial.reserveDatasets.filter(row => row.axis === 'y').length === 2
    && initial.reserveDatasets.filter(row => row.axis === 'yAmount').length === 2,
  JSON.stringify(initial.reserveDatasets));
  check('Live card 为 unavailable，不创建 iframe 或请求受限 symbol',
    initial.live?.status === 'unavailable'
    && Array.isArray(initial.live?.symbols) && initial.live.symbols.length === 0
    && initial.live?.writesDerivedJson === false
    && initial.live?.entersFiscalCalculations === false
    && !initial.liveIframe && normal.externalRequests.length === 0,
  JSON.stringify({ live: initial.live, requests: normal.externalRequests }));
  check('Live unavailable fallback 不将 FRED 冒充 intraday',
    /licensed market-data API/.test(initial.liveFallback || '')
    && /FRED 日频数据伪装成 intraday/.test(initial.liveFallback || ''),
  initial.liveFallback);
  check('unavailable Live Treasury card rendered third-party market content',
    !initial.liveIframe
    && !/(?:Apple Inc|Cboe One|TVC:|CBOT:)/i.test(initial.livePanelContent),
  initial.livePanelContent);
  check('页面公开共同分母、TIC scope 与 no-fill 方法学',
    initial.reserveMethod?.includes('Total Official Reserve Assets')
    && initial.reserveMethod.includes('不使用 COFER USD share')
    && initial.reserveMethod.includes('外国官方机构按 TIC 定义')
    && initial.reserveMethod.includes('不前值填充、不插值'), initial.reserveMethod);
  check('hybrid pointer 下 UST drag zoom 真正启用',
    initialUST.health?.pluginAvailable === true
    && initialUST.health?.mouseCapable === true
    && initialUST.dragEnabled === true
    && /按住鼠标左键/.test(initialUST.hint), JSON.stringify(initialUST));

  const tooltipPoint = await chartAreaPoint(page, 0.5, 0.5);
  await page.mouse.move(tooltipPoint.x, tooltipPoint.y);
  await page.waitForTimeout(100);
  const tooltipEnabled = await page.evaluate(() => {
    const chart = Chart.getChart(document.getElementById('ustChart'));
    return chart.options.plugins.tooltip.enabled !== false;
  });
  check('UST tooltip 保持启用', tooltipEnabled);

  const legendToggle = await page.evaluate(() => {
    const chart = Chart.getChart(document.getElementById('ustChart'));
    chart.setDatasetVisibility(0, false);
    chart.update('none');
    const hidden = !chart.isDatasetVisible(0);
    chart.setDatasetVisibility(0, true);
    chart.update('none');
    return hidden && chart.isDatasetVisible(0);
  });
  check('UST legend dataset visibility toggle 仍可用', legendToggle);

  await dragUST(page);
  const zoomedForLegend = await ustState(page);
  const legendPoint = await page.evaluate(() => {
    const canvas = document.getElementById('ustChart');
    const chart = Chart.getChart(canvas);
    const rect = canvas.getBoundingClientRect();
    const box = chart.legend.legendHitBoxes[0];
    const scaleX = rect.width / chart.width;
    const scaleY = rect.height / chart.height;
    return {
      x: rect.left + (box.left + box.width / 2) * scaleX,
      y: rect.top + (box.top + box.height / 2) * scaleY,
    };
  });
  await page.mouse.dblclick(legendPoint.x, legendPoint.y);
  await page.waitForTimeout(100);
  const afterLegendDblclick = await ustState(page);
  check('双击 legend / 绘图区外不触发 UST reset',
    zoomedForLegend.zoomed && afterLegendDblclick.zoomed,
  JSON.stringify({ before: zoomedForLegend, after: afterLegendDblclick }));

  const paddingPoint = await canvasPaddingPoint(page);
  await page.mouse.dblclick(paddingPoint.x, paddingPoint.y);
  await page.waitForTimeout(100);
  const afterPaddingDblclick = await ustState(page);
  check('双击 canvas padding / chartArea 外不触发 UST reset',
    afterLegendDblclick.zoomed && afterPaddingDblclick.zoomed,
  JSON.stringify({ before: afterLegendDblclick, after: afterPaddingDblclick }));

  const inside = await chartAreaPoint(page, 0.5, 0.5);
  await page.evaluate(() => {
    window.__c18c2DblclickEventCount = 0;
    document.getElementById('ustChart').addEventListener('dblclick', () => {
      window.__c18c2DblclickEventCount += 1;
    });
  });
  await page.mouse.dblclick(inside.x, inside.y);
  await page.waitForTimeout(100);
  const dblclickReset = await ustState(page);
  check('绘图区双击恢复完整 UST X/Y 与按钮状态',
    zoomedForLegend.zoomed && !dblclickReset.zoomed && dblclickReset.resetDisabled
    && sameAxisState(initialUST, dblclickReset),
  JSON.stringify({ initial: initialUST, reset: dblclickReset }));
  check('真实 mouse.dblclick 只触发一个 canvas dblclick event',
    dblclickReset.dblclickEventCount === 1, JSON.stringify(dblclickReset));

  await page.mouse.dblclick(inside.x, inside.y);
  const noOpReset = await ustState(page);
  check('未缩放状态双击是安全 no-op',
    !noOpReset.zoomed && noOpReset.resetDisabled && sameAxisState(initialUST, noOpReset),
  JSON.stringify(noOpReset));

  await dragUST(page);
  const zoomed = await ustState(page);
  check('基于 chartArea 的真实左键拖框缩小 UST X 轴',
    zoomed.zoomed && zoomed.xMax - zoomed.xMin < initialUST.xMax - initialUST.xMin
    && !zoomed.resetDisabled, JSON.stringify({ initial: initialUST, zoomed }));
  check('UST Y 轴按选区内启用 dataset 真实值自动重算',
    Math.abs(zoomed.yMin - zoomed.expectedMin) < 1e-9
    && Math.abs(zoomed.yMax - zoomed.expectedMax) < 1e-9,
  JSON.stringify(zoomed));
  await page.click('#ustResetZoom');
  const buttonReset = await ustState(page);
  check('UST Reset Zoom 按钮与双击恢复结果完全一致',
    !buttonReset.zoomed && buttonReset.resetDisabled
    && sameAxisState(initialUST, buttonReset)
    && sameAxisState(dblclickReset, buttonReset),
  JSON.stringify({ dblclickReset, buttonReset }));
  check('正常页面无未捕获 pageerror', normal.pageErrors.length === 0,
    JSON.stringify(normal.pageErrors));
  await normal.context.close();

  const highDpi = await preparePage({ deviceScaleFactor: 2 });
  const highDpiInitial = await ustState(highDpi.page);
  await highDpi.page.evaluate(() => {
    window.__c18c2DblclickEventCount = 0;
    document.getElementById('ustChart').addEventListener('dblclick', () => {
      window.__c18c2DblclickEventCount += 1;
    });
  });
  await dragUST(highDpi.page);
  const highDpiZoomed = await ustState(highDpi.page);
  const highDpiInside = await chartAreaPoint(highDpi.page, 0.5, 0.5);
  await highDpi.page.mouse.dblclick(highDpiInside.x, highDpiInside.y);
  await highDpi.page.waitForTimeout(100);
  const highDpiDblclickReset = await ustState(highDpi.page);
  await dragUST(highDpi.page);
  await highDpi.page.click('#ustResetZoom');
  const highDpiButtonReset = await ustState(highDpi.page);
  check('DPR > 1 下真实 drag 与 dblclick 使用归一化 chart 坐标',
    highDpiInitial.dpr === 2 && highDpiZoomed.zoomed
    && highDpiDblclickReset.dblclickEventCount === 1
    && !highDpiDblclickReset.zoomed && highDpiDblclickReset.resetDisabled
    && sameAxisState(highDpiInitial, highDpiDblclickReset),
  JSON.stringify({ highDpiInitial, highDpiZoomed, highDpiDblclickReset }));
  check('DPR > 1 下 button reset 与 dblclick reset 最终轴状态一致',
    !highDpiButtonReset.zoomed && highDpiButtonReset.resetDisabled
    && sameAxisState(highDpiInitial, highDpiButtonReset)
    && sameAxisState(highDpiDblclickReset, highDpiButtonReset),
  JSON.stringify({ highDpiDblclickReset, highDpiButtonReset }));
  check('DPR > 1 页面无未捕获 pageerror', highDpi.pageErrors.length === 0,
    JSON.stringify(highDpi.pageErrors));
  await highDpi.context.close();

  const touchOnly = await preparePage({ pointerMode: 'touch' });
  const touchInitial = await ustState(touchOnly.page);
  await dragUST(touchOnly.page);
  const touchAfterDrag = await ustState(touchOnly.page);
  const touchPoint = await chartAreaPoint(touchOnly.page, 0.5, 0.5);
  await touchOnly.page.touchscreen.tap(touchPoint.x, touchPoint.y);
  await touchOnly.page.waitForTimeout(60);
  await touchOnly.page.touchscreen.tap(touchPoint.x, touchPoint.y);
  await touchOnly.page.waitForTimeout(100);
  const touchAfterDblclick = await ustState(touchOnly.page);
  check('touch-only fixture 禁用 drag 且真实 double-tap 不触发 reset',
    touchInitial.health?.mouseCapable === false && !touchInitial.dragEnabled
    && !touchAfterDrag.zoomed && sameAxisState(touchInitial, touchAfterDrag)
    && sameAxisState(touchInitial, touchAfterDblclick),
  JSON.stringify({ touchInitial, touchAfterDrag, touchAfterDblclick }));
  check('touch-only 页面无未捕获 pageerror', touchOnly.pageErrors.length === 0,
    JSON.stringify(touchOnly.pageErrors));
  await touchOnly.context.close();

  const pluginMissing = await preparePage({ blockZoomPlugin: true });
  await pluginMissing.page.waitForFunction(() =>
    !!Chart.getChart(document.getElementById('officialReserveChart'))
    && !!Chart.getChart(document.getElementById('fedChart'))
    && !!Chart.getChart(document.getElementById('cpiChart'))
    && !!Chart.getChart(document.getElementById('debtOverviewChart')),
  null, { timeout: 15000 });
  const missing = await pluginMissing.page.evaluate(() => ({
    zoomRegistered: Boolean(Chart.registry.plugins.get('zoom')),
    health: window.__ustZoomHealth,
    hint: document.getElementById('ustZoomHint')?.textContent,
    disabled: document.getElementById('ustResetZoom')?.disabled,
    charts: ['ustChart', 'fedChart', 'cpiChart', 'debtOverviewChart', 'officialReserveChart']
      .map(id => Boolean(Chart.getChart(document.getElementById(id)))),
  }));
  check('zoom plugin missing 显示固定局部错误并禁用 Reset',
    !missing.zoomRegistered && missing.health?.pluginAvailable === false
    && missing.disabled && missing.hint === '缩放组件加载失败，请刷新重试。',
  JSON.stringify(missing));
  check('zoom plugin missing 不影响其它宏观图', missing.charts.every(Boolean),
    JSON.stringify(missing.charts));
  check('zoom plugin missing 无未捕获 pageerror', pluginMissing.pageErrors.length === 0,
    JSON.stringify(pluginMissing.pageErrors));
  await pluginMissing.context.close();

  const reserveFailure = await preparePage({ failReserve: true });
  await reserveFailure.page.waitForFunction(() =>
    /失败/.test(document.getElementById('officialReserveStatus')?.textContent || ''),
  null, { timeout: 15000 });
  const reserveIsolated = await reserveFailure.page.evaluate(() => ({
    reserve: Boolean(Chart.getChart(document.getElementById('officialReserveChart'))),
    ust: Boolean(Chart.getChart(document.getElementById('ustChart'))),
    liveUnavailable: window.__liveTreasuryWidgetContract?.status === 'unavailable',
    status: document.getElementById('officialReserveStatus')?.textContent,
  }));
  check('official reserve 加载失败只挂替换卡', !reserveIsolated.reserve
    && reserveIsolated.ust && reserveIsolated.liveUnavailable
    && /失败/.test(reserveIsolated.status), JSON.stringify(reserveIsolated));
  check('official reserve 加载失败无未捕获 pageerror',
    reserveFailure.pageErrors.length === 0, JSON.stringify(reserveFailure.pageErrors));
  await reserveFailure.context.close();

  const mobile = await preparePage({ mobile: true });
  await mobile.page.waitForFunction(() => !!Chart.getChart(
    document.getElementById('officialReserveChart')), null, { timeout: 15000 });
  const mobileState = await mobile.page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    ustDrag: Chart.getChart(document.getElementById('ustChart'))
      ?.options.plugins.zoom?.zoom?.drag?.enabled,
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

check('页面请求 official reserve composition 派生产物',
  requested.includes('data/derived/official_reserve_composition.json'));
console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
