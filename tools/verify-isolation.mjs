import { chromium } from 'playwright';

const execPath = 'C:\\Users\\vince\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const browser = await chromium.launch({ headless: true, executablePath: execPath });

// Case 1: make renderDepotTrend throw — 期限结构/播放条 应照常渲染
async function run(label, patch) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('[pageerror] ' + e.message));

  // 拦 root HTML 与拆出的 js/*.js：被注入的函数可能定义在任一文件里
  // （initOIPlayback 已移到 js/playback.js，只拦 root 会让注入变成空操作）。
  await page.route(/^http:\/\/localhost:3001\/(|js\/[\w-]+\.js)$/, async route => {
    const response = await route.fetch();
    let body = await response.text();
    body = patch(body);
    await route.fulfill({ response, body });
  });

  // 不用 networkidle：Chart.js 走 CDN，网络不畅时该事件永不触发（实测超时 30s）。
  // 这里也不能等某个具体图表实例 —— 本脚本故意注入渲染故障，被打掉的图表
  // 本就不会出现。改等 footerNote 被改写：它在 .then 与 .catch 的开头都会执行，
  // 是「渲染流程已跑完」的可靠信号。
  await page.goto('http://localhost:3001', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(
    () => typeof Chart !== 'undefined'
      && !/每周六自动更新/.test(document.getElementById('footerNote')?.textContent || ''),
    { timeout: 60000 });
  await page.waitForTimeout(1200);

  const state = await page.evaluate(() => ({
    footer: document.getElementById('footerNote')?.textContent.slice(0, 30),
    playDate: document.getElementById('oiPlayDate')?.textContent,
    sliderMax: document.getElementById('oiPlaySlider')?.max,
    front: document.getElementById('oiFrontMonth')?.textContent,
    // Chart.js 实例存在即视为该模块渲染成功
    cotDual:    !!Chart.getChart('cotDualChart'),
    cotIndex:   !!Chart.getChart('cotIndexChart'),
    stocks:     !!Chart.getChart('stocksChart'),
    depotTrend: !!Chart.getChart('depotTrendChart'),
    oiMain:     !!Chart.getChart('oiChart'),
    oiDelta:    !!Chart.getChart('oiDeltaChart'),
    oiRoll:     !!Chart.getChart('oiRollChart'),
  }));

  console.log(`\n=== ${label} ===`);
  console.log('state  :', JSON.stringify(state));
  console.log('errors :', errors.length ? errors.map(e => e.slice(0, 90)) : 'none');
  await page.close();
}

// 1. 无注入：基线
await run('baseline (no injection)', b => b);

// 2. 让 renderDepotTrend 抛异常
await run('renderDepotTrend throws', b =>
  b.replace(
    'function renderDepotTrend(',
    'function renderDepotTrend() { throw new Error("INJECTED depot trend failure"); }\nfunction _unused_renderDepotTrend('
  ));

// 3. 让 initOIPlayback 抛异常（播放条坏掉，但 COT 图应照常）
await run('initOIPlayback throws', b =>
  b.replace(
    'function initOIPlayback(',
    'function initOIPlayback() { throw new Error("INJECTED playback failure"); }\nfunction _unused_initOIPlayback('
  ));

await browser.close();
