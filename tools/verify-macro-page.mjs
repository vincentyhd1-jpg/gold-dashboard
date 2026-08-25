// macro.html 护栏：rates/CPI 图形态、债务单图双轴与六条 dataset 数据契约、
// CPI 右端不被补齐、首屏不贴底、发布日文案不冒充日期。
//
// 与其他前端 verify 的两处差异，都是刻意的：
//   1. 自带静态服务：其余脚本依赖外部已起的 localhost:3001。本脚本用 node 内置
//      http 起在随机空端口上，跑之前不需要先开 server，也不依赖 python。
//   2. Chromium 统一经 tools/_browser.mjs 启动，不绑定本机缓存路径或 revision。
//
// 首屏那条断言是把一次性人工实测固化下来的：Chart.js 首帧曾出现全部点贴在
// chartArea.bottom 上（需要 setTimeout(update('none'),0) 兜），现在实测不贴底，
// 于是用断言钉住 —— 若哪天回归贴底，这条会红，而不是靠人再测一次。
import http from 'http';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { launchChromium } from './_browser.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.png': 'image/png',
};

const requestedPaths = [];
const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  requestedPaths.push(rel);
  const file = path.join(ROOT, rel);
  // 只服务仓库内文件：拼出仓库外的路径直接 404，不给 .. 逃逸
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404).end('not found');
    return;
  }
  res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = 'http://127.0.0.1:' + server.address().port;

let pass = 0;
let fail = 0;
const check = (name, ok, detail = '') => {
  if (ok) {
    pass++;
    console.log('PASS ' + name + (detail ? '  ' + detail : ''));
  } else {
    fail++;
    console.log('FAIL ' + name + '  ' + detail);
  }
};

// 期望值取自派生文件，不写死月份：CPI 参考期每月前移，写死会在下次采集后过期，
// 变成「断言红了但代码没错」。文案里的固定部分（发布日未保存）才写死。
const rates = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'derived', 'macro_rates.json'), 'utf-8'));
const cpiFile = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'derived', 'macro_cpi.json'), 'utf-8'));
const debtFile = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'derived', 'macro_debt.json'), 'utf-8'));
const dailyDebtFile = JSON.parse(fs.readFileSync(path.join(ROOT, 'data', 'treasury_debt_daily.json'), 'utf-8'));
const rawDebtFiles = Object.fromEntries([
  ['total', 'data/debt_total.json'],
  ['public', 'data/debt_held_public.json'],
  ['intragov', 'data/debt_intragov.json'],
  ['foreign', 'data/debt_foreign.json'],
  ['gdp', 'data/gdp_nominal.json'],
].map(([name, rel]) => [name,
  JSON.parse(fs.readFileSync(path.join(ROOT, rel), 'utf-8')).data]));
const cpiRows = cpiFile.data.cpi;
const debtRows = debtFile.data.debt;
const debtMeta = debtFile.data.meta;
const dailyDebtRows = dailyDebtFile.data;
const ratesLatest = rates.coverage.last;
const lastNonNullIdx = cpiRows.reduce((acc, row, i) => (row.cpiaucsl_yoy === null ? acc : i), -1);
const lastNonNull = cpiRows[lastNonNullIdx];

const browser = await launchChromium();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });

const errors = [];
page.on('console', m => { if (m.type() === 'error') errors.push('[console] ' + m.text()); });
page.on('pageerror', e => errors.push('[pageerror] ' + e.message));

// 不用 networkidle：Chart.js 走 CDN，网络不畅时该事件永不触发（实测超时 30s）。
// 等状态行被改写 —— 它在 .then 与 .catch 里都会执行，是「加载流程已跑完」的信号。
//
// waitForFunction 的第二个参数是传给页面函数的 arg，配置要放第三个位置：
// 写成 waitForFunction(fn, {timeout}) 时那个对象被当成 arg，超时静默退回默认 30s。
// CDN 取 Chart.js 会偶发失败（本机三连跑复现过一次），失败时页面一张图都没有。
// 重试 3 次：真的断网仍会红，一次网络抖动不会。
let loadErr = null;
for (let attempt = 1; attempt <= 3; attempt++) {
  try {
    // 每次重试都清空 errors：上一次 CDN 取失败会留下一条 "Failed to load resource"，
    // 不清的话重试成功后那条旧错误仍会把「console error 为 0」判红。
    errors.length = 0;
    await page.goto(base + '/macro.html', { waitUntil: 'domcontentloaded', timeout: 60000 });
    await page.waitForFunction(
      () => typeof Chart !== 'undefined' && !!Chart.registry.plugins.get('zoom')
        && !/正在加载/.test(document.getElementById('status')?.textContent || '')
        && !/正在加载/.test(document.getElementById('debtStatus')?.textContent || ''),
      null,
      { timeout: 25000 });
    loadErr = null;
    break;
  } catch (err) {
    loadErr = err;
    console.log(`  第 ${attempt} 次加载未完成：${err.message.split('\n')[0]}`);
  }
}
if (loadErr) {
  const err = loadErr;
  // 本机实测过一次：CDN 取不到 chart.umd.min.js 时 Chart 始终 undefined，页面
  // 一张图都建不起来。这时本脚本什么也验证不了 —— 必须红，且要能和「页面自己
  // 坏了」区分开，否则下次看到红会去改页面。
  const chartLoaded = await page.evaluate(() => typeof Chart !== 'undefined');
  const status = await page.evaluate(() => document.getElementById('status')?.textContent || '');
  console.log(`FAIL 页面加载流程未跑完  ${JSON.stringify({ chartLoaded, status, err: err.message })}`);
  console.log(chartLoaded
    ? '  → Chart.js 已加载，是页面自身没跑完渲染流程'
    : '  → Chart.js（CDN）未加载：环境问题，不是 macro.html 的缺陷；联网后重跑');
  await browser.close();
  server.close();
  console.log('0 passed, 1 failed');
  process.exit(1);
}
await page.waitForTimeout(1200);

