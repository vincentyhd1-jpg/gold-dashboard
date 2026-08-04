import { chromium } from 'playwright';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const execPath = 'C:\\Users\\vince\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';

const browser = await chromium.launch({ headless: true, executablePath: execPath });
const page = await browser.newPage({ viewport: { width: 1400, height: 1200 } });
const errs = [];
page.on('pageerror', e => errs.push(e.message));
page.on('console', m => { if (m.type() === 'error') errs.push('[console] ' + m.text()); });

// 不用 networkidle：Chart.js 走 CDN，网络不畅时该事件永不触发（实测超时 30s）。
// 直接等图表实例就绪。
await page.goto('http://localhost:3001', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForFunction(
  () => typeof Chart !== 'undefined' && Chart.getChart('oiChart') && Chart.getChart('oiDeltaChart'),
  { timeout: 60000 });
await page.waitForSelector('#oiPlaybar');
await page.waitForTimeout(800);

let pass = 0, fail = 0;
const check = (name, ok, detail = '') => {
  if (ok) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}  ${JSON.stringify(detail)}`); }
};

// ── 1. 柱心对齐 ────────────────────────────────────────────────────────
console.log('[1] 柱心 vs 标签心对齐');
const align = await page.evaluate(() => {
  const c = Chart.getChart('oiChart'), d = Chart.getChart('oiDeltaChart');
  const barIdx = c.data.datasets
    .map((ds, i) => ({ ds, i }))
    .filter(x => (x.ds.type || c.config.type) === 'bar').map(x => x.i);
  const cm = c.getDatasetMeta(barIdx[barIdx.length - 1]);
  const dm = d.getDatasetMeta(0);
  const out = { barDs: barIdx.length, rows: [] };
  for (let li = 0; li < c.data.labels.length; li++) {
    out.rows.push({
      label: c.data.labels[li],
      mainOff: Math.round(cm.data[li].x - c.scales.x.getPixelForValue(li)),
      mainW: Math.round(cm.data[li].width),
      deltaOff: Math.round(dm.data[li].x - d.scales.x.getPixelForValue(li)),
      deltaW: Math.round(dm.data[li].width),
    });
  }
  return out;
});
console.log(`  main bar datasets = ${align.barDs}`);
for (const r of align.rows) {
  console.log(`    ${r.label.padEnd(6)} main off=${String(r.mainOff).padStart(4)} w=${String(r.mainW).padStart(3)}   delta off=${String(r.deltaOff).padStart(4)} w=${String(r.deltaW).padStart(3)}`);
}
check('主图所有柱 offset 为 0', align.rows.every(r => r.mainOff === 0),
      align.rows.filter(r => r.mainOff !== 0).map(r => `${r.label}:${r.mainOff}`));
check('主图与 delta 柱宽一致', align.rows.every(r => Math.abs(r.mainW - r.deltaW) <= 1),
      align.rows.filter(r => Math.abs(r.mainW - r.deltaW) > 1).map(r => `${r.label}:${r.mainW}vs${r.deltaW}`));

// ── 2. 倍速按钮选中态 ──────────────────────────────────────────────────
console.log('\n[2] 倍速按钮选中态');
const btnState = async () => page.evaluate(() => ({
  sp: [...document.querySelectorAll('.oi-speed-btns button')].map(b => ({
    speed: b.dataset.speed,
    pressed: b.getAttribute('aria-pressed'),
    bg: getComputedStyle(b).backgroundColor,
    color: getComputedStyle(b).color,
  })),
  ghostGone: !document.getElementById('oiGhostToggle'),
}));
let st = await btnState();
console.log('  初始:', st.sp.map(s => `${s.speed}x[${s.pressed}]`).join(' '));
check('默认 1x 为 pressed', st.sp.find(s => s.speed === '1').pressed === 'true');
check('未选中倍速 pressed=false', st.sp.filter(s => s.speed !== '1').every(s => s.pressed === 'false'));
const active = st.sp.find(s => s.speed === '1'), idle = st.sp.find(s => s.speed === '2');
check('选中态背景与未选中不同', active.bg !== idle.bg, { active: active.bg, idle: idle.bg });

await page.click('.oi-speed-btns button[data-speed="2"]');
await page.waitForTimeout(150);
st = await btnState();
console.log('  点 2x 后:', st.sp.map(s => `${s.speed}x[${s.pressed}]`).join(' '));
check('点击后仅 2x 为 pressed',
      st.sp.filter(s => s.pressed === 'true').map(s => s.speed).join() === '2');
check('残影按钮已移除', st.ghostGone);

// ── 3. roll 面板 X 轴 ──────────────────────────────────────────────────
console.log('\n[3] roll 面板 X 轴刻度与隔离');
const roll = await page.evaluate(() => {
  const r = Chart.getChart('oiRollChart');
  const sep = document.querySelector('#oiRollWrap .oi-panel-sep');
  return {
    xDisplay: r.options.scales.x.display,
    xLabels: r.scales.x.ticks.map(t => t.label).slice(0, 10),
    tickCount: r.scales.x.ticks.length,
    sepText: sep ? sep.textContent.trim() : null,
    sepVisible: sep ? getComputedStyle(sep).display !== 'none' : false,
  };
});
console.log('  x.display =', roll.xDisplay, ' 刻度数 =', roll.tickCount);
console.log('  刻度:', roll.xLabels.join(' '));
console.log('  分隔标题:', roll.sepText);
check('roll X 轴已显示', roll.xDisplay === true);
check('roll X 轴有日期刻度', roll.tickCount > 0 && /\d+\/\d+/.test(roll.xLabels[0]), roll.xLabels);
check('有分隔线小标题', roll.sepVisible && /日期/.test(roll.sepText || ''), roll.sepText);

// ── 4. 合约列表 = 全序列 window_months 并集 ────────────────────────────
// 不按持仓大小过滤。持仓微小的月份保留并渲染成细线 —— 它们仍有结算价，
// 删列会把相邻点间距从 1 个月变成 2 个月，扭曲价格曲线几何。
// 也不按「末帧是否仍挂牌」过滤：到期合约保留 X 轴列位，否则它在仍存续的
// 那些帧上有真实 settle/oi 却无列可放，移仓起点整列消失。
console.log('\n[4] 合约列表 = 全序列 window_months 并集');
const cols = await page.evaluate(() => Chart.getChart('oiChart').data.labels);
console.log('  X 轴合约:', cols.join(' '));
check('已到期合约仍保留 X 轴列位（末帧已不挂牌）', cols.includes('JUN26'), cols);
check('MAR27/MAY27/JUL27（持仓微小但仍挂牌）保留',
      ['MAR27', 'MAY27', 'JUL27'].every(m => cols.includes(m)), cols);

// 回放仍正常
await page.click('#oiPlayBtn');
await page.waitForTimeout(1200);
const playing = await page.evaluate(() => document.getElementById('oiPlayDate').textContent);
await page.click('#oiPlayBtn');
console.log('\n  播放中日期:', playing);
check('回放仍正常', /\d+\/\d+/.test(playing), playing);

// ── 5. cotDual 金价 dataset 非 null 点数 ───────────────────────────────
// 守 index.html:1255 的 gold_price.json 读取点。该点的失败性质是**静默退化**
// 而非崩溃：读到信封形状（非数组）时 index.html:466 的 Array.isArray(goldData)
// 会退化成空 map，goldD 变全 null，金价线整条消失而 pageerror 为空 ——
// 现有断言无一条会红。
//
// 阈值 N=40 的依据：采集层 validate_gold 的 MAX_MISSING_RATIO=0.2 保证
// 52 周里至多 20% 对齐失败，即非 null 至少 42 个；取 40 留 2 个余量应对
// cot weekly 条数不足 52 的情形。实测当前 52/52 全部非 null（余量 +12）。
// 退化时是 0 个，与 40 之间有整个量级的距离，不会误判。
const GOLD_MIN_POINTS = 40;
console.log('\n[5] cotDual 金价 dataset');
const gold = await page.evaluate(() => {
  const c = Chart.getChart('cotDualChart');
  if (!c) return { chartMissing: true };
  const ds = c.data.datasets.find(d => /金价/.test(d.label || ''));
  if (!ds) return { dsMissing: true, labels: c.data.datasets.map(d => d.label) };
  const nonNull = ds.data.filter(v => v !== null && v !== undefined);
  return {
    label: ds.label,
    len: ds.data.length,
    nonNull: nonNull.length,
    min: nonNull.length ? Math.min(...nonNull) : null,
    max: nonNull.length ? Math.max(...nonNull) : null,
  };
});
console.log(`  ${JSON.stringify(gold)}`);
check('cotDualChart 已建起', !gold.chartMissing, gold);
check('金价 dataset 存在', !gold.dsMissing, gold);
check(`金价 dataset 非 null 点数 >= ${GOLD_MIN_POINTS}`,
      (gold.nonNull ?? 0) >= GOLD_MIN_POINTS, gold);

// ── 6. 期限结构历史图 dataset 数 == oi.json 帧数 ───────────────────────
// 守 index.html:1269 的 data/oi.json 读取点。失败性质同金价那条：**静默退化**。
// 读到信封形状（非数组）时，:1103 的 Array.isArray(oiData) 退化成 []，
// :1106 的 `if (!records.length) return` 直接返回 —— 图整张消失，
// 而 pageerror / console error 都是空的，六个 verify 无一条会红。
//
// 判据用相对值（== 帧数）而非固定下界：帧数随时间增长，固定 N 会逐渐
// 衰减成恒真。金价那条的 N=40 之所以成立，是靠采集层 MAX_MISSING_RATIO
// 反推出的硬下界，oi 这边没有等价闸门。严格相等还能顺带盯住「少画了几帧」
// 这类部分失败 —— 那种情况下固定下界同样看不出来。
console.log('\n[6] 期限结构历史图 dataset 数');
const oiRaw = await fetch('http://localhost:3001/data/oi.json').then(r => r.json());
const expectedFrames = Array.isArray(oiRaw) ? oiRaw.length : (oiRaw?.data?.length ?? null);
const hist = await page.evaluate(() => {
  const c = Chart.getChart('oiHistChart');
  if (!c) return { chartMissing: true };
  return { datasets: c.data.datasets.length, labels: c.data.labels.length };
});
console.log(`  oi.json 帧数=${expectedFrames}  ${JSON.stringify(hist)}`);
check('oiHistChart 已建起', !hist.chartMissing, hist);
check(`历史图 dataset 数 == oi.json 帧数 (${expectedFrames})`,
      hist.datasets === expectedFrames, { got: hist.datasets, want: expectedFrames });

// 截图
await page.evaluate(() => {
  const s = document.getElementById('oiPlaySlider');
  s.value = s.max; s.dispatchEvent(new Event('input', { bubbles: true }));
});
await page.waitForTimeout(600);
await page.locator('#oiChart').scrollIntoViewIfNeeded();
await page.waitForTimeout(400);
const top = await page.locator('#oiChart').boundingBox();
const bot = await page.locator('#oiPlaybar').boundingBox();
await page.screenshot({
  path: path.join(__dirname, '..', 'screenshots', 'ui-fixes.png'),
  clip: { x: 100, y: top.y - 70, width: 1200, height: (bot.y + bot.height) - (top.y - 70) + 16 },
});

console.log(`\n${pass} passed, ${fail} failed`);
console.log('page errors:', errs.length ? errs : 'none');
await browser.close();
process.exit(fail || errs.length ? 1 : 0);
