// C14/C15 债务总览核心约束必须能被真实破坏证伪：既保留双轴、低频
// observation、drag/reset 保护，也证明堆叠、用户文案与统一 tooltip 不是恒真。
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
      ok: before.includes(anchor) && !after.includes(anchor)
        && (replacement === '' || after.includes(replacement)),
      detail: replacement.trim() || '(removed)',
    };
  };
}

const ratioAxisAnchor =
  "          type: 'line', yAxisID: ratioAxis, order: 0, sourceField: 'debt_gdp_pct',";
const ratioAxisBroken =
  "          type: 'line', yAxisID: amountAxis, order: 0, sourceField: 'debt_gdp_pct',";
const domesticAnchor =
  "          label: '国内公众持有', data: structurePoints('domestic_public_bn'),";
const duplicatePublic =
  "          label: '国内公众持有', data: structurePoints('public_bn'),";
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

const gdpTooltipLine =
  '        `美国名义 GDP（截至 ${gdpAsOf}）：${_formatDebtAmount(gdpRow?.gdp_bn)}`,\n';
const foreignStackAnchor =
  "          stack: structureStack, yAxisID: amountAxis, sourceField: 'foreign_bn',";
const foreignSplitStack =
  "          stack: 'foreignOnly', yAxisID: amountAxis, sourceField: 'foreign_bn',";
const intragovLabelAnchor =
  "          label: '政府内部持有', data: structurePoints('intragov_bn'),";
const technicalLabel =
  "          label: '结构快照 · 政府内部持有', data: structurePoints('intragov_bn'),";
const totalDataset = `        lineDataset('联邦债务总额', hybrid('total_bn'), COLORS.red, {
          type: 'line', yAxisID: amountAxis, order: 0, sourceField: 'total_bn',
          sourceFrequency: 'hybrid_quarterly_then_daily',
          stack: 'totalDebtLine', borderWidth: 2.5, tension: 0, pointStyle: 'line',
        }),
`;
const redundantDailyLines = totalDataset + `        lineDataset('公众持有（日频）', hybrid('public_bn'), COLORS.purple, {
          type: 'line', yAxisID: amountAxis, order: 0, sourceField: 'public_bn',
          sourceFrequency: 'hybrid_quarterly_then_daily', stack: 'publicDailyLine',
        }),
        lineDataset('政府内部持有（日频）', hybrid('intragov_bn'), COLORS.blue, {
          type: 'line', yAxisID: amountAxis, order: 0, sourceField: 'intragov_daily_bn',
          sourceFrequency: 'hybrid_quarterly_then_daily', stack: 'intragovDailyLine',
        }),
`;

function verifyRedundantDailyLines(original, patched) {
  const before = original.toString('utf8');
  const after = patched.toString('utf8');
  const labels = ['公众持有（日频）', '政府内部持有（日频）'];
  return {
    ok: labels.every(label => !before.includes(label) && after.includes(label))
      && after.includes(totalDataset),
    detail: labels.join(' + '),
  };
}

const structurePointsAnchor = `  const structurePoints = field => completeStructureRows
    .map(row => ({ x: row.date, y: row[field] }));`;
const forwardFilledStructure = `  const structurePoints = field => {
    let previous = null;
    return quarterlyRows.map(row => {
      if (row[field] !== null && row[field] !== undefined) previous = row[field];
      return { x: row.date, y: row[field] ?? previous };
    });
  };`;

