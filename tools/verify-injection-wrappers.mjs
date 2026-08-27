import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { runInjectionSuite, runNodeGuard } from './_injection.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
let passed = 0;
let failed = 0;

function check(name, ok, detail = '') {
  if (ok) {
    passed++;
    console.log(`  PASS  ${name}`);
  } else {
    failed++;
    console.log(`  FAIL  ${name}${detail ? `  ${detail}` : ''}`);
  }
}

const wrappers = [
  'verify-totaloi-injection.mjs',
  'verify-spread-injection.mjs',
  'verify-kpi-injection.mjs',
  'verify-isolation-injection.mjs',
  'verify-cot-index-null-injection.mjs',
  'verify-debt-overview-injection.mjs',
  'verify-fiscal-stress-injection.mjs',
  'verify-cbo-baseline-injection.mjs',
  'verify-cbo-scenario-injection.mjs',
];

console.log('## static contract');
for (const name of wrappers) {
  const source = fs.readFileSync(path.join(__dirname, name), 'utf8');
  check(`${name}: 使用公共 injection suite`,
    source.includes("from './_injection.mjs'") && source.includes('runInjectionSuite('));
  check(`${name}: wrapper 自身明确设置 exit code`,
    source.includes('process.exitCode = result.ok ? 0 : 1'));
  check(`${name}: 不在仓库创建 backup`,
    !/copyFileSync|injection-backup|BACKUP\s*=/.test(source));
}

async function withFixture(execute) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'gold-dashboard-meta-'));
  const target = path.join(dir, 'fixture.txt');
  const original = Buffer.from('baseline');
  fs.writeFileSync(target, original);
  try {
    const result = await execute({ dir, target, original });
    return { result, restored: fs.readFileSync(target).equals(original) };
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

const baseConfig = dir => ({
  name: 'meta fixture',
  root: dir,
  target: 'fixture.txt',
  guard: 'unused.mjs',
  cases: [{
    name: 'controlled mutation',
    patch: () => Buffer.from('injected'),
    expectedFailureMarkers: ['EXPECTED_FAILURE'],
  }],
});

console.log('\n## dynamic contract');

const missingCwd = fs.mkdtempSync(path.join(os.tmpdir(), 'gold-dashboard-spawn-error-'));
fs.rmSync(missingCwd, { recursive: true, force: true });
const spawnError = await runNodeGuard('unused.mjs', { cwd: missingCwd });
check('spawn error 返回结构化失败且不会挂起',
  spawnError.code === null && spawnError.error != null && spawnError.signal == null,
  spawnError.error?.message || 'missing error');

const happy = await withFixture(({ dir, target }) =>
  runInjectionSuite(baseConfig(dir), {
    runGuard: async () => fs.readFileSync(target, 'utf8') === 'baseline'
      ? { code: 0, signal: null, error: null, timedOut: false, stdout: '', stderr: '' }
      : { code: 1, signal: null, error: null, timedOut: false,
          stdout: 'FAIL EXPECTED_FAILURE', stderr: '' },
  }));
check('baseline 绿 → injection 红 → restored 绿时 suite 成功', happy.result.ok);
check('成功路径最终恢复 fixture bytes', happy.restored);

let baselineRedInjectionCalls = 0;
const baselineRed = await withFixture(({ dir }) =>
  runInjectionSuite(baseConfig(dir), {
    runGuard: async ({ phase }) => {
      if (phase === 'injection') baselineRedInjectionCalls++;
      return { code: 1, signal: null, error: null, timedOut: false,
        stdout: 'FAIL BASELINE_RED', stderr: '' };
    },
  }));
check('baseline 本来为红时 suite 失败',
  !baselineRed.result.ok
  && baselineRed.result.failures.some(x => x.includes('baseline guard exit 0')));
check('baseline 红时不执行 injection case', baselineRedInjectionCalls === 0,
  `injectionCalls=${baselineRedInjectionCalls}`);
check('baseline 红路径最终恢复 fixture bytes', baselineRed.restored);

const signaled = await withFixture(({ dir }) =>
  runInjectionSuite(baseConfig(dir), {
    runGuard: async () => ({ code: null, signal: 'SIGTERM', error: null,
      timedOut: false, stdout: '', stderr: '' }),
  }));
check('guard 被 signal 终止时 suite 失败',
  !signaled.result.ok
  && signaled.result.failures.some(x => x.includes('signal=SIGTERM')));
check('signal 路径最终恢复 fixture bytes', signaled.restored);

const falseGreen = await withFixture(({ dir }) =>
  runInjectionSuite(baseConfig(dir), {
    runGuard: async () => ({ code: 0, signal: null, error: null, timedOut: false,
      stdout: '', stderr: '' }),
  }));
check('injection 仍 exit 0 时 suite 失败',
  !falseGreen.result.ok
  && falseGreen.result.failures.some(x => x.includes('护栏假绿')));
check('假绿路径最终恢复 fixture bytes', falseGreen.restored);

const noOp = await withFixture(({ dir }) => {
  const config = baseConfig(dir);
  config.cases[0].patch = original => original;
  return runInjectionSuite(config, {
    runGuard: async () => ({ code: 0, signal: null, error: null, timedOut: false,
      stdout: '', stderr: '' }),
  });
});
check('patch 未改变内容时 suite 失败',
  !noOp.result.ok
  && noOp.result.failures.some(x => x.includes('patch 真实改变文件')));
check('no-op 路径最终恢复 fixture bytes', noOp.restored);

let restoreCalls = 0;
const restoreMismatch = await withFixture(({ dir }) =>
  runInjectionSuite(baseConfig(dir), {
    runGuard: async ({ phase }) => ({
      code: phase === 'injection' ? 1 : 0,
      signal: null,
      error: null,
      timedOut: false,
      stdout: phase === 'injection' ? 'FAIL EXPECTED_FAILURE' : '',
      stderr: '',
    }),
    restoreTarget: ({ targetPath }) => {
      restoreCalls++;
      fs.writeFileSync(targetPath, 'wrong-restore');
    },
  }));
check('restore hash 不一致时 suite 失败',
  !restoreMismatch.result.ok
  && restoreMismatch.result.failures.some(x => x.includes('文件 hash 恢复')),
  `restoreCalls=${restoreCalls}`);
check('restore mismatch 后紧急/finally 恢复 fixture bytes', restoreMismatch.restored);

const patchThrow = await withFixture(({ dir }) => {
  const config = baseConfig(dir);
  config.cases[0].patch = () => { throw new Error('ACTIVE_THROW_AFTER_PATCH_ENTRY'); };
  return runInjectionSuite(config, {
    runGuard: async () => ({ code: 0, signal: null, error: null, timedOut: false,
      stdout: '', stderr: '' }),
  });
});
check('patch 主动 throw 时 suite 失败',
  !patchThrow.result.ok
  && patchThrow.result.failures.some(x => x.includes('ACTIVE_THROW_AFTER_PATCH_ENTRY')));
check('patch throw 后 finally 恢复 fixture bytes', patchThrow.restored);

console.log(`\n${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