const state = await page.evaluate(() => {
  const read = id => {
    const chart = Chart.getChart(document.getElementById(id));
    if (!chart) return null;
    const points = chart.getDatasetMeta(0).data.filter(p => !p.skip);
    const tooltipLabel = chart.options.plugins.tooltip.callbacks?.label;
    const tooltipSamples = id === 'debtOverviewChart' && typeof tooltipLabel === 'function'
      ? {
          amount: tooltipLabel({ parsed: { y: 12345.6 },
            dataset: { label: '联邦债务总额', yAxisID: 'yAmount' } }),
          pct: tooltipLabel({ parsed: { y: 122.56 },
            dataset: { label: '联邦债务 / GDP', yAxisID: 'yPct' } }),
          missing: tooltipLabel({ parsed: { y: null },
            dataset: { label: '外国持有', yAxisID: 'yAmount' } }),
        }
      : null;
    return {
      labels: chart.data.labels,
      datasets: chart.data.datasets.map((d, index) => {
        const meta = chart.getDatasetMeta(index);
        const visiblePoints = meta.data.filter(point => !point.skip
          && Number.isFinite(point.x) && Number.isFinite(point.y));
        const visibleSegments = (meta.dataset?.segments || [])
          .filter(segment => segment.end > segment.start);
        const segmentPointIndexes = new Set();
        for (const segment of visibleSegments) {
          for (let pointIndex = segment.start; pointIndex <= segment.end; pointIndex++) {
            if (!meta.data[pointIndex]?.skip) segmentPointIndexes.add(pointIndex);
          }
        }
        return {
          label: d.label,
          spanGaps: d.spanGaps,
          fill: d.fill,
          stack: d.stack ?? null,
          yAxisID: d.yAxisID ?? null,
          sourceField: d.sourceField ?? null,
          sourceFrequency: d.sourceFrequency ?? null,
          type: d.type || chart.config.type,
          data: Array.from(d.data),
          visiblePointCount: visiblePoints.length,
          visibleSegmentCount: visibleSegments.length,
          segmentVisiblePointCount: segmentPointIndexes.size,
          renderedPointRadius: visiblePoints[0]?.options?.radius ?? null,
        };
      }),
      scaleAxes: Object.values(chart.scales).map(s => ({
        id: s.id,
        axis: s.axis,
        position: s.options.position || '',
        stacked: s.options.stacked === true,
        title: s.options.title?.text || '',
        tickSample: s.axis === 'y' && typeof s.options.ticks?.callback === 'function'
          ? String(s.options.ticks.callback.call(s, 12345.6, 0, []))
          : '',
        tickCount: s.ticks?.length ?? 0,
        maxTicksLimit: s.options.ticks?.maxTicksLimit ?? null,
        min: s.min,
        max: s.max,
      })),
      tooltipSamples,
      legendLabels: chart.legend?.legendItems?.map(item => item.text) || [],
      firstYs: points.slice(0, 5).map(p => p.y),
      chartAreaBottom: chart.chartArea.bottom,
    };
  };
  return {
    ust: read('ustChart'),
    fed: read('fedChart'),
    cpi: read('cpiChart'),
    debtOverview: read('debtOverviewChart'),
    debtPanelExists: !!document.getElementById('debtPanel'),
    debtCanvasIds: Array.from(document.querySelectorAll('#debtPanel canvas')).map(el => el.id),
    oldDebtCanvasesExist: !!document.getElementById('debtRatioChart')
      || !!document.getElementById('debtStackChart'),
    debtPanelText: document.getElementById('debtPanel')?.textContent || '',
    debtStatus: document.getElementById('debtStatus')?.textContent || '',
    debtStatusClass: document.getElementById('debtStatus')?.className || '',
    debtDailyAsOf: document.getElementById('debtDailyAsOf')?.textContent || '',
    debtForeignAsOf: document.getElementById('debtForeignAsOf')?.textContent || '',
    debtGdpAsOf: document.getElementById('debtGdpAsOf')?.textContent || '',
    resetExists: !!document.getElementById('debtResetZoom'),
    resetDisabled: document.getElementById('debtResetZoom')?.disabled,
    zoomRegistered: !!Chart.registry.plugins.get('zoom'),
    zoomOptions: (() => {
      const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
      const zoom = chart?.options?.plugins?.zoom?.zoom;
      return zoom ? {
        mode: zoom.mode,
        dragEnabled: zoom.drag?.enabled,
        dragThreshold: zoom.drag?.threshold,
        dragBackground: zoom.drag?.backgroundColor,
        wheelEnabled: zoom.wheel?.enabled,
        pinchEnabled: zoom.pinch?.enabled,
      } : null;
    })(),
    release: document.getElementById('cpiRelease').textContent,
    status: document.getElementById('status').textContent,
    statusClass: document.getElementById('status').className,
  };
});

// —— 加载与控制台 ——
check('macro.html 四张图全部建起', !!(state.ust && state.fed && state.cpi && state.debtOverview),
  JSON.stringify({ ust: !!state.ust, fed: !!state.fed, cpi: !!state.cpi,
    debtOverview: !!state.debtOverview }));
check('page errors / console error 为 0', errors.length === 0, JSON.stringify(errors));
check('状态行非报错态', !/失败/.test(state.status) && !/err/.test(state.statusClass), state.status);
check('debt 状态行非报错态', !/失败/.test(state.debtStatus) && !/err/.test(state.debtStatusClass), state.debtStatus);
check('macro_debt.json 被页面请求', requestedPaths.includes('data/derived/macro_debt.json'),
  JSON.stringify(requestedPaths.filter(p => p.includes('macro_'))));
check('treasury_debt_daily.json 被页面请求', requestedPaths.includes('data/treasury_debt_daily.json'),
  JSON.stringify(requestedPaths.filter(p => p.includes('treasury_debt'))));
check('美国联邦债务面板存在', state.debtPanelExists, String(state.debtPanelExists));
check('债务区域只有一个主 Chart canvas',
  JSON.stringify(state.debtCanvasIds) === JSON.stringify(['debtOverviewChart']),
  JSON.stringify(state.debtCanvasIds));
check('DOM 不再存在旧的两个独立债务 canvas', !state.oldDebtCanvasesExist,
  String(state.oldDebtCanvasesExist));

