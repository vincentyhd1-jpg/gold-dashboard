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
      () => typeof Chart !== 'undefined'
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
      datasets: chart.data.datasets.map(d => ({
        label: d.label,
        spanGaps: d.spanGaps,
        fill: d.fill,
        stack: d.stack ?? null,
        yAxisID: d.yAxisID ?? null,
        sourceField: d.sourceField ?? null,
        type: d.type || chart.config.type,
        data: Array.from(d.data),
      })),
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

// —— 债务单图 / 数据契约 / stack / 双轴 ——
const debtByField = Object.fromEntries(state.debtOverview.datasets.map(d => [d.sourceField, d]));
const expectedDebtFields = [
  'intragov_bn', 'domestic_public_bn', 'foreign_bn',
  'total_bn', 'gdp_bn', 'debt_gdp_pct',
];
const expectedDebtLabels = [
  '政府内部持有', '本国公众持有', '外国持有',
  '联邦债务总额', '美国名义 GDP', '联邦债务 / GDP',
];
check('债务总览恰有六个目标 dataset',
  JSON.stringify(state.debtOverview.datasets.map(d => d.sourceField)) === JSON.stringify(expectedDebtFields)
    && JSON.stringify(state.debtOverview.datasets.map(d => d.label)) === JSON.stringify(expectedDebtLabels),
  JSON.stringify(state.debtOverview.datasets.map(d => ({ label: d.label, field: d.sourceField }))));
check('债务图例按结构柱、金额线、比例线顺序展示',
  JSON.stringify(state.debtOverview.legendLabels) === JSON.stringify(expectedDebtLabels),
  JSON.stringify(state.debtOverview.legendLabels));
for (const field of expectedDebtFields) {
  check(`债务总览 dataset ${field} 逐点直读派生字段`,
    JSON.stringify(debtByField[field]?.data) === JSON.stringify(debtRows.map(r => r[field])),
    JSON.stringify({ rendered: debtByField[field]?.data.length, source: debtRows.length }));
}
check('public_gdp_pct 不再作为图上 dataset', !debtByField.public_gdp_pct,
  JSON.stringify(state.debtOverview.datasets.map(d => d.sourceField)));
for (const field of ['intragov_bn', 'domestic_public_bn', 'foreign_bn']) {
  check(`债务结构 ${field} 为左轴堆叠柱`,
    debtByField[field]?.type === 'bar' && debtByField[field]?.yAxisID === 'yAmount',
    JSON.stringify(debtByField[field]));
}
for (const field of ['total_bn', 'gdp_bn']) {
  check(`金额折线 ${field} 绑定左轴`,
    debtByField[field]?.type === 'line' && debtByField[field]?.yAxisID === 'yAmount',
    JSON.stringify(debtByField[field]));
}
check('联邦债务/GDP 为右轴比例折线',
  debtByField.debt_gdp_pct?.type === 'line' && debtByField.debt_gdp_pct?.yAxisID === 'yPct',
  JSON.stringify(debtByField.debt_gdp_pct));
const structureStacks = ['intragov_bn', 'domestic_public_bn', 'foreign_bn']
  .map(field => debtByField[field]?.stack);
check('三个债务结构 dataset 属于同一非空 stack',
  structureStacks.every(Boolean) && new Set(structureStacks).size === 1,
  JSON.stringify(structureStacks));
const debtY = state.debtOverview.scaleAxes.filter(s => s.axis === 'y');
const amountY = debtY.find(s => s.id === 'yAmount');
const pctY = debtY.find(s => s.id === 'yPct');
check('债务总览只有 yAmount / yPct 两条 Y 轴',
  debtY.length === 2 && !!amountY && !!pctY, JSON.stringify(debtY));
check('yAmount 是左侧堆叠金额轴且单位为 USD bn',
  amountY?.position === 'left' && amountY?.stacked && amountY?.title === 'USD bn'
    && amountY?.tickSample === '12,345.6',
  JSON.stringify(amountY));
check('yPct 是右侧非堆叠比例轴且单位为 %',
  pctY?.position === 'right' && !pctY?.stacked && pctY?.title === '%'
    && pctY?.tickSample === '12,345.6%',
  JSON.stringify(pctY));
