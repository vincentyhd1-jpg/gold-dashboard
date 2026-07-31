import { chromium } from 'playwright';
const execPath = String.raw`C:\Users\vince\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe`;
const browser = await chromium.launch({ headless: true, executablePath: execPath });
const page = await browser.newPage({ viewport: { width: 1400, height: 1200 } });
const errs = [];
page.on('pageerror', e => errs.push(e.message));
page.on('console', m => { if (m.type() === 'error') errs.push('[console] ' + m.text()); });

await page.goto('http://localhost:3001', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForFunction(
  () => typeof Chart !== 'undefined' && Chart.getChart('oiChart'), { timeout: 60000 });
await page.waitForTimeout(1000);

// derive 的原始落盘值（全精度）。价差断言比这个，不比前端显示串 ——
// 显示串已过 toFixed(2)，拿它对齐后再比等于比截断后的值，绿得没意义。
const derived = await page.evaluate(async () => {
  const r = await fetch('data/derived/term-structure-series.json?_=' + Date.now());
  const p = await r.json();
  const s = p?.data ?? p;
  return Object.fromEntries(s.frames.map(f => [
    f.date.slice(5).replace('-', '/'),
    { total_oi: f.total_oi, spread: f.spread,
      gap: f.spread_gap_days, ann: f.spread_annualized_pct,
      roll_from: f.roll_from, roll_to: f.roll_to },
  ]));
});

const EPS = 1e-9;

let pass = 0, fail = 0;
const check = (n, ok, d = '') => {
  if (ok) { pass++; console.log(`  PASS  ${n}`); }
  else { fail++; console.log(`  FAIL  ${n}  ${JSON.stringify(d)}`); }
};

// ── 1. X 轴按存续过滤 ──────────────────────────────────────────────────
console.log('[1] X 轴合约列表');
const axis = await page.evaluate(() => {
  const c = Chart.getChart('oiChart');
  return { labels: c.data.labels, n: c.data.labels.length };
});
console.log(`  ${axis.n} 列: ${axis.labels.join(' ')}`);
check('JUN26（已到期）已剔除', !axis.labels.includes('JUN26'), axis.labels);
for (const m of ['MAR27', 'MAY27', 'JUL27']) {
  check(`${m}（仍挂牌、持仓微小）已保留`, axis.labels.includes(m));
}
// 保留列在日历序上必须连续（无缺口才不会扭曲曲线几何）
const MON = { JAN:1,FEB:2,MAR:3,APR:4,MAY:5,JUN:6,JUL:7,AUG:8,SEP:9,OCT:10,NOV:11,DEC:12 };
const keys = axis.labels.map(l => {
  const m = /^([A-Z]{3})(\d{2})$/.exec(l);
  return (2000 + +m[2]) * 12 + MON[m[1]];
});
const gaps = keys.slice(1).map((k, i) => k - keys[i]).filter(d => d !== 1);
check('日历序连续无缺口（曲线几何未扭曲）', gaps.length === 0,
      `间隔异常: ${gaps}`);

// ── 2. 最小柱高 ────────────────────────────────────────────────────────
// 必须读动画结束后的几何：el.y 在动画期间是插值中间态，DEC26（24 万手）
// 也会读成 y===base、高度 0。用 getProps(..., true) 取最终值。
console.log('\n[2] 微小持仓最小柱高');
await page.evaluate(() => {
  // 关掉动画并重绘，确保元素落在终态
  const c = Chart.getChart('oiChart');
  c.options.animation = false;
  c.update('none');
});
await page.waitForTimeout(400);
const bars = await page.evaluate(() => {
  const c = Chart.getChart('oiChart');
  const meta = c.getDatasetMeta(0);
  const zeroY = meta.yScale.getPixelForValue(0);
  return c.data.labels.map((l, i) => {
    const p = meta.data[i].getProps(['y', 'base'], true);
    return {
      label: l,
      oi: meta.data[i].$context?.raw,
      h: Math.abs((p.base ?? zeroY) - p.y),
    };
  });
});
for (const b of bars) {
  console.log(`    ${b.label.padEnd(6)} oi=${String(b.oi ?? '-').padStart(7)}  柱高=${b.h.toFixed(2)}px`);
}
const tiny = bars.filter(b => b.oi != null && b.oi > 0 && b.oi < 100);
check('微小持仓合约存在于图上', tiny.length > 0, `找到 ${tiny.length} 个`);
check('所有非零持仓柱高 >= 2px',
      bars.filter(b => b.oi != null && b.oi > 0).every(b => b.h >= 1.99),
      bars.filter(b => b.oi > 0 && b.h < 1.99).map(b => `${b.label}:${b.h.toFixed(2)}`));

// ── 3. 价差锚点 ────────────────────────────────────────────────────────
console.log('\n[3] 价差锚点与年化率');
for (const frac of [1, 0]) {
  const r = await page.evaluate(f => {
    const s = document.getElementById('oiPlaySlider');
    s.value = String(Math.round(f * Number(s.max)));
    s.dispatchEvent(new Event('input', { bubbles: true }));
    const c = Chart.getChart('oiChart');
    const settles = {}, ois = {};
    c.data.labels.forEach((l, i) => {
      settles[l] = c.data.datasets[1].data[i];
      ois[l] = c.data.datasets[0].data[i];
    });
    return {
      date: document.getElementById('oiPlayDate').textContent,
      val: document.getElementById('oiContango').textContent,
      label: document.getElementById('oiContangoLabel').textContent,
      cardLabel: document.querySelector('#oiContango').previousElementSibling.textContent,
      oiVal: document.getElementById('oiVal').textContent,
      settles, ois,
    };
  }, frac);
  const d = derived[r.date];
  console.log(`\n  ${r.date}  卡片「${r.cardLabel}」= ${r.val}`);
  console.log(`    副标题: ${r.label}`);
  console.log(`    derive 落盘: spread=${d?.spread} gap=${d?.gap} ann=${d?.ann} total_oi=${d?.total_oi}`);

  if (!d) {
    check(`${r.date} derive 有对应帧`, false, r.date);
  } else {
    // 独立复算，与 derive 的全精度落盘值比，容差 1e-9
    const expSpread = r.settles[d.roll_to] - r.settles[d.roll_from];
    const expGap = Math.round(
      (Date.UTC(2000 + +d.roll_to.slice(3), MON[d.roll_to.slice(0, 3)] - 1, 1)
       - Date.UTC(2000 + +d.roll_from.slice(3), MON[d.roll_from.slice(0, 3)] - 1, 1)) / 86400000);
    const expAnn = expSpread / r.settles[d.roll_from] * (365 / expGap) * 100;
    const expOi = Object.entries(r.ois)
      .filter(([l]) => r.settles[l] != null)
      .reduce((s, [, v]) => s + (v || 0), 0);

    check(`${r.date} derive spread 与结算价复算一致`,
          Math.abs(d.spread - expSpread) < EPS, { got: d.spread, exp: expSpread });
    check(`${r.date} derive gap_days = 真实日历天数`,
          d.gap === expGap, { got: d.gap, exp: expGap });
    check(`${r.date} derive 年化与复算一致`,
          Math.abs(d.ann - expAnn) < EPS, { got: d.ann, exp: expAnn });
    check(`${r.date} derive total_oi 与柱子求和一致`,
          d.total_oi === expOi, { got: d.total_oi, exp: expOi });

    // 计算层不得舍入到显示精度：落盘值若恰等于自身 2 位舍入、而真值不是，
    // 说明精度在派生层就丢了
    const rounded2 = v => Math.round(v * 100) / 100;
    check(`${r.date} derive 落全精度（未舍入到 2 位）`,
          !(d.ann === rounded2(d.ann) && expAnn !== rounded2(expAnn)),
          { ann: d.ann, rounded: rounded2(expAnn) });

    // 展示层：卡片显示 = 落盘值过 toFixed(2)
    check(`${r.date} 卡片显示 = 落盘值 toFixed(2)`,
          r.val === (d.ann >= 0 ? '+' : '') + d.ann.toFixed(2) + '%',
          { shown: r.val, expect: d.ann.toFixed(2) });
    check(`${r.date} 副标题价差 = 落盘值 toFixed(2)`,
          r.label === `${d.roll_to} − ${d.roll_from}：`
            + (d.spread >= 0 ? '+' : '') + d.spread.toFixed(2),
          { shown: r.label });
    check(`${r.date} 总持仓卡 = 落盘值`,
          r.oiVal === d.total_oi.toLocaleString('en-US'),
          { shown: r.oiVal, expect: d.total_oi.toLocaleString('en-US') });

    check(`${r.date} 锚点非首末列`,
          d.roll_from !== axis.labels[0] || d.roll_to !== axis.labels[axis.labels.length - 1],
          { near: d.roll_from, far: d.roll_to });
    check(`${r.date} 锚点均在 X 轴上`,
          axis.labels.includes(d.roll_from) && axis.labels.includes(d.roll_to));
  }
}

console.log(`\n${pass} passed, ${fail} failed`);
console.log('page errors:', errs.length ? errs : 'none');
await browser.close();
process.exit(fail || errs.length ? 1 : 0);