// —— dataset 数 ——
check('UST 图 3 条 dataset', state.ust.datasets.length === 3,
  JSON.stringify(state.ust.datasets.map(d => d.label)));
check('Fed 图 3 条 dataset', state.fed.datasets.length === 3,
  JSON.stringify(state.fed.datasets.map(d => d.label)));
check('CPI 图 2 条 dataset', state.cpi.datasets.length === 2,
  JSON.stringify(state.cpi.datasets.map(d => d.label)));

// —— C14 mixed-frequency debt contract / stack / dual axis ——
const debtByField = Object.fromEntries(state.debtOverview.datasets.map(d => [d.sourceField, d]));
const expectedDebtFields = [
  'structure_intragov_bn', 'structure_domestic_public_bn', 'structure_foreign_bn',
  'daily_total_bn', 'daily_public_bn', 'daily_intragov_bn',
  'foreign_bn', 'gdp_bn', 'debt_gdp_pct',
];
const expectedDebtLabels = [
  '结构快照 · 政府内部', '结构快照 · 本国公众', '结构快照 · 外国持有',
  '联邦债务总额', '公众持有债务', '政府内部持有',
  '外国持有（真实季度观测）', '美国名义 GDP', '联邦债务 / GDP（季度）',
];
check('债务总览恰有九个 mixed-frequency dataset',
  JSON.stringify(state.debtOverview.datasets.map(d => d.sourceField)) === JSON.stringify(expectedDebtFields)
    && JSON.stringify(state.debtOverview.datasets.map(d => d.label)) === JSON.stringify(expectedDebtLabels),
  JSON.stringify(state.debtOverview.datasets.map(d => ({ label: d.label, field: d.sourceField }))));
check('债务图例顺序与 mixed-frequency 语义一致',
  JSON.stringify(state.debtOverview.legendLabels) === JSON.stringify(expectedDebtLabels),
  JSON.stringify(state.debtOverview.legendLabels));
check('Treasury 日频信封为 strict schema v0 daily', dailyDebtFile.schema_version === 0
  && dailyDebtFile.freq === 'daily' && dailyDebtFile.date_field === 'date'
  && dailyDebtFile.coverage.count === dailyDebtRows.length,
  JSON.stringify({ schema: dailyDebtFile.schema_version, freq: dailyDebtFile.freq,
    coverage: dailyDebtFile.coverage, rows: dailyDebtRows.length }));

const quarterlyByDate = new Map(debtRows.map(r => [r.date, r]));
const dailyByDate = new Map(dailyDebtRows.map(r => [r.date, r]));
const expectedDates = [...new Set([...quarterlyByDate.keys(), ...dailyByDate.keys()])].sort();
check('债务 X 轴是季度与真实日频日期的去重有序并集',
  JSON.stringify(state.debtOverview.labels) === JSON.stringify(expectedDates)
    && new Set(state.debtOverview.labels).size === state.debtOverview.labels.length,
  JSON.stringify({ rendered: state.debtOverview.labels.length, expected: expectedDates.length,
    first: state.debtOverview.labels[0], last: state.debtOverview.labels.at(-1) }));
check('1990 长历史保留且日频从官方起点接入',
  state.debtOverview.labels[0] === '1990-01-01'
    && dailyDebtFile.coverage.first === '1993-04-01'
    && state.debtOverview.labels.includes(dailyDebtFile.coverage.first)
    && debtRows.filter(r => r.date < dailyDebtFile.coverage.first).length > 0,
  JSON.stringify({ firstLabel: state.debtOverview.labels[0], dailyFirst: dailyDebtFile.coverage.first }));

const firstDailyByField = Object.fromEntries(['total_bn', 'public_bn', 'intragov_bn'].map(field => [
  field, dailyDebtRows.find(r => r[field] !== null)?.date || null,
]));
const expectedHybrid = field => expectedDates.map(date => {
  if (firstDailyByField[field] && date >= firstDailyByField[field]) {
    return dailyByDate.get(date)?.[field] ?? null;
  }
  return quarterlyByDate.get(date)?.[field] ?? null;
});
const expectedQuarterly = field => expectedDates.map(date => quarterlyByDate.get(date)?.[field] ?? null);
const expectedQuarterlyPoints = field => debtRows
  .filter(row => row[field] !== null && row[field] !== undefined)
  .map(row => ({ x: row.date, y: row[field] }));

for (const field of ['total_bn', 'public_bn', 'intragov_bn']) {
  const rendered = debtByField['daily_' + field];
  check(`日频金额 ${field} 按字段真实起点衔接且逐点不补值`,
    JSON.stringify(rendered?.data) === JSON.stringify(expectedHybrid(field))
      && rendered?.sourceFrequency === 'hybrid_quarterly_then_daily',
    JSON.stringify({ firstDaily: firstDailyByField[field], rendered: rendered?.data.length,
      expected: expectedDates.length }));
}
for (const [sourceField, sourceRowField] of [
  ['structure_intragov_bn', 'intragov_bn'],
  ['structure_domestic_public_bn', 'domestic_public_bn'],
  ['structure_foreign_bn', 'foreign_bn'],
]) {
  check(`低频 dataset ${sourceField} 只映射真实季度观测`,
    JSON.stringify(debtByField[sourceField]?.data) === JSON.stringify(expectedQuarterly(sourceRowField))
      && debtByField[sourceField]?.sourceFrequency === 'quarterly',
    JSON.stringify({ rendered: debtByField[sourceField]?.data.length, expected: expectedDates.length }));
}

for (const field of ['foreign_bn', 'gdp_bn', 'debt_gdp_pct']) {
  const rendered = debtByField[field];
  const expected = expectedQuarterlyPoints(field);
  check(`低频折线 ${field} 只含真实 observation object，数量与源一致`,
    JSON.stringify(rendered?.data) === JSON.stringify(expected)
      && rendered?.sourceFrequency === 'quarterly'
      && rendered?.data.length === expected.length,
    JSON.stringify({ rendered: rendered?.data.length, expected: expected.length,
      first: rendered?.data[0], last: rendered?.data.at(-1) }));
}
check('GDP 与 debt/GDP 全历史视图存在真实可见 points / segments',
  ['gdp_bn', 'debt_gdp_pct'].every(field => {
    const dataset = debtByField[field];
    return dataset?.visiblePointCount === expectedQuarterlyPoints(field).length
      && dataset.visiblePointCount > 1 && dataset.visibleSegmentCount > 0
      && dataset.segmentVisiblePointCount === dataset.visiblePointCount
      && dataset.renderedPointRadius > 0;
  }),
  JSON.stringify(['gdp_bn', 'debt_gdp_pct'].map(field => ({ field,
    points: debtByField[field]?.visiblePointCount,
    segments: debtByField[field]?.visibleSegmentCount,
    segmentPoints: debtByField[field]?.segmentVisiblePointCount,
    radius: debtByField[field]?.renderedPointRadius }))));
