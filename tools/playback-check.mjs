import { chromium } from 'playwright';
import path from 'path';
import { fileURLToPath } from 'url';
import fs from 'fs';

const execPath = 'C:\\Users\\vince\\AppData\\Local\\ms-playwright\\chromium-1234\\chrome-win64\\chrome.exe';
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ssDir = path.join(__dirname, '..', 'screenshots');
if (!fs.existsSync(ssDir)) fs.mkdirSync(ssDir, { recursive: true });

const browser = await chromium.launch({ headless: true, executablePath: execPath });
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
const page = await ctx.newPage();

const consoleMsgs = [];
const pageErrors = [];
page.on('console', m => consoleMsgs.push({ t: m.type(), s: m.text() }));
page.on('pageerror', e => pageErrors.push(e.message));

// Step 1: navigate
console.log('--- Navigating to http://localhost:3001/?v=3');
await page.goto('http://localhost:3001/?v=3', { waitUntil: 'load', timeout: 30000 });
console.log('--- Waiting 5 seconds');
await page.waitForTimeout(5000);

// Step 2: JS errors
if (pageErrors.length) {
  console.log('=== JS ERRORS ===');
  pageErrors.forEach(e => console.log(' ERROR:', e));
} else {
  console.log('=== No JS errors ===');
}
const warnErr = consoleMsgs.filter(m => m.t === 'error' || m.t === 'warning');
if (warnErr.length) {
  console.log('=== Console errors/warnings ===');
  warnErr.forEach(m => console.log(' [' + m.t + ']', m.s));
}

// Step 3: scroll to Term Structure section
const scrollInfo = await page.evaluate(() => {
  // Try by id
  for (const id of ['oi-section', 'oiSection', 'term-structure', 'termStructure']) {
    const el = document.getElementById(id);
    if (el) { el.scrollIntoView({ block: 'start' }); return 'by id: #' + id; }
  }
  // Try canvas oiChart parent
  const canvas = document.getElementById('oiChart');
  if (canvas) {
    let el = canvas.parentElement;
    while (el && el.tagName !== 'SECTION') el = el.parentElement;
    const target = el || canvas.closest('.section, [class*="section"], .card') || canvas.parentElement;
    if (target) { target.scrollIntoView({ block: 'start' }); return 'via oiChart parent: ' + target.tagName + '#' + (target.id||'?'); }
  }
  // Try heading text
  for (const h of document.querySelectorAll('h1,h2,h3,h4,h5,h6')) {
    if (h.textContent.includes('Term Structure') || h.textContent.includes('期限结构')) {
      h.scrollIntoView({ block: 'start' });
      return 'via heading: ' + h.textContent.slice(0, 40);
    }
  }
  return 'not found';
});
console.log('--- Scroll result:', scrollInfo);
await page.waitForTimeout(800);

// Step 4: screenshot the section
const initPath = path.join(ssDir, 'playback-initial.png');

// Find a bounding box around the whole OI block (charts + playbar + roll)
const ssBbox = await page.evaluate(() => {
  const ids = ['oiChart', 'oiDeltaChart', 'oiRollChart', 'oiPlaySlider', 'oiPlayBtn'];
  let minY = Infinity, maxY = -Infinity, minX = Infinity, maxX = -Infinity;
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    minY = Math.min(minY, r.top);
    maxY = Math.max(maxY, r.bottom);
    minX = Math.min(minX, r.left);
    maxX = Math.max(maxX, r.right);
  }
  if (minY === Infinity) return null;
  // expand a bit for padding
  return { x: Math.max(0, minX - 10), y: Math.max(0, minY - 40), width: maxX - minX + 20, height: maxY - minY + 80 };
});

if (ssBbox) {
  await page.screenshot({ path: initPath, clip: ssBbox });
  console.log('--- Initial screenshot saved (clipped):', initPath);
} else {
  await page.screenshot({ path: initPath, fullPage: false });
  console.log('--- Initial screenshot saved (viewport):', initPath);
}

