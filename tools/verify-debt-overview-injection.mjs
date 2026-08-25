// C12/C14 债务总览核心约束必须能被真实破坏证伪：既保留双轴/结构保护，
// 又证明 drag、reset 与低频 gap 三条新增保护不是恒真。
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
  "          label: '结构快照 · 本国公众', data: quarterly('domestic_public_bn'),";
const duplicatePublic =
  "          label: '结构快照 · 本国公众', data: quarterly('public_bn'),";
const gdpDataset = `        lineDataset('美国名义 GDP', quarterlyPoints('gdp_bn'), COLORS.green, {
          type: 'line', yAxisID: amountAxis, order: 0, sourceField: 'gdp_bn',
          sourceFrequency: 'quarterly',
          stack: 'gdpLine', borderWidth: 2.5, tension: 0,
          pointRadius: 2, pointHoverRadius: 4, pointStyle: 'circle',
        }),
`;
const dragAnchor = '        enabled: finePointer,';
const dragDisabled = '        enabled: false,';
const resetHandler =
  "document.getElementById('debtResetZoom').addEventListener('click', resetDebtZoom);\n";
const quarterlyAnchor = `  const quarterly = field => labels.map(date => {
    const row = quarterlyByDate.get(date);
    return row && row[field] !== undefined ? row[field] : null;
  });`;
const quarterlyForwardFill = `  const quarterly = field => {
    let previous = null;
    return labels.map(date => {
      const row = quarterlyByDate.get(date);
      const value = row && row[field] !== undefined ? row[field] : null;
      if (value !== null) previous = value;
      return value === null ? previous : value;
    });
  };`;

function restoreInvisibleNullArrayRepresentation(original) {
  let patched = original.toString('utf8');
  for (const [field, label] of [
    ['foreign_bn', 'foreign 低频调用'],
    ['gdp_bn', 'GDP 低频调用'],
    ['debt_gdp_pct', 'debt/GDP 低频调用'],
  ]) {
    patched = replaceExactly(
      patched,
      `quarterlyPoints('${field}')`,
      `quarterly('${field}')`,
      label,
    ).toString('utf8');
  }
  patched = replaceExactly(
    patched,
    '          borderWidth: 1, pointRadius: 2, pointHoverRadius: 4, tension: 0,\n'
      + "          pointStyle: 'circle',",
    '          borderWidth: 1, pointRadius: 0, pointHoverRadius: 0, tension: 0,\n'
      + "          pointStyle: 'line',",
    'foreign pointRadius 注入锚点',
  ).toString('utf8');
  patched = replaceExactly(
    patched,
    "          pointRadius: 2, pointHoverRadius: 4, pointStyle: 'circle',",
    "          pointRadius: 0, pointHoverRadius: 0, pointStyle: 'line',",
    'GDP pointRadius 注入锚点',
  ).toString('utf8');
  patched = replaceExactly(
    patched,
    "          tension: 0, pointRadius: 2, pointHoverRadius: 4, pointStyle: 'circle',",
    "          tension: 0, pointRadius: 0, pointHoverRadius: 0, pointStyle: 'line',",
    'debt/GDP pointRadius 注入锚点',
  ).toString('utf8');
  return Buffer.from(patched, 'utf8');
}

function verifyInvisibleNullArrayRepresentation(_original, patched) {
  const text = patched.toString('utf8');
  return {
    ok: ["foreign_bn", "gdp_bn", "debt_gdp_pct"]
      .every(field => text.includes(`quarterly('${field}')`)
        && !text.includes(`quarterlyPoints('${field}')`))
      && (text.match(/pointRadius: 0/g) || []).length >= 3,
    detail: 'daily union + null arrays + spanGaps:false + pointRadius:0',
  };
}

const result = await runInjectionSuite({
  name: 'C14 debt overview injection wrapper',
  target: 'macro.html',
  guard: 'tools/verify-macro-page.mjs',
  timeoutMs: 180_000,
  cases: [
    {
      name: 'debt/GDP bound to amount axis',
      patch: replaceAnchor(ratioAxisAnchor, ratioAxisBroken, 'debt/GDP 轴注入锚点'),
      verifyPatch: verifyReplacement(ratioAxisAnchor, ratioAxisBroken),
      expectedFailureMarkers: ['FAIL 正式 debt/GDP 保持季度且绑定右轴'],
    },
    {
      name: 'domestic stack replaced by full public debt',
      patch: replaceAnchor(domesticAnchor, duplicatePublic, '公众持有重复堆叠注入锚点'),
      verifyPatch: verifyReplacement(domesticAnchor, duplicatePublic),
      expectedFailureMarkers: ['FAIL 低频 dataset structure_domestic_public_bn 只映射真实季度观测'],
    },
    {
      name: 'GDP dataset removed',
      patch: replaceAnchor(gdpDataset, '', 'GDP dataset 注入锚点'),
      verifyPatch: verifyReplacement(gdpDataset, ''),
      expectedFailureMarkers: ['FAIL 债务总览恰有九个 mixed-frequency dataset'],
    },
    {
      name: 'drag zoom disabled',
      patch: replaceAnchor(dragAnchor, dragDisabled, 'drag enabled 注入锚点'),
      verifyPatch: verifyReplacement(dragAnchor, dragDisabled),
      expectedFailureMarkers: ['FAIL drag zoom 只启用 X 轴'],
    },
    {
      name: 'reset click handler removed',
      patch: replaceAnchor(resetHandler, '', 'reset handler 注入锚点'),
      verifyPatch: verifyReplacement(resetHandler, ''),
      expectedFailureMarkers: ['FAIL 点击重置缩放恢复完整历史范围'],
    },
    {
      name: 'quarterly foreign and GDP forward-filled',
      patch: replaceAnchor(quarterlyAnchor, quarterlyForwardFill, '低频 forward-fill 注入锚点'),
      verifyPatch: verifyReplacement(quarterlyAnchor, quarterlyForwardFill),
      expectedFailureMarkers: ['FAIL 低频 dataset structure_intragov_bn 只映射真实季度观测'],
    },
    {
      name: 'low-frequency lines restored to invisible daily-union null arrays',
      patch: restoreInvisibleNullArrayRepresentation,
      verifyPatch: verifyInvisibleNullArrayRepresentation,
      expectedFailureMarkers: ['FAIL 低频折线 foreign_bn 只含真实 observation object'],
    },
  ],
});

process.exitCode = result.ok ? 0 : 1;
