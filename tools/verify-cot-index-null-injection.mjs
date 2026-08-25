// COT Index 的 current value 与历史图两条 null 路径都必须保持不可知，
// 任何 null → 50 回填都要让 verify-ui-fixes 真实变红。
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

const currentAnchor = '  const mfPct = latest.mf_index;';
const currentFallback = '  const mfPct = latest.mf_index ?? 50;';
const chartAnchor = '  const cotIdxD = weekly.map(r => r.mf_index == null ? null : r.mf_index);';
const chartFallback = '  const cotIdxD = weekly.map(r => r.mf_index == null ? 50 : r.mf_index);';

const result = await runInjectionSuite({
  name: 'COT Index null injection wrapper',
  target: 'index.html',
  guard: 'tools/verify-ui-fixes.mjs',
  timeoutMs: 180_000,
  cases: [
    {
      name: 'current null fallback to 50',
      patch: replaceAnchor(currentAnchor, currentFallback, 'current Index 注入锚点'),
      verifyPatch: verifyReplacement(currentAnchor, currentFallback),
      expectedFailureMarkers: ['[9]a mf null 显示 --'],
    },
    {
      name: 'chart null fallback to 50',
      patch: replaceAnchor(chartAnchor, chartFallback, '历史 Index 注入锚点'),
      verifyPatch: verifyReplacement(chartAnchor, chartFallback),
      expectedFailureMarkers: ['[9]a Index 图逐点保持 null'],
    },
  ],
});

process.exitCode = result.ok ? 0 : 1;