// Step 5: diagnostic JS - accessing _play directly (not window._play, since it's const)
console.log('=== DIAGNOSTIC JS ===');
const diag = await page.evaluate(() => {
  const out = [];
  function L(...args) {
    const line = args.map(v => {
      if (v === undefined) return 'undefined';
      if (v === null) return 'null';
      return typeof v === 'object' ? JSON.stringify(v) : String(v);
    }).join(' ');
    out.push(line);
  }

  const mainChart = Chart.getChart('oiChart');
  const deltaChart = Chart.getChart('oiDeltaChart');
  const rollChart = Chart.getChart('oiRollChart');
  L('main chart exists:', !!mainChart);
  L('delta chart exists:', !!deltaChart);
  L('roll chart exists:', !!rollChart);

  if (mainChart) {
    // _play is a const - access via eval trick to read from global scope
    let play;
    try { play = eval('_play'); } catch(e) { play = null; }
    const s = play?.series;
    L('frameIdx:', play?.frameIdx);
    L('total frames:', s?.frames?.length);
    L('contracts:', s?.contracts?.join(','));

    const mca = mainChart.chartArea;
    const dca = deltaChart?.chartArea;
    if (dca) {
      L('left diff:', (mca.left - dca.left).toFixed(1), 'px');
      L('right diff:', ((mainChart.width - mca.right) - (deltaChart.width - dca.right)).toFixed(1), 'px');
    }
    const sc = s?.scale;
    L('scale:', JSON.stringify(sc));
    L('yPrice min:', mainChart.scales.yPrice?.min);
    L('yPrice max:', mainChart.scales.yPrice?.max);
    L('yOI max:', mainChart.scales.yOI?.max);
  }
  L('slider max:', document.getElementById('oiPlaySlider')?.max);
  L('play btn exists:', !!document.getElementById('oiPlayBtn'));
  return out;
});
diag.forEach(l => console.log(l));

// Step 6: click play
console.log('=== CLICKING PLAY ===');
const playBtn = await page.$('#oiPlayBtn');
if (playBtn) {
  await playBtn.click();
  console.log('Clicked #oiPlayBtn');
} else {
  console.log('#oiPlayBtn NOT FOUND');
  const btns = await page.$$eval('button', bs => bs.map(b => b.id + '|' + b.textContent.trim().slice(0,30)));
  console.log('All buttons:', btns.join(' | '));
}

await page.waitForTimeout(3000);

// check state after play
const afterState = await page.evaluate(() => {
  let play;
  try { play = eval('_play'); } catch(e) { play = null; }
  return {
    frameIdx: play?.frameIdx,
    playing: play?.playing,
    sliderValue: document.getElementById('oiPlaySlider')?.value,
    playBtnHTML: document.getElementById('oiPlayBtn')?.innerHTML,
    totalFrames: play?.series?.frames?.length,
  };
});
console.log('After 3s: frameIdx', afterState.frameIdx, '| playing', afterState.playing,
  '| sliderValue', afterState.sliderValue, '| totalFrames', afterState.totalFrames,
  '| playBtn HTML', afterState.playBtnHTML);

// Step 7: screenshot playing
const playPath = path.join(ssDir, 'playback-playing.png');
const ssBbox2 = await page.evaluate(() => {
  const ids = ['oiChart', 'oiDeltaChart', 'oiRollChart', 'oiPlaySlider', 'oiPlayBtn'];
  let minY = Infinity, maxY = -Infinity, minX = Infinity, maxX = -Infinity;
  for (const id of ids) {
    const el = document.getElementById(id);
    if (!el) continue;
    const r = el.getBoundingClientRect();
    minY = Math.min(minY, r.top); maxY = Math.max(maxY, r.bottom);
    minX = Math.min(minX, r.left); maxX = Math.max(maxX, r.right);
  }
  if (minY === Infinity) return null;
  return { x: Math.max(0, minX - 10), y: Math.max(0, minY - 40), width: maxX - minX + 20, height: maxY - minY + 80 };
});

if (ssBbox2) {
  await page.screenshot({ path: playPath, clip: ssBbox2 });
  console.log('--- Playing screenshot saved (clipped):', playPath);
} else {
  await page.screenshot({ path: playPath, fullPage: false });
  console.log('--- Playing screenshot saved (viewport):', playPath);
}

// Final error check
const finalErrs = consoleMsgs.filter(m => m.t === 'error');
console.log('=== FINAL CONSOLE ERRORS:', finalErrs.length, '===');
finalErrs.forEach(m => console.log(' [error]', m.s));

await browser.close();
console.log('Done.');
