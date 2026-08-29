import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { replaceExactly, runInjectionSuite, sha256 } from './_injection.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const pagePath = path.join(ROOT, 'macro.html');
const derivePath = path.join(ROOT, 'derive_gold_vs_debt.py');
const goldPath = path.join(ROOT, 'data', 'gold_price.json');
const outputPath = path.join(ROOT, 'data', 'derived', 'gold_vs_debt.json');
const originalHashes = Object.fromEntries([
  pagePath, derivePath, goldPath, outputPath,
].map(file => [file, sha256(fs.readFileSync(file))]));

const pageResult = await runInjectionSuite({
  name: 'C18C.1 Treasury page behavior injections',
  target: 'macro.html',
  guard: 'tools/verify-treasury-enhancements.mjs',
  timeoutMs: 180_000,
  cases: [
    {
      name: 'UST zoom no longer autoscales Y',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        '        autoscaleUSTY(chart);',
        '        // injected: Y autoscale removed;'),
      verifyPatch: (_, patched) => ({
        ok: !patched.toString('utf8').includes('        autoscaleUSTY(chart);'),
        detail: 'onZoomComplete no longer calls Y autoscale',
      }),
      expectedFailureMarkers: ['FAIL UST Y 轴按选区内真实数据自动重算'],
    },
    {
      name: 'intraday view degraded to daily data',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        "  intraday: { label: '分时', interval: '5', range: '1D' },",
        "  intraday: { label: '分时', interval: 'D', range: '5D' },"),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes("interval: 'D', range: '5D'"),
        detail: '分时 contract 已真实退化为 daily/5D',
      }),
      expectedFailureMarkers: ['FAIL 分时 / 1D / 5D 是分钟 interval + 真实 range contract'],
    },
    {
      name: 'TradingView failure escapes module boundary',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        '    if (token === liveTreasuryState.token) setLiveTreasuryUnavailable();',
        "    if (token === liveTreasuryState.token) throw new Error('injected widget failure');"),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes("throw new Error('injected widget failure')"),
        detail: 'script.onerror now throws instead of rendering fallback',
      }),
      expectedFailureMarkers: ['FAIL TradingView CDN 失败无未捕获 pageerror'],
    },
    {
      name: 'TradingView methodology disclaimer removed',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        'Data / chart source: TradingView',
        'External chart source omitted'),
      verifyPatch: (_, patched) => ({
        ok: !patched.toString('utf8').includes('Data / chart source: TradingView'),
        detail: 'source attribution sentence removed',
      }),
      expectedFailureMarkers: ['FAIL TradingView attribution 与 C17 effective_r 隔离文案存在'],
    },
    {
      name: 'UST reset zoom disabled',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        "  ustChart.resetZoom('none');",
        '  // injected: resetZoom removed;'),
      verifyPatch: (_, patched) => ({
        ok: !patched.toString('utf8').includes("  ustChart.resetZoom('none');"),
        detail: 'reset handler no longer invokes Chart.js resetZoom',
      }),
      expectedFailureMarkers: ['FAIL UST Reset Zoom 恢复完整 X/Y 范围'],
    },
  ],
});

const deriveResult = await runInjectionSuite({
  name: 'C18C.1 gold/debt formula and definition injections',
  target: 'derive_gold_vs_debt.py',
  guard: 'tools/verify-gold-debt-python.mjs',
  timeoutMs: 120_000,
  cases: [
    {
      name: 'global gold value formula scaled by 0.99',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        '                GOLD_STOCK_TONNES * TROY_OZ_PER_METRIC_TONNE * price / 1e12',
        '                GOLD_STOCK_TONNES * TROY_OZ_PER_METRIC_TONNE * price / 1e12 * 0.99'),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes('/ 1e12 * 0.99'),
        detail: 'production gold value is materially understated',
      }),
      expectedFailureMarkers: ['FAIL gold formula uses fixed stock and exact unit conversion'],
    },
    {
      name: 'debt definition changed to debt held by public',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
          '    debt_by_date = {row["date"]: row["total_bn"] for row in debt_rows}',
          '    debt_by_date = {row["date"]: row["public_bn"] for row in debt_payload["data"] if isinstance(row.get("public_bn"), (int, float)) and row["public_bn"] > 0}'),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes('row["public_bn"] for row in debt_payload["data"]'),
        detail: 'production comparison now reads debt held by public',
      }),
      expectedFailureMarkers: ['FAIL debt uses exact Total Public Debt Outstanding source'],
    },
  ],
});

const staleResult = await runInjectionSuite({
  name: 'C18C.1 committed derived freshness injection',
  target: 'data/gold_price.json',
  guard: 'tools/verify-gold-debt-python.mjs',
  timeoutMs: 120_000,
  cases: [{
    name: 'current gold source changes without rebuilding comparison',
    patch: bytes => {
      const payload = JSON.parse(bytes.toString('utf8'));
      payload.data[0].price += 0.001;
      return `${JSON.stringify(payload, null, 2)}\n`;
    },
    verifyPatch: (original, patched) => {
      const before = JSON.parse(original.toString('utf8'));
      const after = JSON.parse(patched.toString('utf8'));
      return {
        ok: after.data[0].price === before.data[0].price + 0.001
          && after.schema_version === before.schema_version
          && after.source === before.source,
        detail: '合法周频金价 +0.001，派生产物保持未变',
      };
    },
    expectedFailureMarkers: [
      'FAIL committed gold-vs-debt output is stale relative to current sources',
    ],
  }],
});

let hashesRestored = true;
for (const [file, hash] of Object.entries(originalHashes)) {
  const restored = sha256(fs.readFileSync(file)) === hash;
  hashesRestored = hashesRestored && restored;
  console.log(`${restored ? 'PASS' : 'FAIL'} final SHA-256 restored ${path.relative(ROOT, file)}`);
}

const result = {
  ok: pageResult.ok && deriveResult.ok && staleResult.ok && hashesRestored,
};
console.log(`${result.ok ? 'PASS' : 'FAIL'} C18C.1 all eight injections detected and restored`);
process.exitCode = result.ok ? 0 : 1;
