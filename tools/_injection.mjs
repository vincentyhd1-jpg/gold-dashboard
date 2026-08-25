import crypto from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawn } from 'child_process';
import { fileURLToPath } from 'url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

export function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

export function replaceExactly(source, anchor, replacement, label = '注入锚点') {
  const count = source.split(anchor).length - 1;
  if (count !== 1) {
    throw new Error(`${label}应恰好命中 1 次，实际 ${count} 次`);
  }
  return source.replace(anchor, replacement);
}

export function runNodeGuard(script, { cwd = ROOT, timeoutMs = 120_000 } = {}) {
  return new Promise(resolve => {
    let stdout = '';
    let stderr = '';
    let settled = false;
    let timer;

    const finish = result => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve({ stdout, stderr, ...result });
    };

    let child;
    try {
      child = spawn(process.execPath, [script], { cwd, shell: false });
    } catch (error) {
      finish({ code: null, signal: null, error, timedOut: false });
      return;
    }

    child.stdout?.on('data', chunk => { stdout += chunk; });
    child.stderr?.on('data', chunk => { stderr += chunk; });
    child.once('error', error => {
      finish({ code: null, signal: null, error, timedOut: false });
    });
    child.once('close', (code, signal) => {
      finish({ code, signal, error: null, timedOut: false });
    });

    timer = setTimeout(() => {
      child.kill();
      finish({
        code: null,
        signal: 'TIMEOUT',
        error: new Error(`guard 超过 ${timeoutMs}ms 未退出`),
        timedOut: true,
      });
    }, timeoutMs);
  });
}

function guardExitedNormally(result) {
  return Number.isInteger(result?.code)
    && result.signal == null
    && result.error == null
    && !result.timedOut;
}

function showGuard(label, result) {
  const state = result.code === 0 ? '绿' : '红';
  const extra = result.signal ? ` signal=${result.signal}` : '';
  console.log(`=== ${label} ===`);
  console.log(`  exit=${String(result.code)}${extra}  ${state}`);
  const output = `${result.stdout || ''}\n${result.stderr || ''}`;
  for (const line of output.split('\n').filter(line => /FAIL|passed,|page errors:/.test(line)).slice(0, 8)) {
    console.log('   ', line.trim().slice(0, 180));
  }
  if (result.error) console.log('   ', result.error.message);
}

function normalizePatchResult(value) {
  if (Buffer.isBuffer(value)) return value;
  if (typeof value === 'string') return Buffer.from(value, 'utf8');
  throw new TypeError('patch 必须返回 Buffer 或 string');
}

/**
 * Run baseline -> injected failure -> byte-exact restore -> restored baseline.
 * Test-only dependencies let verify-injection-wrappers.mjs exercise failure
 * paths against temporary fixtures. Production wrappers use the defaults.
 */