check('foreign 全历史视图存在真实可见低频 observations / segments',
  debtByField.foreign_bn?.visiblePointCount === expectedQuarterlyPoints('foreign_bn').length
    && debtByField.foreign_bn.visiblePointCount > 1
    && debtByField.foreign_bn.visibleSegmentCount > 0
    && debtByField.foreign_bn.segmentVisiblePointCount === debtByField.foreign_bn.visiblePointCount
    && debtByField.foreign_bn.renderedPointRadius > 0,
  JSON.stringify({ points: debtByField.foreign_bn?.visiblePointCount,
    segments: debtByField.foreign_bn?.visibleSegmentCount,
    segmentPoints: debtByField.foreign_bn?.segmentVisiblePointCount,
    radius: debtByField.foreign_bn?.renderedPointRadius }));

const structureFields = [
  'structure_intragov_bn', 'structure_domestic_public_bn', 'structure_foreign_bn',
];
const structureStacks = structureFields.map(field => debtByField[field]?.stack);
check('三个结构快照 dataset 属于同一非空 stack 且绑定金额轴',
  structureStacks.every(Boolean) && new Set(structureStacks).size === 1
    && structureFields.every(field => debtByField[field]?.type === 'bar'
      && debtByField[field]?.yAxisID === 'yAmount'),
  JSON.stringify(structureFields.map(field => ({ field, stack: debtByField[field]?.stack,
    axis: debtByField[field]?.yAxisID }))));
for (const field of ['daily_total_bn', 'daily_public_bn', 'daily_intragov_bn', 'foreign_bn', 'gdp_bn']) {
  check(`金额折线 ${field} 绑定左轴`,
    debtByField[field]?.type === 'line' && debtByField[field]?.yAxisID === 'yAmount',
    JSON.stringify({ type: debtByField[field]?.type, axis: debtByField[field]?.yAxisID }));
}
check('正式 debt/GDP 保持季度且绑定右轴',
  debtByField.debt_gdp_pct?.type === 'line'
    && debtByField.debt_gdp_pct?.yAxisID === 'yPct'
    && debtByField.debt_gdp_pct?.sourceFrequency === 'quarterly',
  JSON.stringify({ type: debtByField.debt_gdp_pct?.type,
    axis: debtByField.debt_gdp_pct?.yAxisID,
    freq: debtByField.debt_gdp_pct?.sourceFrequency }));
check('public_gdp_pct 仍不作为图上 dataset', !debtByField.public_gdp_pct,
  JSON.stringify(state.debtOverview.datasets.map(d => d.sourceField)));

const overlapDate = dailyDebtRows.find(r => quarterlyByDate.has(r.date)
  && quarterlyByDate.get(r.date).total_bn !== r.total_bn)?.date;
const overlapIdx = state.debtOverview.labels.indexOf(overlapDate);
check('日频覆盖期同日 total 优先 Treasury，不被 FRED 季度值覆盖', !!overlapDate
  && debtByField.daily_total_bn.data[overlapIdx] === dailyByDate.get(overlapDate).total_bn
  && debtByField.daily_total_bn.data[overlapIdx] !== quarterlyByDate.get(overlapDate).total_bn,
  JSON.stringify({ overlapDate,
    rendered: debtByField.daily_total_bn.data[overlapIdx],
    treasury: dailyByDate.get(overlapDate)?.total_bn,
    fred: quarterlyByDate.get(overlapDate)?.total_bn }));
const noDailyQuarter = debtRows.find(r => r.date >= firstDailyByField.total_bn
  && !dailyByDate.has(r.date));
const noDailyQuarterIdx = state.debtOverview.labels.indexOf(noDailyQuarter?.date);
check('Treasury 起点后缺少真实日记录时不回退季度 total', !!noDailyQuarter
  && debtByField.daily_total_bn.data[noDailyQuarterIdx] === null,
  JSON.stringify({ date: noDailyQuarter?.date,
    rendered: debtByField.daily_total_bn.data[noDailyQuarterIdx] }));

const anomalyDate = dailyDebtFile.warnings
  .map(text => /^\d{4}-\d{2}-\d{2}/.exec(text)?.[0])
  .find(Boolean);
const sourceAnomaly = dailyByDate.get(anomalyDate);
const sourceAnomalyIdx = state.debtOverview.labels.indexOf(sourceAnomaly?.date);
check('Treasury warning 指向的单日分项异常保持双 null，但 total 继续显示', !!sourceAnomaly
  && debtByField.daily_total_bn.data[sourceAnomalyIdx] === sourceAnomaly.total_bn
  && debtByField.daily_public_bn.data[sourceAnomalyIdx] === null
  && debtByField.daily_intragov_bn.data[sourceAnomalyIdx] === null,
  JSON.stringify({ date: sourceAnomaly?.date, total: sourceAnomaly?.total_bn,
    public: debtByField.daily_public_bn.data[sourceAnomalyIdx],
    intragov: debtByField.daily_intragov_bn.data[sourceAnomalyIdx] }));

const incompleteStructureRows = debtRows.filter(r =>
  ['intragov_bn', 'domestic_public_bn', 'foreign_bn'].some(f => r[f] === null));
check('foreign/恒等式缺口的结构三项同时 null，不补 0/前值', incompleteStructureRows.length > 0
  && incompleteStructureRows.every(r =>
    ['intragov_bn', 'domestic_public_bn', 'foreign_bn'].every(f => r[f] === null)),
  JSON.stringify(incompleteStructureRows.map(r => r.date)));
