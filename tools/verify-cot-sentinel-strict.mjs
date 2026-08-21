import { chromium } from 'playwright';

const execPath = String.raw`C:\Users\vince\AppData\Local\ms-playwright\chromium-1234\chrome-win64\chrome.exe`;
const browser = await chromium.launch({ headless: true, executablePath: execPath });
const page = await browser.newPage({ viewport: { width: 1400, height: 1200 } });

const errors = [];
let routeHit = false;
let sentinelServed = false;
page.on('pageerror', e => errors.push(e.message));
page.on('console', m => {
  if (m.type() === 'error') errors.push('[console] ' + m.text());
});

await page.route('**/data/cot.json*', async route => {
  routeHit = true;
  sentinelServed = true;
  await route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ SENTINEL: true, latest: { date: '2099-01-01' }, weekly: [] }),
  });
});

await page.goto('http://localhost:3001', { waitUntil: 'domcontentloaded', timeout: 60000 });
await page.waitForTimeout(1500);

const state = await page.evaluate(() => ({
  footer: document.getElementById('footerNote')?.textContent || '',
  cotDate: document.getElementById('cotDate')?.textContent || '',
  signalDate: document.getElementById('signalDate')?.textContent || '',
  bodyHasSentinel: document.body.innerText.includes('SENTINEL') || document.body.innerText.includes('2099-01-01'),
}));

const expectedError = errors.some(e => e.includes('cot.json: 期望信封格式'));
const usedMock = state.footer.includes('当前显示模拟数据');
const sentinelBlocked = !state.bodyHasSentinel && !state.cotDate.includes('2099-01-01') && !state.signalDate.includes('2099-01-01');

let pass = 0;
let fail = 0;
const check = (name, ok, detail = '') => {
  if (ok) { pass++; console.log(`PASS ${name}`); }
  else { fail++; console.log(`FAIL ${name} ${JSON.stringify(detail)}`); }
};

check('cot.json 路由注入已命中', routeHit && sentinelServed, { routeHit, sentinelServed });
check('裸格式 strict 报错已出现', expectedError, errors);
check('页面进入必需数据失败兜底', usedMock, state.footer);
check('SENTINEL 未进入页面数据', sentinelBlocked, state);

await browser.close();
console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