export async function runInjectionSuite(config, testOnly = {}) {
  const {
    name,
    target,
    guard,
    cases,
    root = ROOT,
    timeoutMs = 120_000,
  } = config;
  const targetPath = path.resolve(root, target);
  const original = fs.readFileSync(targetPath);
  const originalHash = sha256(original);
  const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'gold-dashboard-injection-'));
  const backupPath = path.join(tempDir, path.basename(targetPath));
  fs.writeFileSync(backupPath, original);

  const failures = [];
  let passed = 0;
  const check = (label, ok, detail = '') => {
    if (ok) {
      passed++;
      console.log(`  PASS  ${label}`);
    } else {
      failures.push(`${label}${detail ? `：${detail}` : ''}`);
      console.log(`  FAIL  ${label}${detail ? `  ${detail}` : ''}`);
    }
    return ok;
  };

  const runner = testOnly.runGuard
    || (() => runNodeGuard(guard, { cwd: root, timeoutMs }));
  const restoreTarget = testOnly.restoreTarget
    || (() => fs.copyFileSync(backupPath, targetPath));

  const forceRestore = () => {
    fs.copyFileSync(backupPath, targetPath);
    const actual = sha256(fs.readFileSync(targetPath));
    if (actual !== originalHash) {
      throw new Error(`紧急恢复 hash 不一致：${actual} != ${originalHash}`);
    }
  };

  const restoreAndCheck = label => {
    try {
      restoreTarget({ backupPath, targetPath, original, originalHash });
      return check(`${label}: 文件 hash 恢复`,
        sha256(fs.readFileSync(targetPath)) === originalHash);
    } catch (error) {
      check(`${label}: 文件恢复`, false, error.message);
      return false;
    }
  };

  console.log(`\n## ${name}`);
  try {
    const baseline = await runner({ phase: 'baseline', targetPath });
    showGuard('注入前（基线）', baseline);
    const baselineOk = check('baseline guard exit 0',
      guardExitedNormally(baseline) && baseline.code === 0,
      `exit=${baseline.code} signal=${baseline.signal || '-'} error=${baseline.error?.message || '-'}`);

    if (baselineOk) {
      for (const testCase of cases) {
        forceRestore();
        console.log(`\n--- case: ${testCase.name} ---`);
        try {
          const patched = normalizePatchResult(await testCase.patch(Buffer.from(original)));
          const patchedHash = sha256(patched);
          const changed = !patched.equals(original) && patchedHash !== originalHash;
          if (!check(`${testCase.name}: patch 真实改变文件`, changed)) continue;

          if (testCase.verifyPatch) {
            const verdict = await testCase.verifyPatch(Buffer.from(original), patched);
            const ok = verdict === true || verdict?.ok === true;
            const detail = typeof verdict === 'object' ? verdict.detail : '';
            if (!check(`${testCase.name}: 业务锚点真实改变`, ok, detail || '校验未通过')) continue;
          }

          fs.writeFileSync(targetPath, patched);
          const diskHash = sha256(fs.readFileSync(targetPath));
          if (!check(`${testCase.name}: 注入 bytes 已落盘`,
            diskHash === patchedHash && diskHash !== originalHash,
            `disk=${diskHash} patched=${patchedHash}`)) continue;

          const result = await runner({ phase: 'injection', case: testCase.name, targetPath });
          showGuard(`注入：${testCase.name}`, result);
          const normal = check(`${testCase.name}: guard 正常返回 exit code`,
            guardExitedNormally(result),
            `exit=${result.code} signal=${result.signal || '-'} error=${result.error?.message || '-'}`);
          if (normal) {
            check(`${testCase.name}: 注入后 guard 必须非零`, result.code !== 0,
              `exit=${result.code}，护栏假绿`);
          }

          const output = `${result.stdout || ''}\n${result.stderr || ''}`;
          for (const marker of testCase.expectedFailureMarkers || []) {
            check(`${testCase.name}: 命中预期 FAIL marker`, output.includes(marker), marker);
          }
        } catch (error) {
          check(`${testCase.name}: injection 执行完成`, false, error.message);
        } finally {
          const restored = restoreAndCheck(`${testCase.name} 后恢复`);
          if (!restored) {
            try {
              forceRestore();
            } catch (error) {
              check(`${testCase.name}: 紧急恢复`, false, error.message);
            }
          }
        }
      }
    } else {
      console.log('  baseline 已红，跳过 injection；红色基线不能作为注入证据');
    }

    forceRestore();
    const restored = await runner({ phase: 'restored', targetPath });
    showGuard('恢复后', restored);
    check('restored guard exit 0',
      guardExitedNormally(restored) && restored.code === 0,
      `exit=${restored.code} signal=${restored.signal || '-'} error=${restored.error?.message || '-'}`);
    check('最终文件 SHA-256 与运行前一致',
      sha256(fs.readFileSync(targetPath)) === originalHash);
  } catch (error) {
    check('suite 未发生未处理异常', false, error.stack || error.message);
  } finally {
    try {
      forceRestore();
      check('finally 恢复 SHA-256', sha256(fs.readFileSync(targetPath)) === originalHash);
    } catch (error) {
      check('finally 恢复目标文件', false, error.message);
    }
    fs.rmSync(tempDir, { recursive: true, force: true });
  }

  console.log(`\n${passed} passed, ${failures.length} failed`);
  if (failures.length) {
    console.log('FAILURES:');
    for (const failure of failures) console.log(' ', failure);
  }
  return { ok: failures.length === 0, passed, failed: failures.length, failures, originalHash };
}