check('债务 tooltip 区分 USD bn / % 且 null 不显示为 0',
  /12,345\.6 USD bn$/.test(state.debtOverview.tooltipSamples?.amount || '')
    && /122\.6%$/.test(state.debtOverview.tooltipSamples?.pct || '')
    && /--$/.test(state.debtOverview.tooltipSamples?.missing || '')
    && !/0(?:\.0+)? USD bn$/.test(state.debtOverview.tooltipSamples?.missing || ''),
  JSON.stringify(state.debtOverview.tooltipSamples));
check('债务面板明确说明左右轴单位',
  /左轴（USD bn）.*右轴（%）/.test(state.debtPanelText), state.debtPanelText.trim());
check('债务总览季度标签逐点来自派生文件',
  JSON.stringify(state.debtOverview.labels) === JSON.stringify(debtRows.map(r => r.quarter)),
  JSON.stringify({ rendered: state.debtOverview.labels.length, source: debtRows.length }));

const firstValidTotal = debtRows.find(r => r.total_bn !== null
  && r.gdp_bn !== null && r.debt_gdp_pct !== null);
check('债务总额/GDP/比率真实历史从 1990-Q1 开始',
  firstValidTotal?.date === '1990-01-01' && firstValidTotal?.quarter === '1990-Q1',
  JSON.stringify(firstValidTotal));
check('债务图 labels 包含 1990 且没有人为裁到 2016',
  state.debtOverview.labels[0] === '1990-Q1'
    && state.debtOverview.labels.includes('1990-Q4')
    && debtRows.filter(r => r.date < '2016-01-01').length === 104
    && state.debtOverview.labels.length === debtRows.length,
  JSON.stringify({ first: state.debtOverview.labels[0], labels: state.debtOverview.labels.length,
    before2016: debtRows.filter(r => r.date < '2016-01-01').length }));

const rawDateSets = Object.fromEntries(Object.entries(rawDebtFiles)
  .map(([name, rows]) => [name, new Set(rows.map(r => r.date))]));
const firstRawStructureDate = [...rawDateSets.total]
  .filter(d => rawDateSets.public.has(d)
    && rawDateSets.intragov.has(d) && rawDateSets.foreign.has(d))
  .sort()[0];
const firstStructure = debtRows.find(r =>
  ['intragov_bn', 'domestic_public_bn', 'foreign_bn'].every(f => r[f] !== null));
check('结构历史起点由四条真实上游共同 coverage 决定',
  firstRawStructureDate === '1990-01-01' && firstStructure?.date === firstRawStructureDate,
  JSON.stringify({ firstRawStructureDate, firstDerivedStructure: firstStructure?.date }));
const incompleteStructureRows = debtRows.filter(r =>
  ['intragov_bn', 'domestic_public_bn', 'foreign_bn'].some(f => r[f] === null));
check('历史结构缺口三项同时为 null，且不被补 0',
  incompleteStructureRows.length > 0
    && incompleteStructureRows.every(r =>
      ['intragov_bn', 'domestic_public_bn', 'foreign_bn'].every(f => r[f] === null)),
  JSON.stringify(incompleteStructureRows.map(r => ({ date: r.date,
    values: [r.intragov_bn, r.domestic_public_bn, r.foreign_bn] }))));
const debtX = state.debtOverview.scaleAxes.find(s => s.axis === 'x');
check('36 年季度标签由 Chart.js 自动减至最多 12 个 tick',
  debtX?.maxTicksLimit === 12 && debtX.tickCount <= 12
    && debtX.tickCount < state.debtOverview.labels.length,
  JSON.stringify(debtX));

const identityIdx = debtRows.findIndex(r => r.total_bn !== null
  && ['intragov_bn', 'domestic_public_bn', 'foreign_bn'].every(f => r[f] !== null));
const identityRow = identityIdx >= 0 ? debtRows[identityIdx] : null;
const renderedStructureTotal = identityRow
  ? ['intragov_bn', 'domestic_public_bn', 'foreign_bn']
      .reduce((sum, field) => sum + debtByField[field].data[identityIdx], 0)
  : null;
