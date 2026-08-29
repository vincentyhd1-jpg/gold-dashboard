import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { replaceExactly, runInjectionSuite, sha256 } from './_injection.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const targetPath = path.join(ROOT, 'macro.html');
const originalHash = sha256(fs.readFileSync(targetPath));

const result = await runInjectionSuite({
  name: 'C18C.2 Treasury interaction and restricted-symbol injections',
  target: 'macro.html',
  guard: 'tools/verify-treasury-enhancements.mjs',
  timeoutMs: 240_000,
  cases: [
    {
      name: 'hybrid pointer detection regresses to primary pointer',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        "  return window.matchMedia('(any-pointer: fine)').matches\n    && window.matchMedia('(any-hover: hover)').matches;",
        "  return window.matchMedia('(pointer: fine)').matches\n    && window.matchMedia('(hover: hover)').matches;"),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes("matchMedia('(pointer: fine)')"),
        detail: 'mouse capability now incorrectly follows primary pointer',
      }),
      expectedFailureMarkers: ['FAIL hybrid pointer contract 使用 any-pointer / any-hover'],
    },
    {
      name: 'UST canvas dblclick reset listener removed',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        "document.getElementById('ustChart').addEventListener('dblclick', event => {",
        "document.getElementById('ustChart').addEventListener('c18c2-disabled-dblclick', event => {"),
      verifyPatch: (_, patched) => ({
        ok: !patched.toString('utf8').includes("getElementById('ustChart').addEventListener('dblclick'"),
        detail: 'canvas no longer receives dblclick reset',
      }),
      expectedFailureMarkers: ['FAIL UST 双击只在 chartArea 内调用共同 resetUSTZoom'],
    },
    {
      name: 'UST dblclick restores X but leaves zoomed Y bounds',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        '  resetUSTZoom();\n});\n\nfunction renderFed',
        `  const injectedY = { min: ustChart.scales.y.min, max: ustChart.scales.y.max };
  ustChart.resetZoom('none');
  ustChart.options.scales.y.min = injectedY.min;
  ustChart.options.scales.y.max = injectedY.max;
  ustChart.update('none');
  syncUSTResetButton(ustChart);
});

function renderFed`),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes('const injectedY = { min: ustChart.scales.y.min'),
        detail: 'dblclick reset deliberately retains zoomed Y min/max',
      }),
      expectedFailureMarkers: ['FAIL 绘图区双击恢复完整 UST X/Y 与按钮状态'],
    },
    {
      name: 'missing zoom plugin still claims drag zoom is available',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        '      ? UST_ZOOM_UNAVAILABLE_HINT\n      : (health.mouseCapable ? UST_ZOOM_AVAILABLE_HINT : UST_ZOOM_TOUCH_HINT);',
        '      ? UST_ZOOM_AVAILABLE_HINT\n      : (health.mouseCapable ? UST_ZOOM_AVAILABLE_HINT : UST_ZOOM_TOUCH_HINT);'),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes('? UST_ZOOM_AVAILABLE_HINT\n      : (health.mouseCapable'),
        detail: 'plugin-missing branch now lies about availability',
      }),
      expectedFailureMarkers: ['FAIL zoom plugin missing 显示固定局部错误并禁用 Reset'],
    },
    {
      name: 'restricted TVC yield symbol reintroduced into production contract',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        '  symbols: Object.freeze([]),',
        "  symbols: Object.freeze(['TVC:US10Y']),"),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes("Object.freeze(['TVC:US10Y'])"),
        detail: 'forbidden third-party TradingView yield symbol is back',
      }),
      expectedFailureMarkers: ['FAIL production 不再请求任何受限 TVC / CBOT symbol'],
    },
    {
      name: 'unavailable market card mislabeled as live Treasury yield',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        '实时美债市场（暂不可用） / Live U.S. Treasury Market',
        '实时国债收益率 / Live U.S. Treasury Yield'),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes('实时国债收益率 / Live U.S. Treasury Yield'),
        detail: 'unavailable card now promises a yield product',
      }),
      expectedFailureMarkers: ['FAIL Live Treasury unavailable card 存在且未伪装为实时收益率产品'],
    },
    {
      name: 'Live market contract enters fiscal calculations',
      patch: bytes => replaceExactly(bytes.toString('utf8'),
        '  entersFiscalCalculations: false,',
        '  entersFiscalCalculations: true,'),
      verifyPatch: (_, patched) => ({
        ok: patched.toString('utf8').includes('  entersFiscalCalculations: true,'),
        detail: 'third-party market context now contaminates fiscal calculations',
      }),
      expectedFailureMarkers: ['FAIL Live unavailable contract 不写 JSON 或进入财政计算'],
    },
    {
      name: 'fixed-stock historical methodology warning removed',
      patch: bytes => {
        const text = bytes.toString('utf8');
        const anchor = '历史所有日期当前均使用固定 end-2025 的 220,700 t 存量';
        const count = text.split(anchor).length - 1;
        if (count !== 2) throw new Error(`fixed-stock 锚点应命中 2 次，实际 ${count}`);
        return text.replaceAll(anchor, '历史库存方法说明已删除');
      },
      verifyPatch: (_, patched) => ({
        ok: !patched.toString('utf8').includes('历史所有日期当前均使用固定 end-2025 的 220,700 t 存量'),
        detail: 'initial and dynamically rendered methodology both lost the caveat',
      }),
      expectedFailureMarkers: ['FAIL 页面公开 fixed-stock valuation proxy 与当前价格代理'],
    },
  ],
});

const restored = sha256(fs.readFileSync(targetPath)) === originalHash;
console.log(`${restored ? 'PASS' : 'FAIL'} C18C.2 final macro.html SHA-256 restored`);
const ok = result.ok && restored;
console.log(`${ok ? 'PASS' : 'FAIL'} C18C.2 all eight injections detected and restored`);
process.exitCode = result.ok ? 0 : 1;
if (!restored) process.exitCode = 1;
