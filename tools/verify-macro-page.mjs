// macro.html 护栏：图形态（三图 / dataset 数 / 无中值 / 单 Y 轴 / 不跨 null 连线）、
// CPI 右端不被补齐、首屏不贴底、发布日文案不冒充日期。
//
// 与其他前端 verify 的两处差异，都是刻意的：
//   1. 自带静态服务：其余脚本依赖外部已起的 localhost:3001。本脚本用 node 内置
//      http 起在随机空端口上，跑之前不需要先开 server，也不依赖 python。
//   2. Chromium 路径走 tools/_browser.mjs 的 findChromiumExecutable()，
//      不再抄第二份硬编码路径。
//
// 首屏那条断言是把一次性人工实测固化下来的：Chart.js 首帧曾出现全部点贴在
// chartArea.bottom 上（需要 setTimeout(update('none'),0) 兜），现在实测不贴底，
// 于是用断言钉住 —— 若哪天回归贴底，这条会红，而不是靠人再测一次。
import { chromium } from 'playwright';
import http from 'http';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { findChromiumExecutable } from './_browser.mjs';

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

const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
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
const cpiRows = cpiFile.data.cpi;
const ratesLatest = rates.coverage.last;
const lastNonNullIdx = cpiRows.reduce((acc, row, i) => (row.cpiaucsl_yoy === null ? acc : i), -1);
const lastNonNull = cpiRows[lastNonNullIdx];

const execPath = findChromiumExecutable();
if (!execPath) throw new Error('找不到 Playwright Chromium chrome.exe');
const browser = await chromium.launch({ headless: true, executablePath: execPath });
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
        && !/正在加载/.test(document.getElementById('status')?.textContent || ''),
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
    return {
      labels: chart.data.labels,
      datasets: chart.data.datasets.map(d => ({
        label: d.label,
        spanGaps: d.spanGaps,
        fill: d.fill,
      })),
      scaleAxes: Object.values(chart.scales).map(s => ({ id: s.id, axis: s.axis })),
      firstYs: points.slice(0, 5).map(p => p.y),
      chartAreaBottom: chart.chartArea.bottom,
    };
  };
  return {
    ust: read('ustChart'),
    fed: read('fedChart'),
    cpi: read('cpiChart'),
    release: document.getElementById('cpiRelease').textContent,
    status: document.getElementById('status').textContent,
    statusClass: document.getElementById('status').className,
  };
});

// —— 加载与控制台 ——
check('macro.html 三张图全部建起', !!(state.ust && state.fed && state.cpi),
  JSON.stringify({ ust: !!state.ust, fed: !!state.fed, cpi: !!state.cpi }));
check('page errors / console error 为 0', errors.length === 0, JSON.stringify(errors));
check('状态行非报错态', !/失败/.test(state.status) && !/err/.test(state.statusClass), state.status);

// —— dataset 数 ——
check('UST 图 3 条 dataset', state.ust.datasets.length === 3,
  JSON.stringify(state.ust.datasets.map(d => d.label)));
check('Fed 图 3 条 dataset', state.fed.datasets.length === 3,
  JSON.stringify(state.fed.datasets.map(d => d.label)));
check('CPI 图 2 条 dataset', state.cpi.datasets.length === 2,
  JSON.stringify(state.cpi.datasets.map(d => d.label)));

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

await browser.close();
server.close();
console.log(pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