function restoreInvisibleNullArrayRepresentation(original) {
  let patched = original.toString('utf8');
  patched = replaceExactly(
    patched,
    structurePointsAnchor,
    `${structurePointsAnchor}\n  const quarterlyArray = field => labels.map(date => {\n`
      + `    const row = quarterlyByDate.get(date);\n`
      + `    return row && row[field] !== undefined ? row[field] : null;\n`
      + `  });`,
    'daily-union helper 注入锚点',
  ).toString('utf8');
  patched = replaceExactly(
    patched,
    '  return { labels, hybrid, quarterlyPoints, structurePoints, firstDaily };',
    '  return { labels, hybrid, quarterlyPoints, quarterlyArray, structurePoints, firstDaily };',
    'daily-union helper return 注入锚点',
  ).toString('utf8');
  patched = replaceExactly(
    patched,
    '  const { labels, hybrid, quarterlyPoints, structurePoints } = buildDebtSeries(',
    '  const { labels, hybrid, quarterlyPoints, quarterlyArray, structurePoints } = buildDebtSeries(',
    'daily-union helper destructure 注入锚点',
  ).toString('utf8');
  for (const field of ['gdp_bn', 'debt_gdp_pct']) {
    patched = replaceExactly(
      patched,
      `quarterlyPoints('${field}')`,
      `quarterlyArray('${field}')`,
      `${field} daily-union 调用`,
    ).toString('utf8');
  }
  patched = patched.replaceAll(
    "pointRadius: 2, pointHoverRadius: 4, pointStyle: 'circle'",
    "pointRadius: 0, pointHoverRadius: 0, pointStyle: 'line'",
  );
  return Buffer.from(patched, 'utf8');
}

function verifyInvisibleNullArrayRepresentation(_original, patched) {
  const text = patched.toString('utf8');
  return {
    ok: ['gdp_bn', 'debt_gdp_pct'].every(field =>
      text.includes(`quarterlyArray('${field}')`)
        && !text.includes(`quarterlyPoints('${field}')`))
      && (text.match(/pointRadius: 0/g) || []).length >= 2,
    detail: 'daily union + null arrays + spanGaps:false + pointRadius:0',
  };
}

const result = await runInjectionSuite({
  name: 'C15 debt stack and unified tooltip injection wrapper',
  target: 'macro.html',
  guard: 'tools/verify-macro-page.mjs',
  timeoutMs: 180_000,
  cases: [
    {
      name: 'GDP line removed from unified tooltip',
      patch: replaceAnchor(gdpTooltipLine, '', 'GDP tooltip 注入锚点'),
      verifyPatch: verifyReplacement(gdpTooltipLine, ''),
      expectedFailureMarkers: ['FAIL 最新 Treasury 日期真实 hover 显示统一六项 tooltip'],
    },
    {
      name: 'foreign bar moved to a different stack',
      patch: replaceAnchor(foreignStackAnchor, foreignSplitStack, 'foreign stack 注入锚点'),
      verifyPatch: verifyReplacement(foreignStackAnchor, foreignSplitStack),
      expectedFailureMarkers: ['FAIL 三项结构柱同 stack / yAmount 且同季度 x 集合'],
    },
    {
      name: 'technical structure snapshot label restored',
      patch: replaceAnchor(intragovLabelAnchor, technicalLabel, '技术图例文案注入锚点'),
      verifyPatch: verifyReplacement(intragovLabelAnchor, technicalLabel),
      expectedFailureMarkers: ['FAIL 债务总览恰有六个用户可见 dataset'],
    },
    {
      name: 'redundant daily public and intragov lines restored',
      patch: replaceAnchor(totalDataset, redundantDailyLines, '冗余日频折线注入锚点'),
      verifyPatch: verifyRedundantDailyLines,
      expectedFailureMarkers: ['FAIL 债务总览恰有六个用户可见 dataset'],
    },
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
      expectedFailureMarkers: ['FAIL 结构柱 domestic_public_bn 只含完整季度真实 observation'],
    },
    {
      name: 'GDP dataset removed',
      patch: replaceAnchor(gdpDataset, '', 'GDP dataset 注入锚点'),
      verifyPatch: verifyReplacement(gdpDataset, ''),
      expectedFailureMarkers: ['FAIL 债务总览恰有六个用户可见 dataset'],
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
      name: 'quarterly structure forward-filled across gaps',
      patch: replaceAnchor(structurePointsAnchor, forwardFilledStructure,
        '结构 forward-fill 注入锚点'),
      verifyPatch: verifyReplacement(structurePointsAnchor, forwardFilledStructure),
      expectedFailureMarkers: ['FAIL 结构柱 intragov_bn 只含完整季度真实 observation'],
    },
    {
      name: 'low-frequency lines restored to invisible daily-union null arrays',
      patch: restoreInvisibleNullArrayRepresentation,
      verifyPatch: verifyInvisibleNullArrayRepresentation,
      expectedFailureMarkers: ['FAIL 低频折线 gdp_bn 只含真实 observation object'],
    },
  ],
});

process.exitCode = result.ok ? 0 : 1;
