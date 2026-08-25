// C12 债务总览的三条核心约束必须能被真实破坏证伪：比例轴不能绑到金额轴，
// foreign 不能与完整 public 重复堆叠，GDP 金额线不能消失。
import { replaceExactly, runInjectionSuite } from './_injection.mjs';

function replaceAnchor(anchor, replacement, label) {
  return original => replaceExactly(
    original.toString('utf8'), anchor, replacement, label);
}

function verifyReplacement(anchor, replacement) {
  return (original, patched) => {
    const before = original.toString('utf8');
    const after = patched.toString('utf8');
    return {
      ok: before.includes(anchor) && !after.includes(anchor) && after.includes(replacement),
      detail: replacement.trim(),
    };
  };
}

const ratioAxisAnchor =
  "          type: 'line', yAxisID: ratioAxis, order: 0, sourceField: 'debt_gdp_pct',";
const ratioAxisBroken =
  "          type: 'line', yAxisID: amountAxis, order: 0, sourceField: 'debt_gdp_pct',";
const domesticAnchor =
  "          label: '本国公众持有', data: rows.map(r => r.domestic_public_bn),";
const duplicatePublic =
  "          label: '本国公众持有', data: rows.map(r => r.domestic_public_bn === null || r.foreign_bn === null ? null : r.domestic_public_bn + r.foreign_bn),";
const gdpDataset = `        lineDataset('美国名义 GDP', rows.map(r => r.gdp_bn), COLORS.green, {
          type: 'line', yAxisID: amountAxis, order: 0, sourceField: 'gdp_bn',
          stack: 'gdpLine', borderWidth: 2.5, tension: 0, pointStyle: 'line',
        }),
`;

const result = await runInjectionSuite({
  name: 'C12 debt overview injection wrapper',
  target: 'macro.html',
  guard: 'tools/verify-macro-page.mjs',
  timeoutMs: 180_000,
  cases: [
    {
      name: 'debt/GDP bound to amount axis',
      patch: replaceAnchor(ratioAxisAnchor, ratioAxisBroken, 'debt/GDP 轴注入锚点'),
      verifyPatch: verifyReplacement(ratioAxisAnchor, ratioAxisBroken),
      expectedFailureMarkers: ['FAIL 联邦债务/GDP 为右轴比例折线'],
    },
    {
      name: 'domestic stack replaced by full public debt',
      patch: replaceAnchor(domesticAnchor, duplicatePublic, '公众持有重复堆叠注入锚点'),
      verifyPatch: verifyReplacement(domesticAnchor, duplicatePublic),
      expectedFailureMarkers: ['FAIL 债务总览 dataset domestic_public_bn 逐点直读派生字段'],
    },
    {
      name: 'GDP dataset removed',
      patch: replaceAnchor(gdpDataset, '', 'GDP dataset 注入锚点'),
      verifyPatch: verifyReplacement(gdpDataset, ''),
      expectedFailureMarkers: ['FAIL 债务总览恰有六个目标 dataset'],
    },
  ],
});

process.exitCode = result.ok ? 0 : 1;