const lagRow = debtRows.find(r => r.date > debtMeta.stack_last
  && r.total_bn !== null && r.gdp_bn !== null && r.debt_gdp_pct !== null);
const lagIdx = state.debtOverview.labels.indexOf(lagRow?.date);
const gdpPointByDate = new Map(debtByField.gdp_bn.data.map(point => [point.x, point.y]));
const ratioPointByDate = new Map(debtByField.debt_gdp_pct.data.map(point => [point.x, point.y]));
check('foreign 右端缺口季度 stack 为空但 GDP/debt-GDP 继续', !!lagRow
  && structureFields.every(field => debtByField[field].data[lagIdx] === null)
  && gdpPointByDate.get(lagRow.date) === lagRow.gdp_bn
  && ratioPointByDate.get(lagRow.date) === lagRow.debt_gdp_pct,
  JSON.stringify({ date: lagRow?.date,
    stack: structureFields.map(field => debtByField[field].data[lagIdx]),
    gdp: gdpPointByDate.get(lagRow?.date), ratio: ratioPointByDate.get(lagRow?.date) }));

const identityRow = debtRows.find(r => r.total_bn !== null
  && ['intragov_bn', 'domestic_public_bn', 'foreign_bn'].every(f => r[f] !== null));
const identityIdx = state.debtOverview.labels.indexOf(identityRow?.date);
const renderedStructureTotal = identityRow ? structureFields.reduce(
  (sum, field) => sum + debtByField[field].data[identityIdx], 0) : null;
check('真实季度结构快照三项之和与季度 total 对账', !!identityRow
  && Math.abs(renderedStructureTotal - identityRow.total_bn)
    <= Math.max(1, Math.abs(identityRow.total_bn)) * 1e-12,
  JSON.stringify({ date: identityRow?.date, renderedStructureTotal, total: identityRow?.total_bn }));

const debtY = state.debtOverview.scaleAxes.filter(s => s.axis === 'y');
const amountY = debtY.find(s => s.id === 'yAmount');
const pctY = debtY.find(s => s.id === 'yPct');
check('债务总览只有 yAmount / yPct 两条 Y 轴',
  debtY.length === 2 && !!amountY && !!pctY, JSON.stringify(debtY));
check('yAmount 是左侧堆叠金额轴且单位为 USD bn',
  amountY?.position === 'left' && amountY?.stacked && amountY?.title === 'USD bn'
    && amountY?.tickSample === '12,345.6', JSON.stringify(amountY));
check('yPct 是右侧非堆叠比例轴且单位为 %',
  pctY?.position === 'right' && !pctY?.stacked && pctY?.title === '%'
    && pctY?.tickSample === '12,345.6%', JSON.stringify(pctY));
check('债务 tooltip 区分 USD bn / % 且 null 不显示为 0',
  /12,345\.6 USD bn$/.test(state.debtOverview.tooltipSamples?.amount || '')
    && /122\.6%$/.test(state.debtOverview.tooltipSamples?.pct || '')
    && /--$/.test(state.debtOverview.tooltipSamples?.missing || '')
    && !/0(?:\.0+)? USD bn$/.test(state.debtOverview.tooltipSamples?.missing || ''),
  JSON.stringify(state.debtOverview.tooltipSamples));
check('债务面板明确说明左右轴与 mixed-frequency 不填充',
  /左轴（USD bn）.*右轴（%）/.test(state.debtPanelText)
    && /低频数据未做日频填充/.test(state.debtPanelText), state.debtPanelText.trim());
const debtX = state.debtOverview.scaleAxes.find(s => s.axis === 'x');
check('全历史 8452 个日期由 Chart.js 自动减至最多 12 个 tick',
  debtX?.maxTicksLimit === 12 && debtX.tickCount <= 12
    && debtX.tickCount < state.debtOverview.labels.length,
  JSON.stringify({ tickCount: debtX?.tickCount, labels: state.debtOverview.labels.length }));

const freshnessDays = (Date.now() - Date.parse(dailyDebtFile.coverage.last + 'T00:00:00Z')) / 86400000;
check('页面分别显示 debt / foreign / GDP 真实截至日期',
  state.debtDailyAsOf === dailyDebtFile.coverage.last
    && state.debtForeignAsOf === debtMeta.foreign_last.slice(0, 7)
    && state.debtGdpAsOf === `${debtMeta.gdp_last.slice(0, 4)}-Q${Math.floor((Number(debtMeta.gdp_last.slice(5, 7)) - 1) / 3) + 1}`,
  JSON.stringify({ debt: state.debtDailyAsOf, foreign: state.debtForeignAsOf,
    gdp: state.debtGdpAsOf }));
check('Treasury coverage.last 在合理日频 freshness 阈值内',
  freshnessDays >= -1 && freshnessDays <= 10,
  JSON.stringify({ last: dailyDebtFile.coverage.last, freshnessDays }));
const latestIdx = state.debtOverview.labels.indexOf(dailyDebtFile.coverage.last);
const latestDaily = dailyDebtRows.at(-1);
check('官方最新日频 record 被三条金额线使用', latestDaily.date === dailyDebtFile.coverage.last
  && debtByField.daily_total_bn.data[latestIdx] === latestDaily.total_bn
  && debtByField.daily_public_bn.data[latestIdx] === latestDaily.public_bn
  && debtByField.daily_intragov_bn.data[latestIdx] === latestDaily.intragov_bn,
  JSON.stringify({ latest: latestDaily.date, idx: latestIdx,
    total: debtByField.daily_total_bn.data[latestIdx] }));

check('zoom plugin 已注册且 reset 按钮初始 disabled', state.zoomRegistered
  && state.resetExists && state.resetDisabled, JSON.stringify({ registered: state.zoomRegistered,
    resetExists: state.resetExists, resetDisabled: state.resetDisabled }));
check('drag zoom 只启用 X 轴并有深色主题选择框与最小阈值',
  state.zoomOptions?.mode === 'x' && state.zoomOptions.dragEnabled === true
    && state.zoomOptions.dragThreshold >= 10
    && /rgba/.test(state.zoomOptions.dragBackground || '')
    && state.zoomOptions.wheelEnabled === false && state.zoomOptions.pinchEnabled === false,
  JSON.stringify(state.zoomOptions));

