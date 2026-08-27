(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.CboScenarioEngine = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const LIMITS = Object.freeze({
    startYear: Object.freeze([2026, 2036]),
    growthShockPp: Object.freeze([-3, 3]),
    primaryBalanceShockPp: Object.freeze([-3, 3]),
    netInterestSpendingShockPp: Object.freeze([-2, 2]),
  });
  const ZERO_CONFIG = Object.freeze({
    startYear: 2026,
    growthShockPp: 0,
    primaryBalanceShockPp: 0,
    netInterestSpendingShockPp: 0,
  });

  function finite(value, name) {
    if (typeof value !== 'number' || !Number.isFinite(value)) {
      throw new TypeError(`${name} 必须是有限数值`);
    }
    return value;
  }

  function normalizeConfig(input) {
    const config = { ...ZERO_CONFIG, ...(input || {}) };
    if (!Number.isInteger(config.startYear)
        || config.startYear < LIMITS.startYear[0]
        || config.startYear > LIMITS.startYear[1]) {
      throw new RangeError('startYear 必须是 2026–2036 的整数');
    }
    for (const [name, bounds] of Object.entries({
      growthShockPp: LIMITS.growthShockPp,
      primaryBalanceShockPp: LIMITS.primaryBalanceShockPp,
      netInterestSpendingShockPp: LIMITS.netInterestSpendingShockPp,
    })) {
      const value = finite(config[name], name);
      if (value < bounds[0] || value > bounds[1]) {
        throw new RangeError(`${name} 超出 [${bounds.join(', ')}]`);
      }
      config[name] = value;
    }
    return config;
  }

  function validateBasis(basis) {
    if (!basis || typeof basis !== 'object' || !Array.isArray(basis.annual)) {
      throw new TypeError('scenario basis annual 缺失');
    }
    if (basis.methodology?.no_forward_fiscal_gap !== true
        || basis.methodology?.official_baseline_is_read_only !== true) {
      throw new TypeError('scenario basis methodology contract 非法');
    }
    if (basis.annual.length !== 12
        || basis.annual[0]?.year !== 2025
        || basis.annual.at(-1)?.year !== 2036) {
      throw new RangeError('scenario basis 必须连续覆盖 2025–2036');
    }
    const fields = [
      'baseline_debt_bn', 'baseline_debt_pct_gdp', 'baseline_gdp_bn',
      'baseline_primary_balance_pct_gdp', 'baseline_net_interest_pct_gdp',
      'baseline_overall_balance_pct_gdp',
    ];
    basis.annual.forEach((row, index) => {
      if (row.year !== 2025 + index) throw new RangeError('scenario basis year 不连续');
      fields.forEach(field => finite(row[field], `${row.year} ${field}`));
      if (index) {
        finite(row.baseline_nominal_g_pct, `${row.year} baseline_nominal_g_pct`);
        finite(row.baseline_sfa_bn, `${row.year} baseline_sfa_bn`);
        finite(row.baseline_sfa_pct_gdp, `${row.year} baseline_sfa_pct_gdp`);
      }
    });
    return basis;
  }

  function isZeroShock(config) {
    return config.growthShockPp === 0
      && config.primaryBalanceShockPp === 0
      && config.netInterestSpendingShockPp === 0;
  }

  function runScenario(rawBasis, rawConfig) {
    const basis = validateBasis(rawBasis);
    const config = normalizeConfig(rawConfig);
    const zeroShock = isZeroShock(config);
    const rows = [];
    let previousDebt;
    let previousGdp;

    basis.annual.forEach((source, index) => {
      const beforeStart = source.year < config.startYear;
      if (index === 0 || beforeStart) {
        const row = {
          year: source.year,
          kind: source.kind,
          baselineDebtBn: source.baseline_debt_bn,
          baselineDebtPctGdp: source.baseline_debt_pct_gdp,
          scenarioDebtBn: source.baseline_debt_bn,
          scenarioDebtPctGdp: source.baseline_debt_pct_gdp,
          scenarioGdpBn: source.baseline_gdp_bn,
          scenarioNominalGPct: source.baseline_nominal_g_pct,
          scenarioOverallBalancePctGdp: source.baseline_overall_balance_pct_gdp,
          scenarioSfaBn: source.baseline_sfa_bn,
          differencePp: 0,
          shockActive: false,
        };
        rows.push(row);
        previousDebt = row.scenarioDebtBn;
        previousGdp = row.scenarioGdpBn;
        return;
      }

      const scenarioGrowth = source.baseline_nominal_g_pct + config.growthShockPp;
      const scenarioGdp = zeroShock
        ? source.baseline_gdp_bn
        : previousGdp * (1 + scenarioGrowth / 100);
      const scenarioOverallBalance = source.baseline_overall_balance_pct_gdp
        + config.primaryBalanceShockPp - config.netInterestSpendingShockPp;
      const deficit = -scenarioOverallBalance / 100 * scenarioGdp;
      const sfa = source.baseline_sfa_pct_gdp / 100 * scenarioGdp;
      const computedDebt = previousDebt + deficit + sfa;

      if (zeroShock && Math.abs(computedDebt - source.baseline_debt_bn) > 1e-8) {
        throw new Error(`${source.year}: zero-shock debt closure failed`);
      }
      // Official ratios are published at three decimals.  After the amount-level
      // reconciliation succeeds, zero shock canonically returns those official
      // ratios instead of manufacturing extra precision from rounded inputs.
      const scenarioDebt = zeroShock ? source.baseline_debt_bn : computedDebt;
      const scenarioRatio = zeroShock
        ? source.baseline_debt_pct_gdp
        : scenarioDebt / scenarioGdp * 100;
      rows.push({
        year: source.year,
        kind: source.kind,
        baselineDebtBn: source.baseline_debt_bn,
        baselineDebtPctGdp: source.baseline_debt_pct_gdp,
        scenarioDebtBn: scenarioDebt,
        scenarioDebtPctGdp: scenarioRatio,
        scenarioGdpBn: scenarioGdp,
        scenarioNominalGPct: scenarioGrowth,
        scenarioOverallBalancePctGdp: scenarioOverallBalance,
        scenarioSfaBn: sfa,
        differencePp: scenarioRatio - source.baseline_debt_pct_gdp,
        shockActive: true,
      });
      previousDebt = scenarioDebt;
      previousGdp = scenarioGdp;
    });

    const projection = rows.filter(row => row.kind === 'projection');
    const terminal = projection.at(-1);
    return {
      config: { ...config },
      rows,
      summary: {
        terminalYear: terminal.year,
        terminalScenarioDebtPctGdp: terminal.scenarioDebtPctGdp,
        terminalBaselineDebtPctGdp: terminal.baselineDebtPctGdp,
        terminalDifferencePp: terminal.differencePp,
        peakScenarioDebtPctGdp: Math.max(...projection.map(row => row.scenarioDebtPctGdp)),
        terminalScenarioOverallBalancePctGdp: terminal.scenarioOverallBalancePctGdp,
      },
    };
  }

  function resetConfig() {
    return { ...ZERO_CONFIG };
  }

  return Object.freeze({ LIMITS, ZERO_CONFIG, normalizeConfig, runScenario, resetConfig });
}));
