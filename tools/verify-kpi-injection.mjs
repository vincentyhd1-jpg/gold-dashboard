// KPI 字段的四种破坏必须逐项让 verify-contract-contango 变红。
import { runInjectionSuite } from './_injection.mjs';

function patchFrames(field, value) {
  return original => {
    const payload = JSON.parse(original.toString('utf8'));
    let changed = 0;
    for (const frame of payload.data.frames) {
      if (field === 'spread_annualized_pct' && value !== null
          && frame.spread_annualized_pct == null) continue;
      if (frame[field] !== value) changed++;
      frame[field] = value;
    }
    if (changed === 0) throw new Error(`${field}: 目标值原本已是 ${value}`);
    return JSON.stringify(payload);
  };
}

function verifyField(field, expected) {
  return (original, patched) => {
    const before = JSON.parse(original.toString('utf8')).data.frames;
    const after = JSON.parse(patched.toString('utf8')).data.frames;
    const changed = after.filter((frame, i) => frame[field] !== before[i][field]);
    const valuesCorrect = changed.length > 0
      && changed.every(frame => frame[field] === expected);
    return { ok: valuesCorrect, detail: `${field} changed=${changed.length}` };
  };
}

const result = await runInjectionSuite({
  name: 'KPI injection wrapper',
  target: 'data/derived/term-structure-series.json',
  guard: 'tools/verify-contract-contango.mjs',
  cases: [
    {
      name: 'annualized wrong value 99.99',
      patch: patchFrames('spread_annualized_pct', 99.99),
      verifyPatch: verifyField('spread_annualized_pct', 99.99),
      expectedFailureMarkers: ['derive 年化与复算一致'],
    },
    {
      name: 'annualized null',
      patch: patchFrames('spread_annualized_pct', null),
      verifyPatch: verifyField('spread_annualized_pct', null),
      expectedFailureMarkers: ['derive 年化与复算一致'],
    },
    {
      name: 'spread wrong value 999.9',
      patch: patchFrames('spread', 999.9),
      verifyPatch: verifyField('spread', 999.9),
      expectedFailureMarkers: ['derive spread'],
    },
    {
      name: 'total_oi wrong value 1',
      patch: patchFrames('total_oi', 1),
      verifyPatch: verifyField('total_oi', 1),
      expectedFailureMarkers: ['derive total_oi'],
    },
  ],
});

process.exitCode = result.ok ? 0 : 1;
