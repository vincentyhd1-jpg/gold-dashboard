import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { replaceExactly, runInjectionSuite, sha256 } from './_injection.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const monitorPath = path.join(ROOT, 'data', 'derived', 'fiscal_risk_monitor.json');
const sourcePath = path.join(ROOT, 'data', 'derived', 'macro_fiscal_stress.json');
const monitorHash = sha256(fs.readFileSync(monitorPath));
const sourceHash = sha256(fs.readFileSync(sourcePath));

function exactCase(name, anchor, changed, marker) {
  return {
    name,
    patch: original => replaceExactly(original.toString('utf8'), anchor, changed, name),
    verifyPatch: (original, patched) => ({
      ok: original.toString('utf8').includes(anchor)
        && !patched.toString('utf8').includes(anchor)
        && patched.toString('utf8').includes(changed),
      detail: changed.trim().slice(0, 120),
    }),
    expectedFailureMarkers: [marker],
  };
}

const pythonResult = await runInjectionSuite({
  name: 'C18C fiscal risk monitor derivation injections',
  target: 'derive_fiscal_risk_monitor.py',
  guard: 'tools/verify-fiscal-risk-monitor-python.mjs',
  timeoutMs: 180_000,
  cases: [
    exactCase(
      'A YoY uses current quarter instead of same quarter prior year',
      '        prior = by_quarter.get(f"{year - 1}-Q{number}")',
      '        prior = by_quarter.get(f"{year}-Q{number}")',
      'FAIL debt_gdp_yoy_change_pp same-quarter YoY'),
    exactCase(
      'B missing prior year becomes zero',
      '                and prior_value is not None\n                else None\n            )',
      '                and prior_value is not None\n                else 0.0\n            )',
      'FAIL missing same-quarter prior produces null, never zero'),
    exactCase(
      'C r-minus-g sign condition inverted',
      '        current["r_minus_g_condition"] = _condition(\n'
        + '            row["r_minus_g_pct_points"], "positive", "zero", "negative")',
      '        current["r_minus_g_condition"] = _condition(\n'
        + '            row["r_minus_g_pct_points"], "negative", "zero", "positive")',
      'FAIL production conditions dynamically follow current signs'),
    exactCase(
      'D fiscal-gap nonpositive condition mislabeled positive',
      '        current["fiscal_gap_condition"] = _condition(\n'
        + '            row["fiscal_gap_pct_gdp"], "gap_positive", "gap_nonpositive",\n'
        + '            "gap_nonpositive")',
      '        current["fiscal_gap_condition"] = _condition(\n'
        + '            row["fiscal_gap_pct_gdp"], "gap_positive", "gap_positive",\n'
        + '            "gap_positive")',
      'FAIL production conditions dynamically follow current signs'),
    exactCase(
      'E risk score injected into production rows',
      '        current = copy.deepcopy(row)\n        year, number = _quarter_parts(row["quarter"])',
      '        current = copy.deepcopy(row)\n        current["risk_score"] = 50\n        year, number = _quarter_parts(row["quarter"])',
      'FAIL output has no scoring fields'),
  ],
});

const pageResult = await runInjectionSuite({
  name: 'C18C fiscal risk monitor page injections',
  target: 'macro.html',
  guard: 'tools/verify-fiscal-risk-monitor-page.mjs',
  timeoutMs: 180_000,
  cases: [
    exactCase(
      'F debt threshold HIGH RISK added',
      '<div class="risk-monitor-kpi-label">公众持有债务 / GDP</div>',
      '<div class="risk-monitor-kpi-label">公众持有债务 / GDP · HIGH RISK &gt; 100%</div>',
      'FAIL 页面没有 C18C 风险分数/动态阈值/危机判定'),
    exactCase(
      'G DGS10 mislabeled as effective r for forward dynamics',
      'DGS10<strong id="riskDgs10">',
      'DGS10（effective r，用于前向债务动力学）<strong id="riskDgs10">',
      'FAIL 市场利率明确不替代 effective r 或进入前向动力学'),
    exactCase(
      'H no-score no-probability disclaimer removed',
      '<div class="risk-monitor-note" id="riskMonitorDisclaimer">本模块是多指标描述性监测，不是综合风险评分，不估计危机概率或危机年份。缺失数据保持未知，不补点、不插值、不归零。</div>',
      '<div class="risk-monitor-note" id="riskMonitorDisclaimer"></div>',
      'FAIL 免责声明明确非评分/概率/危机年份且缺失不补齐'),
  ],
});