check('真实完整季度的三项 chart 映射之和与 total_bn 对账', !!identityRow
  && Math.abs(renderedStructureTotal - identityRow.total_bn)
    <= Math.max(1, Math.abs(identityRow.total_bn)) * 1e-12,
  JSON.stringify({ date: identityRow?.date, renderedStructureTotal, total: identityRow?.total_bn }));

const lagIdx = debtRows.findIndex(r => r.date > debtMeta.stack_last
  && r.total_bn !== null && r.gdp_bn !== null && r.debt_gdp_pct !== null);
const lagRow = lagIdx >= 0 ? debtRows[lagIdx] : null;
check('派生锚点存在：foreign 右端滞后但总债务/GDP/比率仍有效', !!lagRow,
  JSON.stringify({ stackLast: debtMeta.stack_last, lagRow }));
check('foreign 右端缺失季度的三个源 stack 字段均为 null', !!lagRow
  && ['intragov_bn', 'domestic_public_bn', 'foreign_bn'].every(f => lagRow[f] === null),
  JSON.stringify(lagRow));
check('foreign 右端缺失季度的三个图表 stack 值均保持 null', !!lagRow
  && ['intragov_bn', 'domestic_public_bn', 'foreign_bn']
    .every(f => debtByField[f].data[lagIdx] === null),
  JSON.stringify(Object.fromEntries(['intragov_bn', 'domestic_public_bn', 'foreign_bn']
    .map(f => [f, debtByField[f]?.data[lagIdx]]))));
check('同一缺口季度 total debt / GDP / debt-GDP 仍显示', !!lagRow
  && debtByField.total_bn?.data[lagIdx] === lagRow.total_bn
  && debtByField.gdp_bn?.data[lagIdx] === lagRow.gdp_bn
  && debtByField.debt_gdp_pct?.data[lagIdx] === lagRow.debt_gdp_pct,
  JSON.stringify({ total: debtByField.total_bn?.data[lagIdx],
    gdp: debtByField.gdp_bn?.data[lagIdx], ratio: debtByField.debt_gdp_pct?.data[lagIdx] }));

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
check('债务总览三条折线 spanGaps=false',
  state.debtOverview.datasets.filter(d => d.type === 'line').every(d => d.spanGaps === false),
  JSON.stringify(state.debtOverview.datasets.filter(d => d.type === 'line').map(d => d.spanGaps)));

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
    await probe.route(`**/data/derived/${file}*`, route => route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ injected: file }),
    }));
  }
  await probe.goto(base + '/macro.html', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await probe.waitForFunction(() => typeof Chart !== 'undefined'
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

const debtFailure = await loadFailureScenario(['macro_debt.json']);
check('debt 加载错误只挂 debt 模块',
  debtFailure.result.ust && debtFailure.result.fed && debtFailure.result.cpi
    && !debtFailure.result.debtOverview
    && /失败/.test(debtFailure.result.debtStatus)
    && /err/.test(debtFailure.result.debtStatusClass),
  JSON.stringify(debtFailure));
check('debt 加载错误明确进入 debt 错误边界',
  debtFailure.errors.some(e => e.includes('[fetch] 美国联邦债务数据加载失败')),
  JSON.stringify(debtFailure.errors));

const ratesCpiFailure = await loadFailureScenario(['macro_rates.json', 'macro_cpi.json']);
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

await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(300);
const mobile = await page.evaluate(() => {
  const chart = Chart.getChart(document.getElementById('debtOverviewChart'));
  const rect = document.getElementById('debtOverviewChart').getBoundingClientRect();
  return {
    innerWidth: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    canvasLeft: rect.left,
    canvasRight: rect.right,
    chartAlive: !!chart,
    legendWithinChart: !!chart?.legend
      && chart.legend.width <= chart.width && chart.legend.height < chart.height,
  };
});
check('移动端债务图无页面横向溢出且 legend 保持在图内',
  mobile.chartAlive && mobile.scrollWidth <= mobile.innerWidth + 1
    && mobile.canvasLeft >= 0 && mobile.canvasRight <= mobile.innerWidth + 1
    && mobile.legendWithinChart,
  JSON.stringify(mobile));

await browser.close();
server.close();
console.log(pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
