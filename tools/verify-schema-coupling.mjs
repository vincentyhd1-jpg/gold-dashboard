// 体检：四个 verify 脚本是否会在 schema 破坏时静默通过？
// 它们都不直接读派生 JSON（走 Chart.js 实例 + DOM），需要确认这不等于
// "对 schema 破坏无感" —— 若图表照样建起来只是数据错，那才是危险的静默通过。
import { launchChromium } from './_browser.mjs';
const browser = await launchChromium();

async function probe(label, patch) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 1200 } });
  const errs = [];
  page.on('pageerror', e => errs.push(e.message.slice(0, 60)));
  page.on('console', m => { if (m.type() === 'error') errs.push('[c] ' + m.text().slice(0, 60)); });

  if (patch) {
    await page.route('**/term-structure-series.json*', async route => {
      const res = await route.fetch();
      const p = await res.json();
      await route.fulfill({ response: res, body: JSON.stringify(patch(p)) });
    });
  }
  await page.goto('http://localhost:3001', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForTimeout(3500);

  const st = await page.evaluate(() => ({
    charts: ['oiChart', 'oiDeltaChart', 'oiRollChart']
      .map(id => !!(typeof Chart !== 'undefined' && Chart.getChart(id))),
    labels: (typeof Chart !== 'undefined' && Chart.getChart('oiChart'))
      ? Chart.getChart('oiChart').data.labels.length : 0,
    playDate: document.getElementById('oiPlayDate')?.textContent,
    contango: document.getElementById('oiContango')?.textContent,
  }));
  console.log(`  ${label.padEnd(30)} charts=${JSON.stringify(st.charts)} labels=${st.labels} date=${st.playDate} 价差=${st.contango}`);
  console.log(`  ${''.padEnd(30)} errors=${errs.length ? errs[0] : 'none'}`);
  await page.close();
  return st;
}

console.log('基线：');
const base = await probe('正常信封', null);

console.log('\n破坏 schema（模拟回归）：');
// 1. data 键整个消失（信封字段还在）
const a = await probe('data 键缺失', p => { const q = { ...p }; delete q.data; return q; });
// 2. data 存在但 frames 空
const b = await probe('data.frames 为空数组', p => ({ ...p, data: { ...p.data, frames: [] } }));
// 3. 退回旧的平铺格式（strict 下应拒绝：四源+派生全部信封化后兼容分支已删）
const c = await probe('旧平铺格式（应拒绝）', p => p.data);

console.log('\n判定：');
let failed = 0;
const dead = s => s.charts.every(x => !x);
const judge = (name, ok, okMsg, badMsg) => {
  console.log(`  ${name} →`, ok ? okMsg : badMsg);
  if (!ok) failed++;
};
judge('data 缺失     ', dead(a), '图表未建起 → 四脚本的几何断言必然失败（不会静默通过）', '图表仍建起 ← 需查是否静默通过');
judge('frames 空     ', dead(b), '图表未建起 → 断言失败', `图表建起但 labels=${b.labels} → 几何/合约列表断言会失败`);
judge('旧平铺格式    ', dead(c), '抛错/图表未建起 → strict 拒绝裸格式生效', `图表仍建起（labels=${c.labels}）← strict 未生效，兼容分支仍在`);

await browser.close();
console.log(`\n${3 - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