const staleResult = await runInjectionSuite({
  name: 'C18C committed monitor freshness injection',
  target: 'data/derived/macro_fiscal_stress.json',
  guard: 'tools/verify-fiscal-risk-monitor-python.mjs',
  timeoutMs: 180_000,
  cases: [{
    name: 'I current fiscal source changes without regenerating monitor',
    patch: original => {
      const payload = JSON.parse(original.toString('utf8'));
      const row = [...payload.data.quarterly].reverse().find(item =>
        item.calculation_status === 'complete'
        && Number.isFinite(item.net_interest_receipts_pct));
      if (!row) throw new Error('no complete fiscal source row available for stale injection');
      row.net_interest_receipts_pct += 0.001;
      return Buffer.from(`${JSON.stringify(payload, null, 2)}\n`);
    },
    verifyPatch: (original, patched) => {
      const before = JSON.parse(original.toString('utf8'));
      const payload = JSON.parse(patched.toString('utf8'));
      const row = [...payload.data.quarterly].reverse().find(item =>
        item.calculation_status === 'complete'
        && Number.isFinite(item.net_interest_receipts_pct));
      const beforeRow = before.data.quarterly.find(item => item.quarter === row?.quarter);
      return { ok: row && beforeRow
          && Math.abs(row.net_interest_receipts_pct
            - beforeRow.net_interest_receipts_pct - 0.001) < 1e-12,
        detail: `${row?.quarter || '<missing>'}=${row?.net_interest_receipts_pct}` };
    },
    expectedFailureMarkers: [
      'FAIL committed fiscal risk monitor is stale relative to current fiscal stress source',
    ],
  }],
});

const snapshotPythonResult = await runInjectionSuite({
  name: 'C18C Python snapshot-coupling injection',
  target: 'derive_fiscal_risk_monitor.py',
  guard: 'tools/verify-fiscal-risk-monitor-snapshot-contract.mjs',
  cases: [exactCase(
    'J production latest-observed test re-bound to a fixed quarter',
    '    expected_latest_observed = source_rows[-1]["quarter"]',
    '    expected_latest_observed = "2026-Q2"',
    'FAIL C18C latest observed expectation comes from final source row')],
});

const snapshotPageResult = await runInjectionSuite({
  name: 'C18C page hover snapshot-coupling injection',
  target: 'tools/verify-fiscal-risk-monitor-page.mjs',
  guard: 'tools/verify-fiscal-risk-monitor-snapshot-contract.mjs',
  cases: [exactCase(
    'K hover re-bound to current one-quarter lag by array position',
    '  const index = chart.data.labels.indexOf(latestCompleteQuarter);',
    '  const index = chart.data.labels.length - 2;',
    'FAIL C18C hover locates latest complete quarter by label')],
});

const sourceRestored = sha256(fs.readFileSync(sourcePath)) === sourceHash;
const monitorRestored = sha256(fs.readFileSync(monitorPath)) === monitorHash;
console.log(`${sourceRestored ? 'PASS' : 'FAIL'} C18C source SHA-256 restored`);
console.log(`${monitorRestored ? 'PASS' : 'FAIL'} C18C monitor SHA-256 unchanged`);

const result = { ok: pythonResult.ok && pageResult.ok && staleResult.ok
  && snapshotPythonResult.ok && snapshotPageResult.ok
  && sourceRestored && monitorRestored };
process.exitCode = result.ok ? 0 : 1;
