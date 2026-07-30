import { chromium } from 'playwright';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const execPath = 'C:\\Users\\vince\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';

const browser = await chromium.launch({ headless: true, executablePath: execPath });
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });

const errs = [];
page.on('pageerror', e => errs.push(e.message));
page.on('console', m => { if (m.type() === 'error') errs.push('[console] ' + m.text()); });

// 不用 networkidle：Chart.js 走 CDN，网络不畅时该事件永不触发（实测超时 30s）。
// 直接等图表实例就绪。
await page.goto('http://localhost:3001', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForFunction(
  () => typeof Chart !== 'undefined' && Chart.getChart('oiChart'),
  { timeout: 60000 });
await page.waitForSelector('#oiPlaybar');
await page.waitForTimeout(800);

const read = () => page.evaluate(() => ({
  date: document.getElementById('oiPlayDate').textContent,
  slider: document.getElementById('oiPlaySlider').value,
  max: document.getElementById('oiPlaySlider').max,
  btn: document.getElementById('oiPlayBtn').textContent.trim(),
  front: document.getElementById('oiFrontMonth').textContent,
  oiVal: document.getElementById('oiVal').textContent,
}));

console.log('--- initial (should be last frame 07/27) ---');
console.log(JSON.stringify(await read()));

// Drag slider to frame 0
await page.evaluate(() => {
  const s = document.getElementById('oiPlaySlider');
  s.value = 0;
  s.dispatchEvent(new Event('input', { bubbles: true }));
});
await page.waitForTimeout(400);
console.log('--- after slider -> 0 (should be 06/26) ---');
console.log(JSON.stringify(await read()));

// Click play, let it run a few frames
await page.click('#oiPlayBtn');
await page.waitForTimeout(1400);
const mid = await read();
console.log('--- ~1.4s into play (should be advancing, btn = pause) ---');
console.log(JSON.stringify(mid));

// Pause
await page.click('#oiPlayBtn');
await page.waitForTimeout(200);
const paused = await read();
console.log('--- after pause ---');
console.log(JSON.stringify(paused));

// Arrow key step
await page.evaluate(() => document.body.focus());
await page.keyboard.press('ArrowRight');
await page.waitForTimeout(250);
console.log('--- after ArrowRight (frame +1) ---');
console.log(JSON.stringify(await read()));
await page.keyboard.press('ArrowLeft');
await page.waitForTimeout(250);
console.log('--- after ArrowLeft (frame -1) ---');
console.log(JSON.stringify(await read()));

// Speed button（选中态用 aria-pressed，不是 .active class）
await page.click('.oi-speed-btns button[data-speed="2"]');
await page.waitForTimeout(150);
const speedActive = await page.evaluate(() =>
  [...document.querySelectorAll('.oi-speed-btns button')]
    .map(b => b.dataset.speed + (b.getAttribute('aria-pressed') === 'true' ? '*' : '')).join(' '));
console.log('--- speed buttons (2x should be active) ---');
console.log(speedActive);

// Reset to last frame, screenshot the whole OI block
await page.evaluate(() => {
  const s = document.getElementById('oiPlaySlider');
  s.value = s.max;
  s.dispatchEvent(new Event('input', { bubbles: true }));
});
await page.waitForTimeout(600);

await page.locator('#oiChart').scrollIntoViewIfNeeded();
await page.waitForTimeout(400);
const top = await page.locator('#oiChart').boundingBox();
const bot = await page.locator('#oiPlaybar').boundingBox();
await page.screenshot({
  path: path.join(__dirname, 'playback-controls.png'),
  clip: { x: 100, y: top.y - 70, width: 1200, height: (bot.y + bot.height) - (top.y - 70) + 16 },
});
console.log('\nscreenshot -> screenshots/playback-controls.png');
console.log('page errors:', errs.length ? errs : 'none');

await browser.close();
