// 破坏注入：退回 total_oi 旧口径后，verify-contract-contango 必须变红。
import { runInjectionSuite } from './_injection.mjs';

const result = await runInjectionSuite({
  name: 'total_oi injection wrapper',
  target: 'data/derived/term-structure-series.json',
  guard: 'tools/verify-contract-contango.mjs',
  cases: [{
    name: 'total_oi 退回全部挂牌月旧口径',
    patch(original) {
      const payload = JSON.parse(original.toString('utf8'));
      const frame = payload.data.frames.at(-1);
      const oldCaliber = payload.data.contracts.reduce(
        (sum, _contract, index) => sum
          + (frame.settle[index] != null ? (frame.oi[index] || 0) : 0),
        0,
      );
      frame.total_oi = oldCaliber;
      return JSON.stringify(payload);
    },
    verifyPatch(original, patched) {
      const before = JSON.parse(original.toString('utf8')).data.frames.at(-1).total_oi;
      const after = JSON.parse(patched.toString('utf8')).data.frames.at(-1).total_oi;
      return { ok: before !== after, detail: `${before} -> ${after}` };
    },
    expectedFailureMarkers: ['derive total_oi'],
  }],
});

process.exitCode = result.ok ? 0 : 1;
