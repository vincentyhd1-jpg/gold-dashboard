import { replaceExactly, runInjectionSuite } from './_injection.mjs';

function replacement(anchor, changed, label) {
  return original => replaceExactly(original.toString('utf8'), anchor, changed, label);
}

function changedAnchor(anchor, changed) {
  return (original, patched) => {
    const before = original.toString('utf8');
    const after = patched.toString('utf8');
    return { ok: before.includes(anchor) && !after.includes(anchor) && after.includes(changed),
      detail: changed.trim() };
  };
}

const fetchAnchor = '"receipts_bn": bn(receipts_total, "receipts"),';
const fetchBroken = '"receipts_bn": bn(outlays_total, "receipts"),';
const fetchResult = await runInjectionSuite({
  name: 'C17 MTS selection injection',
  target: 'fetch_treasury_fiscal.py',
  guard: 'tools/verify-fiscal-stress-python.mjs',
  timeoutMs: 180_000,
  cases: [{
    name: 'receipts total replaced by outlays total',
    patch: replacement(fetchAnchor, fetchBroken, 'MTS receipts selection'),
    verifyPatch: changedAnchor(fetchAnchor, fetchBroken),
    expectedFailureMarkers: ['FAIL hierarchy 区分两个 Total'],
  }],
});

const deriveCases = [
  {
    name: 'primary balance sign inverted',
    anchor: 'primary = overall + interest if fiscal else None',
    changed: 'primary = overall - interest if fiscal else None',
    marker: 'FAIL primary balance=receipts-outlays+interest',
  },
  {
    name: 'DGS10-like marginal rate substituted for effective r',
    anchor: 'effective_r = interest / average_public_debt * 100 if (',
    changed: 'effective_r = 10.0 if (',
    marker: 'FAIL effective r 只用公众债务分母',
  },
  {
    name: 'missing TTM month forward-filled',
    anchor: '    if any(row is None for row in rows):\n        return None, months',
    changed: '    if any(row is None for row in rows):\n'
      + '        rows = [row or next(item for item in rows if item is not None) for row in rows]',
    marker: 'FAIL 缺一个月整组 TTM 置 null',
  },
  {
    name: 'quarterly GDP divided by four',
    anchor: 'primary_pct = primary / gdp * 100 if (',
    changed: 'primary_pct = primary / (gdp / 4) * 100 if (',
    marker: 'FAIL GDP SAAR 未除以 4',
  },
  {
    name: 'p-star percent conversion removed',
    anchor: 'stabilizing = r_minus_g * public_d / 100 if (',
    changed: 'stabilizing = r_minus_g * public_d if (',
    marker: 'FAIL p* 包含 /100 单位换算',
  },
  {
    name: 'total debt substituted for public debt denominator',
    anchor: 'return sum(row["public_bn"] for row in valid) / len(valid), len(valid)',
    changed: 'return sum(row["total_bn"] for row in valid) / len(valid), len(valid)',
    marker: 'FAIL effective r 只用公众债务分母',
  },
].map(item => ({
  name: item.name,
  patch: replacement(item.anchor, item.changed, item.name),
  verifyPatch: changedAnchor(item.anchor, item.changed),
  expectedFailureMarkers: [item.marker],
}));

const deriveResult = await runInjectionSuite({
  name: 'C17 fiscal derivation injection',
  target: 'derive_fiscal_stress.py',
  guard: 'tools/verify-fiscal-stress-python.mjs',
  timeoutMs: 180_000,
  cases: deriveCases,
});

const frontendAnchor = "  for (const [id, field, suffix] of kpis) {";
const frontendBroken = `  latest.fiscal_gap_pct_gdp = latest.stabilizing_primary_balance_pct_gdp
    - latest.primary_balance_gdp_pct;
  for (const [id, field, suffix] of kpis) {`;
const frontendCases = [
  {
    name: 'fiscal gap decision sign inverted',
    anchor: "      title: '稳定条件不满足',",
    changed: "      title: '稳定条件满足',",
    marker: 'FAIL gap_positive 历史 fixture 显示稳定条件不满足',
  },
  {
    name: 'p-star criterion label removed',
    anchor: "lineDataset('稳定债务所需初级余额 p*（判据线）',",
    changed: "lineDataset('稳定债务所需初级余额 p*',",
    marker: 'FAIL p* dataset 标记为判据线且使用独立虚线样式',
  },
  {
    name: 'zero reference mislabeled as criterion',
    anchor: "lineDataset('0% GDP 参考线', rows.map(() => 0), COLORS.muted,",
    changed: "lineDataset('0% GDP 判据线', rows.map(() => 0), COLORS.muted,",
    marker: 'FAIL 0% GDP 只命名为参考线而非判据线',
  },
  {
    name: 'tooltip fiscal gap removed',
    anchor: '      `Fiscal Gap：${_formatFiscalGap(row.fiscal_gap_pct_gdp)}`,',
    changed: "      'Fiscal Gap：--',",
    marker: 'FAIL 最新季度真实 hover tooltip 包含 actual / p* / Fiscal Gap / 判决',
  },
].map(item => ({
  name: item.name,
  patch: replacement(item.anchor, item.changed, item.name),
  verifyPatch: changedAnchor(item.anchor, item.changed),
  expectedFailureMarkers: [item.marker],
}));
frontendCases.push({
  name: 'frontend fiscal gap arithmetic reintroduced',
  patch: replacement(frontendAnchor, frontendBroken, 'frontend gap binding'),
  verifyPatch: (original, patched) => {
    const before = original.toString('utf8');
    const after = patched.toString('utf8');
    const arithmetic = 'latest.fiscal_gap_pct_gdp = latest.stabilizing_primary_balance_pct_gdp';
    return { ok: !before.includes(arithmetic) && after.includes(arithmetic)
      && after.includes(frontendAnchor), detail: arithmetic };
  },
  expectedFailureMarkers: ['FAIL 前端没有 fiscal gap / p* / r / g 算术重算'],
});
const frontendResult = await runInjectionSuite({
  name: 'C17.1 fiscal decision frontend injection',
  target: 'macro.html',
  guard: 'tools/verify-fiscal-stress-page.mjs',
  timeoutMs: 180_000,
  cases: frontendCases,
});

const result = { ok: fetchResult.ok && deriveResult.ok && frontendResult.ok };
process.exitCode = result.ok ? 0 : 1;
