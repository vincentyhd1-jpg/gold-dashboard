// 线上端到端验证（不是本地 dev server）。
import { launchChromium } from './_browser.mjs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const URL = 'https://zhangtongxue.com/?_=' + Date.now();

const browser = await launchChromium();
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });

const errs = [];
page.on('pageerror', e => errs.push('[pageerror] ' + e.message));
page.on('console', m => { if (m.type() === 'error') errs.push('[console] ' + m.text()); });

// 不用 networkidle：Chart.js 走 CDN，网络不畅时该事件永不触发（实测超时 30s）。
// 直接等图表实例就绪。线上站点放宽超时。
await page.goto(URL, { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForFunction(
  () => typeof Chart !== 'undefined' && Chart.getChart('oiChart'),
  { timeout: 60000 });
await page.waitForSelector('#oiPlaybar', { timeout: 15000 });
await page.waitForTimeout(1200);

const read = () => page.evaluate(() => ({
  date:   document.getElementById('oiPlayDate').textContent,
  slider: document.getElementById('oiPlaySlider').value,
  max:    document.getElementById('oiPlaySlider').max,
  btn:    document.getElementById('oiPlayBtn').textContent.trim(),
  front:  document.getElementById('oiFrontMonth').textContent,
  oiVal:  document.getElementById('oiVal').textContent,
  footer: document.getElementById('footerNote').textContent.slice(0, 24),
  charts: {
    main:  !!Chart.getChart('oiChart'),
    delta: !!Chart.getChart('oiDeltaChart'),
    roll:  !!Chart.getChart('oiRollChart'),
  },
}));

console.log('--- 线上初始状态 ---');
console.log(JSON.stringify(await read(), null, 1));

// 拖到首帧
await page.evaluate(() => {
  const s = document.getElementById('oiPlaySlider');
  s.value = 0; s.dispatchEvent(new Event('input', { bubbles: true }));
});
await page.waitForTimeout(400);
console.log('\n--- 滑块 → 0 ---');
console.log(JSON.stringify(await read()));

// 播放
await page.click('#oiPlayBtn');
await page.waitForTimeout(1400);
console.log('\n--- 播放 1.4s ---');
console.log(JSON.stringify(await read()));
await page.click('#oiPlayBtn');

// X 轴对齐实测：三个图的 chartArea.left / right
const align = await page.evaluate(() => {
  const g = id => {
    const c = Chart.getChart(id);
    if (!c) return null;
    return { left: +c.chartArea.left.toFixed(2), right: +(c.width - c.chartArea.right).toFixed(2) };
  };
  return { main: g('oiChart'), delta: g('oiDeltaChart'), roll: g('oiRollChart') };
});
console.log('\n--- X 轴对齐（chartArea 左/右边距）---');
console.log(JSON.stringify(align));
const dl = [align.delta.left - align.main.left, align.roll.left - align.main.left];
const dr = [align.delta.right - align.main.right, align.roll.right - align.main.right];
console.log(`  左边距偏差: delta ${dl[0]}px  roll ${dl[1]}px`);
console.log(`  右边距偏差: delta ${dr[0]}px  roll ${dr[1]}px`);

// 回到最新帧截图
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
  path: path.join(__dirname, '..', 'screenshots', 'live-playback.png'),
  clip: { x: 100, y: top.y - 70, width: 1200, height: (bot.y + bot.height) - (top.y - 70) + 16 },
});
console.log('\nscreenshot -> screenshots/live-playback.png');
console.log('page errors:', errs.length ? errs : 'none');

await browser.close();