// —— Fed 图不画中值 ——
// FRED 只发布上下限，中值是构造出来的数。既查 label 也查条数：只查 label 的话，
// 把中值系列改个名字就能绕过。
const MID_RE = /中值|中位|midpoint|median|mid[_-]?point/i;
check('Fed 图无中值 dataset',
  !state.fed.datasets.some(d => MID_RE.test(d.label || '')) && state.fed.datasets.length === 3,
  JSON.stringify(state.fed.datasets.map(d => d.label)));
check('Fed 图 dataset 恰为上限/下限/DFF',
  JSON.stringify(state.fed.datasets.map(d => d.label))
    === JSON.stringify(['目标区间上限', '目标区间下限', 'DFF 有效利率']),
  JSON.stringify(state.fed.datasets.map(d => d.label)));
check('Fed 区间带靠 fill:+1，不靠中值线',
  state.fed.datasets[0].fill === '+1' && state.fed.datasets[1].fill === false,
  JSON.stringify(state.fed.datasets.map(d => d.fill)));

// —— 单 Y 轴 ——
for (const [name, chart] of [['UST', state.ust], ['Fed', state.fed], ['CPI', state.cpi]]) {
  const yAxes = chart.scaleAxes.filter(s => s.axis === 'y');
  check(name + ' 图只有一条 Y 轴', yAxes.length === 1, JSON.stringify(chart.scaleAxes));
}
const totalY = [state.ust, state.fed, state.cpi]
  .reduce((n, c) => n + c.scaleAxes.filter(s => s.axis === 'y').length, 0);
check('全页 Y 轴总数 = 3（每图各一条，无第二条）', totalY === 3, String(totalY));

// —— 不跨 null 连线 ——
check('CPI 图两条线 spanGaps=false',
  state.cpi.datasets.every(d => d.spanGaps === false),
  JSON.stringify(state.cpi.datasets.map(d => d.spanGaps)));
check('全页所有 dataset spanGaps=false',
  [state.ust, state.fed, state.cpi].every(c => c.datasets.every(d => d.spanGaps === false)),
  JSON.stringify([state.ust, state.fed, state.cpi].map(c => c.datasets.map(d => d.spanGaps))));
check('债务总览全部折线 spanGaps=false',
  state.debtOverview.datasets.filter(d => d.type === 'line').every(d => d.spanGaps === false),
  JSON.stringify(state.debtOverview.datasets.filter(d => d.type === 'line').map(d => d.spanGaps)));

// —— 真实鼠标拖框、reset 与 legend 交互 ——
await page.locator('#debtOverviewChart').scrollIntoViewIfNeeded();
await page.waitForTimeout(100);
const initialZoom = await page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
  const rect = chart.canvas.getBoundingClientRect();
  return {
    xMin: chart.scales.x.min, xMax: chart.scales.x.max,
    amountMin: chart.scales.yAmount.min, amountMax: chart.scales.yAmount.max,
    pctMin: chart.scales.yPct.min, pctMax: chart.scales.yPct.max,
    chartArea: {
      left: rect.left + chart.chartArea.left,
      right: rect.left + chart.chartArea.right,
      top: rect.top + chart.chartArea.top,
      bottom: rect.top + chart.chartArea.bottom,
    },
  };
});
const dragY = (initialZoom.chartArea.top + initialZoom.chartArea.bottom) / 2;
// Deliberately drag right-to-left: both directions must work.
await page.mouse.move(initialZoom.chartArea.right - 100, dragY);
await page.mouse.down({ button: 'left' });
await page.mouse.move(initialZoom.chartArea.left + 350, dragY, { steps: 12 });
await page.mouse.up({ button: 'left' });
try {
  await page.waitForFunction(() => {
    const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
    return chart?.isZoomedOrPanned?.() === true;
  }, null, { timeout: 5000 });
} catch (_) {
  // Keep evaluating and emit stable FAIL markers below. Injection wrappers
  // must not depend on a Playwright timeout stack trace as their evidence.
}
const zoomed = await page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
  return {
    xMin: chart.scales.x.min, xMax: chart.scales.x.max,
    amountMin: chart.scales.yAmount.min, amountMax: chart.scales.yAmount.max,
    pctMin: chart.scales.yPct.min, pctMax: chart.scales.yPct.max,
    resetDisabled: document.getElementById('debtResetZoom').disabled,
    tooltipEnabled: chart.options.plugins.tooltip.enabled !== false,
  };
});
check('真实右向左鼠标 drag 后 X 可见范围明显缩小',
  zoomed.xMax - zoomed.xMin < (initialZoom.xMax - initialZoom.xMin) * 0.8,
  JSON.stringify({ before: [initialZoom.xMin, initialZoom.xMax],
    after: [zoomed.xMin, zoomed.xMax] }));
check('drag zoom 不改变左右 Y 轴范围且 tooltip 保持启用',
  zoomed.amountMin === initialZoom.amountMin && zoomed.amountMax === initialZoom.amountMax
    && zoomed.pctMin === initialZoom.pctMin && zoomed.pctMax === initialZoom.pctMax
    && zoomed.tooltipEnabled,
  JSON.stringify({ initialZoom, zoomed }));
check('缩放后 reset 按钮变为可用', zoomed.resetDisabled === false,
  String(zoomed.resetDisabled));

await page.evaluate(() => document.getElementById('debtResetZoom').click());
try {
  await page.waitForFunction(() => {
    const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
    return !chart?.isZoomedOrPanned?.();
  }, null, { timeout: 5000 });
} catch (_) {
  // Stable reset assertion below reports the product failure.
}
const resetState = await page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
  return { xMin: chart.scales.x.min, xMax: chart.scales.x.max,
    resetDisabled: document.getElementById('debtResetZoom').disabled };
});
check('点击重置缩放恢复完整历史范围', resetState.xMin === initialZoom.xMin
  && resetState.xMax === initialZoom.xMax && resetState.resetDisabled === true,
  JSON.stringify({ initial: [initialZoom.xMin, initialZoom.xMax], resetState }));

