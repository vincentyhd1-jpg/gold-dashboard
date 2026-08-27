import { replaceExactly, runInjectionSuite } from './_injection.mjs';

function makeCase(name, anchor, changed, marker) {
  return {
    name,
    patch: original => replaceExactly(original.toString('utf8'), anchor, changed, name),
    verifyPatch: (original, patched) => ({
      ok: original.toString('utf8').includes(anchor)
        && !patched.toString('utf8').includes(anchor)
        && patched.toString('utf8').includes(changed),
      detail: changed.trim(),
    }),
    expectedFailureMarkers: [marker],
  };
}

const engineCases = [
  makeCase(
    'primary balance shock sign reversed',
    '        + config.primaryBalanceShockPp - config.netInterestSpendingShockPp;',
    '        - config.primaryBalanceShockPp - config.netInterestSpendingShockPp;',
    'FAIL +1pp primary balance lowers terminal debt/GDP'),
  makeCase(
    'interest spending shock sign reversed',
    '        + config.primaryBalanceShockPp - config.netInterestSpendingShockPp;',
    '        + config.primaryBalanceShockPp + config.netInterestSpendingShockPp;',
    'FAIL +1pp net interest spending raises terminal debt/GDP'),
  makeCase(
    'SFA reconciliation removed',
    '      const computedDebt = previousDebt + deficit + sfa;',
    '      const computedDebt = previousDebt + deficit;',
    'zero-shock debt closure failed'),
  makeCase(
    'C17 effective_r enters C18B engine',
    '      const scenarioOverallBalance = source.baseline_overall_balance_pct_gdp\n',
    '      const effective_r = source.effective_r || 0;\n      const scenarioOverallBalance = source.baseline_overall_balance_pct_gdp + effective_r\n',
    'FAIL C18B engine 不读取 C17 effective_r'),
  makeCase(
    'scenario overwrites baseline presentation field',
    '        baselineDebtPctGdp: source.baseline_debt_pct_gdp,\n        scenarioDebtBn: scenarioDebt,',
    '        baselineDebtPctGdp: scenarioRatio,\n        scenarioDebtBn: scenarioDebt,',
    'FAIL scenario 不覆盖 baseline 字段'),
  makeCase(
    'zero shock no longer returns official ratio',
    '      const scenarioRatio = zeroShock\n        ? source.baseline_debt_pct_gdp\n        : scenarioDebt / scenarioGdp * 100;',
    '      const scenarioRatio = scenarioDebt / scenarioGdp * 100;',
    'FAIL zero shock 逐年精确复现官方 debt/GDP'),
];

const engineResult = await runInjectionSuite({
  name: 'C18B scenario engine injection',
  target: 'assets/js/cbo-scenario-engine.js',
  guard: 'tools/verify-cbo-scenario-engine.mjs',
  cases: engineCases,
});

const pageCases = [
  makeCase(
    'User Scenario mislabeled as CBO Projection',
    "        lineDataset('User Scenario / 用户情景', scenario, COLORS.blue, {",
    "        lineDataset('CBO Projection', scenario, COLORS.blue, {",
    'FAIL baseline 与 scenario 标签/来源不能混写'),
  makeCase(
    'scenario disclaimer deleted',
    '<div class="scenario-disclaimer" id="scenarioDisclaimer">该工具是基于 CBO Baseline 的确定性敏感性分析。用户输入的情景不是 CBO 预测，也不是发生概率估计。结果不代表债务危机、违约或所谓“失控年份”。Stock-flow adjustment 仅用于会计闭合，不是风险指标。</div>',
    '<div class="scenario-disclaimer" id="scenarioDisclaimer"></div>',
    'FAIL 免责声明锁定 deterministic/not CBO/probability/no crisis-year 语义'),
];

const pageResult = await runInjectionSuite({
  name: 'C18B Scenario Lab page injection',
  target: 'macro.html',
  guard: 'tools/verify-cbo-scenario-page.mjs',
  timeoutMs: 180_000,
  cases: pageCases,
});

const result = { ok: engineResult.ok && pageResult.ok };
process.exitCode = result.ok ? 0 : 1;
