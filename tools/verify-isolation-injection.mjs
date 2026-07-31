// 破坏注入：verify-isolation 的断言累加器是否真会红？
//
// 此前该脚本无任何断言，退出码只反映「脚本自身是否抛异常」—— 三个模块全挂
// 也是 exit 0。这里注入「本该隔离却没隔离」的故障，验证新判定机制会变红。
//
// 注入手法：篡改 index.html 的 _safeRender，让它把异常吞掉不打模块名 /
// 或让某个本该存活的模块也挂掉。都是 verify-isolation 应当抓到的回归。
import { spawn } from 'child_process';
import fs from 'fs';

const TARGET = 'index.html';
const BACKUP = 'index.html.isolation-injection-backup';

function run() {
  return new Promise(resolve => {
    const p = spawn('node', ['tools/verify-isolation.mjs'], { shell: false });
    let out = '';
    p.stdout.on('data', d => out += d);
    p.stderr.on('data', d => out += d);
    p.on('close', code => resolve({ code, out }));
  });
}

const show = (label, r, maxFail = 6) => {
  console.log(`=== ${label} ===`);
  console.log(`  exit=${r.code}  ${r.code === 0 ? '绿' : '红'}`);
  const fails = r.out.split('\n').filter(l => /FAIL/.test(l));
  for (const l of fails.slice(0, maxFail)) console.log('   ', l.trim().slice(0, 120));
  if (fails.length > maxFail) console.log(`    …另有 ${fails.length - maxFail} 条 FAIL`);
  const tail = r.out.split('\n').filter(l => /passed,/.test(l));
  for (const l of tail) console.log('   ', l.trim());
};

const CASES = [
  // a) 单模块：_safeRender 吞掉异常不打模块名 → 「console 报出模块名」该红
  ['_safeRender 吞掉异常不打模块名（单模块）',
   src => src.replace(
     "    console.error('[render] ' + name + ' 渲染失败：', err);",
     "    /* 注入：吞掉不报 */")],

  // b) 双模块：隔离失效 —— 任一模块抛异常就整体降级为 mock，
  //    本该存活的模块也挂掉，且 footer 谎报「数据文件未找到」
  ['隔离失效：一个模块挂就整体降级（双模块受害）',
   src => src.replace(
     'function _safeRender(name, fn) {\n  try {\n    fn();',
     'function _safeRender(name, fn) {\n  try {\n    fn();')
     .replace(
       "    _safeRender('COT/金价', () => renderAll(cotData, goldData, stocksData));",
       "    renderAll(cotData, goldData, stocksData);")
     .replace(
       "    _safeRender('仓库趋势', () => renderDepotTrend(stocksData));",
       "    renderDepotTrend(stocksData);")],
];

fs.copyFileSync(TARGET, BACKUP);
try {
  show('注入前（基线）', await run());

  for (const [label, patch] of CASES) {
    const src = fs.readFileSync(BACKUP, 'utf8');
    const patched = patch(src);
    if (patched === src) {
      console.log(`\n=== ${label} ===\n  锚点未命中，跳过`);
      continue;
    }
    fs.writeFileSync(TARGET, patched);
    console.log();
    show(`注入：${label}`, await run());
  }

  fs.copyFileSync(BACKUP, TARGET);
  console.log();
  show('改回后', await run());
} finally {
  fs.copyFileSync(BACKUP, TARGET);
  fs.unlinkSync(BACKUP);
}