// 再做一次左向右真实拖框，并以真实双击事件复验第二条 reset 路径。
await page.mouse.move(initialZoom.chartArea.left + 180, dragY);
await page.mouse.down({ button: 'left' });
await page.mouse.move(initialZoom.chartArea.right - 250, dragY, { steps: 10 });
await page.mouse.up({ button: 'left' });
try {
  await page.waitForFunction(() => Chart.getChart(document.getElementById('debtOverviewChart'))
    ?.isZoomedOrPanned?.() === true, null, { timeout: 5000 });
} catch (_) {}
await page.mouse.dblclick(
  (initialZoom.chartArea.left + initialZoom.chartArea.right) / 2, dragY,
  { button: 'left' });
try {
  await page.waitForFunction(() => !Chart.getChart(document.getElementById('debtOverviewChart'))
    ?.isZoomedOrPanned?.(), null, { timeout: 5000 });
} catch (_) {}
const doubleResetState = await page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
  return {
    xMin: chart.scales.x.min, xMax: chart.scales.x.max,
    amountMin: chart.scales.yAmount.min, amountMax: chart.scales.yAmount.max,
    pctMin: chart.scales.yPct.min, pctMax: chart.scales.yPct.max,
    resetDisabled: document.getElementById('debtResetZoom').disabled,
  };
});
check('双击重置恢复完整 X 范围且左右 Y 轴保持不变',
  doubleResetState.xMin === initialZoom.xMin && doubleResetState.xMax === initialZoom.xMax
    && doubleResetState.amountMin === initialZoom.amountMin
    && doubleResetState.amountMax === initialZoom.amountMax
    && doubleResetState.pctMin === initialZoom.pctMin
    && doubleResetState.pctMax === initialZoom.pctMax
    && doubleResetState.resetDisabled === true,
  JSON.stringify({ initial: [initialZoom.xMin, initialZoom.xMax], doubleResetState }));

// 4 px < threshold=12，必须保持完整范围。
await page.mouse.move(initialZoom.chartArea.left + 120, dragY);
await page.mouse.down({ button: 'left' });
await page.mouse.move(initialZoom.chartArea.left + 124, dragY);
await page.mouse.up({ button: 'left' });
const tinyDrag = await page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
  return { xMin: chart.scales.x.min, xMax: chart.scales.x.max,
    zoomed: chart.isZoomedOrPanned() };
});
check('极小拖动低于 threshold 不触发异常 zoom', tinyDrag.xMin === initialZoom.xMin
  && tinyDrag.xMax === initialZoom.xMax && !tinyDrag.zoomed, JSON.stringify(tinyDrag));

const legendToggle = await page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
  const item = chart.legend.legendItems[0];
  const onClick = chart.options.plugins.legend.onClick || Chart.defaults.plugins.legend.onClick;
  const before = chart.isDatasetVisible(item.datasetIndex);
  onClick.call(chart.legend, {}, item, chart.legend);
  const hidden = !chart.isDatasetVisible(item.datasetIndex);
  onClick.call(chart.legend, {}, item, chart.legend);
  const restored = chart.isDatasetVisible(item.datasetIndex);
  return { before, hidden, restored, handler: typeof onClick };
});
check('zoom 接入后 Chart.js legend 点击处理器仍可隐藏/恢复 dataset',
  legendToggle.handler === 'function' && legendToggle.before
    && legendToggle.hidden && legendToggle.restored,
  JSON.stringify(legendToggle));

// —— CPI 右端不被补齐 ——
// 「早于」按月比：CPI 参考期是月度（YYYY-MM），利率是日度（YYYY-MM-DD）。
check('CPI 最新非 null YoY 的 ref_period 早于利率最新日期',
  !!lastNonNull && lastNonNull.ref_period < ratesLatest.slice(0, 7),
  JSON.stringify({ cpi: lastNonNull && lastNonNull.ref_period, rates: ratesLatest }));
check('派生文件里最新非 null 之后无补出的行',
  lastNonNullIdx === cpiRows.length - 1
    || cpiRows.slice(lastNonNullIdx + 1).every(r => r.cpiaucsl_yoy === null && r.cpilfesl_yoy === null),
  JSON.stringify({ lastNonNullIdx, total: cpiRows.length }));
check('CPI 图末个标签就是最新非 null 参考期（没往后补月份）',
  state.cpi.labels[state.cpi.labels.length - 1] === lastNonNull.ref_period,
  JSON.stringify({ lastLabel: state.cpi.labels[state.cpi.labels.length - 1], expect: lastNonNull.ref_period }));
check('CPI 图不含利率最新月份的点（不拿利率日期给 CPI 造点）',
  !state.cpi.labels.includes(ratesLatest.slice(0, 7)),
  JSON.stringify({ ratesMonth: ratesLatest.slice(0, 7) }));

// —— 首屏落位 ——
for (const [name, chart] of [['UST', state.ust], ['Fed', state.fed], ['CPI', state.cpi]]) {
  const gap = Math.min(...chart.firstYs.map(y => chart.chartAreaBottom - y));
  check(name + ' 图首屏不贴底（间距 > 1px）', chart.firstYs.length > 0 && gap > 1,
    JSON.stringify({ ys: chart.firstYs, bottom: chart.chartAreaBottom, gap }));
}

// —— 发布日文案 ——
check('发布日文案与派生 meta 一致，且照实说未保存',
  state.release === 'CPI 最新参考期 ' + cpiFile.data.meta.cpi_latest_ref_period + '，发布日：当前采集未保存',
  JSON.stringify(state.release));
check('文案里不出现任何 YYYY-MM-DD 形态的日期（不冒充发布日）',
  !/\d{4}-\d{2}-\d{2}/.test(state.release), JSON.stringify(state.release));
check('派生 meta 明示发布日不可用',
  cpiFile.data.meta.cpi_latest_published_at === null
    && cpiFile.data.meta.cpi_release_date_available === false,
  JSON.stringify(cpiFile.data.meta));

