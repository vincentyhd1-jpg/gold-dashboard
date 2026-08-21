import { chromium } from 'playwright';
import path from 'path';
import { fileURLToPath } from 'url';
import { findChromiumExecutable } from './_browser.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const execPath = findChromiumExecutable();
if (!execPath) throw new Error('找不到 Playwright Chromium chrome.exe');
const browser = await chromium.launch({ headless: true, executablePath: execPath });

const page = await browser.newPage();
await page.setContent('<!doctype html>');
await page.addScriptTag({ path: path.join(__dirname, '..', 'js', 'data-helpers.js') });

const names = [
  'cot.json',
  'gold_price.json',
  'stocks.json',
  'oi.json',
  'term-structure-series.json',
];

let pass = 0;
let fail = 0;
for (const name of names) {
  const result = await page.evaluate(([name]) => {
    const payload = { SENTINEL: true };
    const cases = {
      'cot.json': p => window.unwrapEnvelope(p, 'cot.json', true, q => ({ ...q.data, generated_at: q.generated_at })),
      'gold_price.json': p => window.unwrapEnvelope(p, 'gold_price.json', true),
      'stocks.json': p => window.unwrapEnvelope(p, 'stocks.json', true),
      'oi.json': p => window.unwrapEnvelope(p, 'oi.json', true),
      'term-structure-series.json': p => window.unwrapEnvelope(p, 'term-structure-series.json', true),
    };
    try {
      cases[name](payload);
      return { threw: false, message: '' };
    } catch (err) {
      return { threw: true, message: err.message };
    }
  }, [name]);
  const ok = result.threw && result.message === `${name}: 期望信封格式`;
  if (ok) {
    pass++;
    console.log(`PASS ${name} 裸输入抛错: ${result.message}`);
  } else {
    fail++;
    console.log(`FAIL ${name} 裸输入未正确抛错: ${JSON.stringify(result)}`);
  }
}

await browser.close();
console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
