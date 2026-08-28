import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const python = fs.readFileSync(path.join(ROOT, 'derive_fiscal_risk_monitor.py'), 'utf8');
const pageGuard = fs.readFileSync(
  path.join(ROOT, 'tools', 'verify-fiscal-risk-monitor-page.mjs'), 'utf8');

let passed = 0;
let failed = 0;
function check(name, ok) {
  if (ok) {
    passed++;
    console.log(`PASS ${name}`);
  } else {
    failed++;
    console.log(`FAIL ${name}`);
  }
}

const fixedProductionSnapshot = [
  /(?:^|[^A-Za-z0-9_])data\["latest_observed_quarter"\]\s*==\s*["']\d{4}-Q[1-4]["']/m,
  /(?:^|[^A-Za-z0-9_])data\["latest_complete_quarter"\]\s*==\s*["']\d{4}-Q[1-4]["']/m,
  /(?:^|[^A-Za-z0-9_])data\["complete_lag_quarters"\]\s*==\s*\d+/m,
];
check('C18C production snapshot contract has no fixed quarter or lag comparison',
  fixedProductionSnapshot.every(pattern => !pattern.test(python)));
check('C18C latest observed expectation comes from final source row',
  python.includes('expected_latest_observed = source_rows[-1]["quarter"]'));
check('C18C latest complete expectation filters source complete/core-valid rows',
  python.includes('expected_complete_source_rows = [')
  && python.includes('all(row.get(field) is not None for field in CORE_FIELDS)'));
check('C18C lag expectation uses quarter indices',
  python.includes('expected_lag = (_quarter_index(expected_latest_observed)')
  && python.includes('- _quarter_index(expected_latest_complete))'));
check('C18C rolling fixtures cover zero and positive lag',
  python.includes('rolling fixture latest observed equals latest complete with lag zero')
  && python.includes('rolling fixture new incomplete quarter increases lag dynamically'));

const fixedPageCondition = /state\.values\.risk(?:Debt|Gap|RMinusG|Primary)Condition\.includes\(\s*["']/;
check('C18C page condition guard maps derived states instead of fixed visible snapshot text',
  pageGuard.includes('expectedConditions(latest)') && !fixedPageCondition.test(pageGuard));
check('C18C hover locates latest complete quarter by label',
  pageGuard.includes('chart.data.labels.indexOf(latestCompleteQuarter)')
  && pageGuard.includes('monitor.latest_complete_quarter')
  && !/chart\.data\.labels\.length\s*-\s*\d+/.test(pageGuard));
check('C18C reverse page fixture covers opposite condition states',
  pageGuard.includes('反向 condition fixture 证明页面映射不依赖当前 production snapshot'));

console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
