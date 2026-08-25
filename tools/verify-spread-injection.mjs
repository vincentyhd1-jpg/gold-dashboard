// 破坏注入：把某帧 spread 改为 999.9，verify-contract-contango 必须变红。
import { runInjectionSuite } from './_injection.mjs';

const result = await runInjectionSuite({
  name: 'spread injection wrapper',
  target: 'data/derived/term-structure-series.json',
  guard: 'tools/verify-contract-contango.mjs',
  cases: [{
    name: 'spread -> 999.9',
    patch(original) {
      const payload = JSON.parse(original.toString('utf8'));
      payload.data.frames.at(-1).spread = 999.9;
      return JSON.stringify(payload);
    },
    verifyPatch(original, patched) {
      const before = JSON.parse(original.toString('utf8')).data.frames.at(-1).spread;
      const after = JSON.parse(patched.toString('utf8')).data.frames.at(-1).spread;
      return { ok: before !== after && after === 999.9, detail: `${before} -> ${after}` };
    },
    expectedFailureMarkers: ['derive spread'],
  }],
});

process.exitCode = result.ok ? 0 : 1;
