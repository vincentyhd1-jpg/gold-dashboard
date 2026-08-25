import path from 'path';
import { fileURLToPath } from 'url';
import { launchChromium } from './_browser.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const browser = await launchChromium();

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

const validEnvelope = data => ({
  schema_version: 0,
  source: 'fixture',
  freq: 'daily',
  generated_at: '2026-01-01T00:00:00Z',
  date_field: 'date',
  coverage: { first: null, last: null, count: 0 },
  derived_from: [],
  warnings: [],
  info: [],
  data,
});

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
  const ok = result.threw && result.message.startsWith(`${name}: 期望信封格式`);
  if (ok) {
    pass++;
    console.log(`PASS ${name} 裸输入抛错: ${result.message}`);
  } else {
    fail++;
    console.log(`FAIL ${name} 裸输入未正确抛错: ${JSON.stringify(result)}`);
  }
}

const contractCases = await page.evaluate(valid => {
  const probe = payload => {
    try {
      return { threw: false, value: window.unwrapEnvelope(payload, 'contract.json', true) };
    } catch (err) {
      return { threw: true, message: err.message };
    }
  };
  const future = { ...valid, schema_version: 999 };
  const missing = { ...valid };
  delete missing.coverage;
  return { valid: probe(valid), future: probe(future), missing: probe(missing) };
}, validEnvelope({ rows: [1, 2] }));

const contractChecks = [
  ['合法 schema v0 信封正常解包',
   !contractCases.valid.threw && contractCases.valid.value.rows.length === 2,
   contractCases.valid],
  ['未知 schema_version 被拒绝',
   contractCases.future.threw && contractCases.future.message.includes('未知 schema_version=999'),
   contractCases.future],
  ['缺 required key 被拒绝',
   contractCases.missing.threw && contractCases.missing.message.includes('coverage'),
   contractCases.missing],
];
for (const [label, ok, detail] of contractChecks) {
  if (ok) { pass++; console.log(`PASS ${label}`); }
  else { fail++; console.log(`FAIL ${label}: ${JSON.stringify(detail)}`); }
}

await browser.close();
console.log(`${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
