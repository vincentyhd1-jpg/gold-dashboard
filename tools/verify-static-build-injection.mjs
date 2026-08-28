import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CONFIG = path.join(ROOT, 'wrangler.jsonc');
const DIST = path.join(ROOT, 'dist');
const BUILD = 'tools/build-static-site.mjs';
const GUARD = 'tools/verify-static-build.mjs';
const originalConfig = fs.readFileSync(CONFIG);
const originalHash = sha256(originalConfig);
const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'gold-dashboard-static-injection-'));
const backup = path.join(tempDir, 'wrangler.jsonc');
fs.writeFileSync(backup, originalConfig);

let passed = 0;
let failed = 0;

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function check(name, ok, detail = '') {
  if (ok) {
    passed++;
    console.log(`PASS ${name}${detail ? `  ${detail}` : ''}`);
  } else {
    failed++;
    console.log(`FAIL ${name}${detail ? `  ${detail}` : ''}`);
  }
}

function run(script) {
  const result = spawnSync(process.execPath, [script], {
    cwd: ROOT,
    encoding: 'utf8',
    shell: false,
    timeout: 120_000,
  });
  return {
    code: result.status,
    signal: result.signal,
    error: result.error,
    output: `${result.stdout || ''}\n${result.stderr || ''}`,
  };
}

function show(label, result) {
  console.log(`=== ${label}: exit=${String(result.code)} signal=${result.signal || '-'} ===`);
  for (const line of result.output.split('\n')
    .filter(text => /^(?:FAIL|\d+ passed,)/.test(text)).slice(0, 12)) {
    console.log(`  ${line}`);
  }
  if (result.error) console.log(`  ${result.error.message}`);
}

function runBuildAndGuard(label) {
  const build = run(BUILD);
  show(`${label} build`, build);
  check(`${label} build exit 0`, build.code === 0 && !build.signal && !build.error);
  const guard = run(GUARD);
  show(`${label} guard`, guard);
  check(`${label} guard exit 0`, guard.code === 0 && !guard.signal && !guard.error);
  return guard;
}

function expectRed(label, marker) {
  const result = run(GUARD);
  show(label, result);
  check(`${label} guard 真实返回非零`, Number.isInteger(result.code)
    && result.code !== 0 && !result.signal && !result.error, `exit=${result.code}`);
  check(`${label} 命中预期 FAIL marker`, result.output.includes(marker), marker);
}

function restoreConfig() {
  fs.copyFileSync(backup, CONFIG);
  check('wrangler.jsonc 恢复原始 SHA-256',
    sha256(fs.readFileSync(CONFIG)) === originalHash);
}

console.log('## C16 static deployment negative verification');
try {
  runBuildAndGuard('baseline');

  console.log('\n--- A: 删除 assets.directory ---');
  const configText = originalConfig.toString('utf8');
  const anchor = /"assets": \{\r?\n    "directory": "\.\/dist"\r?\n  \}/g;
  const hits = [...configText.matchAll(anchor)].length;
  check('A 注入锚点恰好命中一次', hits === 1, `hits=${hits}`);
  const missingAssetsDirectory = Buffer.from(configText.replace(anchor, '"assets": {}'));
  fs.writeFileSync(CONFIG, missingAssetsDirectory);
  check('A 注入 bytes 已落盘', sha256(fs.readFileSync(CONFIG))
    === sha256(missingAssetsDirectory) && sha256(missingAssetsDirectory) !== originalHash);
  expectRed('A missing assets.directory', 'FAIL wrangler assets.directory 固定为 ./dist');
  restoreConfig();

  console.log('\n--- B: dist 缺少 macro.html ---');
  run(BUILD);
  const macro = path.join(DIST, 'macro.html');
  check('B 注入前 macro.html 存在', fs.existsSync(macro));
  fs.rmSync(macro, { force: true });
  check('B macro.html 已真实删除', !fs.existsSync(macro));
  expectRed('B missing macro.html', 'FAIL dist 必需文件 macro.html');
  runBuildAndGuard('B restore');

  console.log('\n--- C: dist 泄漏 fetch_treasury_debt.py ---');
  const leaked = path.join(DIST, 'fetch_treasury_debt.py');
  fs.copyFileSync(path.join(ROOT, 'fetch_treasury_debt.py'), leaked);
  check('C Python 文件已真实进入 dist', fs.existsSync(leaked)
    && fs.statSync(leaked).size > 0);
  expectRed('C leaked Python', 'FAIL dist 不公开 fetch_treasury_debt.py');
  runBuildAndGuard('C restore');

  console.log('\n--- D: dist baseline 更新但 scenario basis 未重建 ---');
  run(BUILD);
  const distBaselinePath = path.join(DIST, 'data', 'derived', 'cbo_baseline_latest.json');
  const distBaseline = JSON.parse(fs.readFileSync(distBaselinePath, 'utf8'));
  const originalDebt = distBaseline.data.annual[1].debt_held_by_public_bn;
  distBaseline.data.annual[1].debt_held_by_public_bn = originalDebt + 0.001;
  fs.writeFileSync(distBaselinePath, `${JSON.stringify(distBaseline, null, 2)}\n`);
  check('D dist baseline 合法数值已真实改变',
    JSON.parse(fs.readFileSync(distBaselinePath, 'utf8')).data.annual[1]
      .debt_held_by_public_bn === originalDebt + 0.001);
  expectRed('D stale scenario basis',
    'FAIL dist CBO scenario basis 业务数据对应 baseline');
  runBuildAndGuard('D restore');

  runBuildAndGuard('final restored');
  check('最终 wrangler.jsonc SHA-256 一致',
    sha256(fs.readFileSync(CONFIG)) === originalHash);
} catch (error) {
  failed++;
  console.log(`FAIL wrapper 异常  ${error.stack || error.message}`);
} finally {
  fs.copyFileSync(backup, CONFIG);
  const finalBuild = run(BUILD);
  const finalGuard = run(GUARD);
  check('finally 恢复 wrangler.jsonc SHA-256',
    sha256(fs.readFileSync(CONFIG)) === originalHash);
  check('finally 重建 dist 并恢复全绿', finalBuild.code === 0 && finalGuard.code === 0,
    `build=${finalBuild.code} guard=${finalGuard.code}`);
  fs.rmSync(tempDir, { recursive: true, force: true });
}

console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
