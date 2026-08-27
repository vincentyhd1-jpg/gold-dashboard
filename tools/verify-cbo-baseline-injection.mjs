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

const overwriteAnchor = `        if _comparable(existing_vintage) != _comparable(output):
            raise CboBaselineFailure(
                f"拒绝覆盖 immutable vintage: {vintage_path.name}")
        canonical = existing_vintage`;
const overwriteBroken = `        if _comparable(existing_vintage) != _comparable(output):
            writer(str(vintage_path), output, compact=False)
        canonical = output`;
const parserCases = [
  {
    name: 'primary deficit kept positive',
    anchor: '        return -number\n    if source_sign == "deficit_negative":',
    changed: '        return number\n    if source_sign == "deficit_negative":',
    marker: 'FAIL primary deficit 符号统一为 surplus-positive',
  },
  {
    name: 'percent fraction guard relaxed',
    anchor: '                              low=50, high=250)',
    changed: '                              low=0, high=250)',
    marker: 'FAIL 118% 误读为 1.18% 必须拒绝',
  },
  {
    name: 'immutable vintage overwritten',
    anchor: overwriteAnchor,
    changed: overwriteBroken,
    marker: 'FAIL 旧 vintage 永不覆盖',
  },
  {
    name: 'CBO rate declared compatible with historical effective r',
    anchor: '        "forward_fiscal_gap_available": False,',
    changed: '        "forward_fiscal_gap_available": True,',
    marker: 'FAIL CBO forward Fiscal Gap 未强行构造',
  },
].map(item => ({
  name: item.name,
  patch: replacement(item.anchor, item.changed, item.name),
  verifyPatch: changedAnchor(item.anchor, item.changed),
  expectedFailureMarkers: [item.marker],
}));

const parserResult = await runInjectionSuite({
  name: 'C18A parser and vintage injection',
  target: 'fetch_cbo_baseline.py',
  guard: 'tools/verify-cbo-baseline-python.mjs',
  timeoutMs: 180_000,
  cases: parserCases,
});

const frontendCases = [
  {
    name: 'CBO projection visual distinction removed',
    anchor: "          sourceKind: 'cbo_official_baseline', borderDash: [8, 5], borderWidth: 2.5,",
    changed: "          sourceKind: 'cbo_official_baseline', borderDash: [], borderWidth: 2.5,",
    marker: 'FAIL actual 实线与 projection 虚线视觉区分',
  },
  {
    name: 'official CBO debt baseline recomputed in frontend',
    anchor: '    row.debt_held_by_public_pct_gdp]));',
    changed: '    (row.debt_held_by_public_bn / row.nominal_gdp_bn) * 100]));',
    marker: 'FAIL 前端没有重算官方 CBO debt baseline',
  },
].map(item => ({
  name: item.name,
  patch: replacement(item.anchor, item.changed, item.name),
  verifyPatch: changedAnchor(item.anchor, item.changed),
  expectedFailureMarkers: [item.marker],
}));

const frontendResult = await runInjectionSuite({
  name: 'C18A baseline frontend injection',
  target: 'macro.html',
  guard: 'tools/verify-cbo-baseline-page.mjs',
  timeoutMs: 180_000,
  cases: frontendCases,
});

const result = { ok: parserResult.ok && frontendResult.ok };
process.exitCode = result.ok ? 0 : 1;
