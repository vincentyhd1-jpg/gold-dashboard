// 破坏模块隔离后，verify-isolation 必须逐 case 变红且最终恢复 index.html。
import { replaceExactly, runInjectionSuite } from './_injection.mjs';

const SAFE_RENDER_LOG = "    console.error('[render] ' + name + ' 渲染失败：', err);";
const SAFE_RENDER_SILENT = '    /* 注入：吞掉模块错误 */';
const COT_SAFE_CALL = "    _safeRender('COT/金价', () => renderAll(cotData, goldData, stocksData));";
const COT_DIRECT_CALL = '    renderAll(cotData, goldData, stocksData);';
const DEPOT_SAFE_CALL = "    _safeRender('仓库趋势', () => renderDepotTrend(stocksData));";
const DEPOT_DIRECT_CALL = '    renderDepotTrend(stocksData);';

const result = await runInjectionSuite({
  name: 'isolation injection wrapper',
  target: 'index.html',
  guard: 'tools/verify-isolation.mjs',
  cases: [
    {
      name: '_safeRender 吞异常',
      patch(original) {
        return replaceExactly(
          original.toString('utf8'), SAFE_RENDER_LOG, SAFE_RENDER_SILENT,
          '_safeRender console 锚点',
        );
      },
      verifyPatch(original, patched) {
        const before = original.toString('utf8');
        const after = patched.toString('utf8');
        return {
          ok: before.includes(SAFE_RENDER_LOG)
            && !after.includes(SAFE_RENDER_LOG)
            && after.includes(SAFE_RENDER_SILENT),
          detail: 'console.error -> silent comment',
        };
      },
      expectedFailureMarkers: ['console 报出'],
    },
    {
      name: '隔离失效导致双模块受害',
      patch(original) {
        let source = original.toString('utf8');
        source = replaceExactly(source, COT_SAFE_CALL, COT_DIRECT_CALL, 'COT safeRender 锚点');
        source = replaceExactly(source, DEPOT_SAFE_CALL, DEPOT_DIRECT_CALL, 'depot safeRender 锚点');
        return source;
      },
      verifyPatch(original, patched) {
        const before = original.toString('utf8');
        const after = patched.toString('utf8');
        return {
          ok: before.includes(COT_SAFE_CALL) && before.includes(DEPOT_SAFE_CALL)
            && !after.includes(COT_SAFE_CALL) && !after.includes(DEPOT_SAFE_CALL)
            && after.includes(COT_DIRECT_CALL) && after.includes(DEPOT_DIRECT_CALL),
          detail: '两个模块均绕过 _safeRender',
        };
      },
      expectedFailureMarkers: ['footer 未谎报'],
    },
  ],
});

process.exitCode = result.ok ? 0 : 1;
