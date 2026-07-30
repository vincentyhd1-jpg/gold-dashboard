// 前端对断层帧的处理：oi_chg 全 null + 移仓相关字段全 null。
// derive 已实测会产出这种帧（挖掉交易日时），但 UI 从未收到过。
import { chromium } from 'playwright';

const execPath = 'C:\\Users\\vince\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const browser = await chromium.launch({ headless: true, executablePath: execPath });
const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });

const errs = [];
page.on('pageerror', e => errs.push('[pageerror] ' + e.message));
page.on('console', m => { if (m.type() === 'error') errs.push('[console] ' + m.text()); });

// 拦截 series JSON，注入一个断层帧
await page.route('**/term-structure-series.json*', async route => {
  const res = await route.fetch();
  const s = await res.json();
  // 把第 5 帧改成断层帧：oi_chg 全 null，移仓相关字段全 null
  const i = 5;
  s.frames[i].oi_chg = s.frames[i].oi_chg.map(() => null);
  s.frames[i].front_remaining = null;
  s.frames[i].roll_noise = null;
  s.frames[i].roll_noise_ma = null;
  s.frames[i].roll_to = null;
  s.frames[i].unreliable_chg = null;
  await route.fulfill({ response: res, body: JSON.stringify(s) });
});

// 不用 networkidle：Chart.js 走 CDN，网络不畅时该事件永不触发（实测超时 30s）。
// 直接等图表实例就绪。
await page.goto('http://localhost:3001', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForFunction(
  () => typeof Chart !== 'undefined' && Chart.getChart('oiChart') && Chart.getChart('oiDeltaChart'),
  { timeout: 60000 });
await page.waitForSelector('#oiPlaybar', { timeout: 15000 });
await page.waitForTimeout(800);

const readAt = async idx => {
  await page.evaluate(i => {
    const s = document.getElementById('oiPlaySlider');
    s.value = i; s.dispatchEvent(new Event('input', { bubbles: true }));
  }, idx);
  await page.waitForTimeout(350);
  return page.evaluate(() => {
    const delta = Chart.getChart('oiDeltaChart');
    const ds = delta.data.datasets[0];
    return {
      date: document.getElementById('oiPlayDate').textContent,
      oiVal: document.getElementById('oiVal').textContent,
      front: document.getElementById('oiFrontMonth').textContent,
      deltaAllTransparent: ds.backgroundColor.every(c => c === 'transparent'),
      deltaData: ds.data.slice(0, 4),
      chartsAlive: {
        main: !!Chart.getChart('oiChart'),
        delta: !!delta,
        roll: !!Chart.getChart('oiRollChart'),
      },
    };
  });
};

console.log('--- 断层帧前 (idx 4) ---');
console.log(JSON.stringify(await readAt(4)));
console.log('\n--- 断层帧 (idx 5, oi_chg 全 null / front_remaining null) ---');
console.log(JSON.stringify(await readAt(5)));
console.log('\n--- 断层帧后 (idx 6) ---');
console.log(JSON.stringify(await readAt(6)));

// 播放穿过断层帧
await page.evaluate(() => {
  const s = document.getElementById('oiPlaySlider');
  s.value = 3; s.dispatchEvent(new Event('input', { bubbles: true }));
});
await page.waitForTimeout(300);
await page.click('#oiPlayBtn');
await page.waitForTimeout(1600);
await page.click('#oiPlayBtn');
console.log('\n--- 播放穿过断层帧后 ---');
console.log(JSON.stringify(await page.evaluate(() => ({
  date: document.getElementById('oiPlayDate').textContent,
  charts: ['oiChart','oiDeltaChart','oiRollChart'].map(id => !!Chart.getChart(id)),
}))));

console.log('\npage errors:', errs.length ? errs : 'none');
await browser.close();
process.exit(errs.length ? 1 : 0);