// —— 加载故障隔离 ——
// 用网络层返回 500，验证真正的 loadJson/.catch 边界；不是隐藏 DOM 或直接调用
// render 函数。两个场景都重新打开完整页面，确保另一组模块走真实加载与渲染。
async function loadFailureScenario(blockedFiles) {
  const probe = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const probeErrors = [];
  probe.on('console', m => { if (m.type() === 'error') probeErrors.push('[console] ' + m.text()); });
  probe.on('pageerror', e => probeErrors.push('[pageerror] ' + e.message));
  for (const file of blockedFiles) {
    await probe.route(`**/${file}*`, route => route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ injected: file }),
    }));
  }
  await probe.goto(base + '/macro.html', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await probe.waitForFunction(() => typeof Chart !== 'undefined' && !!Chart.registry.plugins.get('zoom')
    && !/正在加载/.test(document.getElementById('status')?.textContent || '')
    && !/正在加载/.test(document.getElementById('debtStatus')?.textContent || ''), null,
  { timeout: 25000 });
  await probe.waitForTimeout(300);
  const result = await probe.evaluate(() => {
    const alive = id => !!Chart.getChart(document.getElementById(id));
    return {
      ust: alive('ustChart'), fed: alive('fedChart'), cpi: alive('cpiChart'),
      debtOverview: alive('debtOverviewChart'),
      status: document.getElementById('status')?.textContent || '',
      statusClass: document.getElementById('status')?.className || '',
      debtStatus: document.getElementById('debtStatus')?.textContent || '',
      debtStatusClass: document.getElementById('debtStatus')?.className || '',
    };
  });
  await probe.close();
  return { result, errors: probeErrors };
}

const debtFailure = await loadFailureScenario(['data/derived/macro_debt.json']);
check('debt 加载错误只挂 debt 模块',
  debtFailure.result.ust && debtFailure.result.fed && debtFailure.result.cpi
    && !debtFailure.result.debtOverview
    && /失败/.test(debtFailure.result.debtStatus)
    && /err/.test(debtFailure.result.debtStatusClass),
  JSON.stringify(debtFailure));
check('debt 加载错误明确进入 debt 错误边界',
  debtFailure.errors.some(e => e.includes('[fetch] 美国联邦债务数据加载失败')),
  JSON.stringify(debtFailure.errors));

const dailyDebtFailure = await loadFailureScenario(['data/treasury_debt_daily.json']);
check('Treasury 日频加载错误只挂 debt 模块',
  dailyDebtFailure.result.ust && dailyDebtFailure.result.fed && dailyDebtFailure.result.cpi
    && !dailyDebtFailure.result.debtOverview
    && /失败/.test(dailyDebtFailure.result.debtStatus)
    && /err/.test(dailyDebtFailure.result.debtStatusClass),
  JSON.stringify(dailyDebtFailure));
check('Treasury 日频加载错误进入 debt 错误边界',
  dailyDebtFailure.errors.some(e => e.includes('[fetch] 美国联邦债务数据加载失败')),
  JSON.stringify(dailyDebtFailure.errors));

const ratesCpiFailure = await loadFailureScenario([
  'data/derived/macro_rates.json', 'data/derived/macro_cpi.json',
]);
check('rates/CPI 加载错误不影响 debt 模块',
  !ratesCpiFailure.result.ust && !ratesCpiFailure.result.fed && !ratesCpiFailure.result.cpi
    && ratesCpiFailure.result.debtOverview
    && !/失败/.test(ratesCpiFailure.result.debtStatus),
  JSON.stringify(ratesCpiFailure));
check('rates/CPI 加载错误明确进入原宏观错误边界',
  /失败/.test(ratesCpiFailure.result.status)
    && /err/.test(ratesCpiFailure.result.statusClass)
    && ratesCpiFailure.errors.some(e => e.includes('[fetch] 宏观数据加载失败')),
  JSON.stringify(ratesCpiFailure.errors));

// 直接使用真正的 touch/mobile context 验证移动端，不把已经渲染过桌面图表的
// page 缩窄后当作手机。后者会保留 Chart.js 桌面 canvas 内联宽度，测到的是
// Playwright viewport 切换副作用，而不是用户首次打开页面的移动端布局。
const mobileContext = await browser.newContext({
  viewport: { width: 390, height: 844 },
  isMobile: true,
  hasTouch: true,
});
const mobilePage = await mobileContext.newPage();
const mobileErrors = [];
mobilePage.on('pageerror', e => mobileErrors.push(e.message));
await mobilePage.goto(base + '/macro.html', { waitUntil: 'domcontentloaded', timeout: 60000 });
await mobilePage.waitForFunction(() => typeof Chart !== 'undefined'
  && !!Chart.registry.plugins.get('zoom')
  && !/正在加载/.test(document.getElementById('debtStatus')?.textContent || ''), null,
{ timeout: 25000 });
const touchState = await mobilePage.evaluate(() => {
  const canvas = document.getElementById('debtOverviewChart');
  const chart = Chart.getChart(canvas);
  const rect = canvas.getBoundingClientRect();
  window.scrollTo(0, 500);
  return {
    finePointer: matchMedia('(hover: hover) and (pointer: fine)').matches,
    dragEnabled: chart.options.plugins.zoom.zoom.drag.enabled,
    resetExists: !!document.getElementById('debtResetZoom'),
    scrollY: window.scrollY,
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth: window.innerWidth,
    canvasLeft: rect.left,
    canvasRight: rect.right,
    chartAlive: !!chart,
    legendWithinChart: !!chart?.legend
      && chart.legend.width <= chart.width && chart.legend.height < chart.height,
  };
});
check('真实触摸环境禁用 drag zoom 且页面仍可滚动', !touchState.finePointer
  && touchState.dragEnabled === false && touchState.resetExists && touchState.scrollY > 0,
  JSON.stringify(touchState));
check('真实触摸环境无横向溢出且无 pageerror',
  touchState.chartAlive
    && touchState.scrollWidth <= touchState.innerWidth + 1
    && touchState.canvasLeft >= 0 && touchState.canvasRight <= touchState.innerWidth + 1
    && touchState.legendWithinChart && mobileErrors.length === 0,
  JSON.stringify({ touchState, mobileErrors }));
await mobileContext.close();

await browser.close();
server.close();
console.log(pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
