// Native-Windows bridge for the C18C Python guard. The repository root is
// derived from this verifier, converted by WSL, and passed without a shell.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const distro = 'Ubuntu-22.04';
const portableWindowsRoot = root.split(path.sep).join('/');
const converted = await run('wsl.exe', ['-d', distro, '--',
  'wslpath', '-a', portableWindowsRoot]);

if (!succeeded(converted) || !converted.stdout.trim()) {
  showFailure('Windows to WSL path conversion', converted);
  process.exitCode = 1;
} else {
  const convertedRoot = converted.stdout.trim();
  const canonical = await run('wsl.exe', ['-d', distro, '--',
    'readlink', '-f', convertedRoot]);
  if (!succeeded(canonical) || !canonical.stdout.trim()) {
    showFailure('WSL repo path canonicalization', canonical);
    process.exitCode = 1;
  } else {
    const wslRoot = canonical.stdout.trim();
    const pythonEntry = [
      'import os, runpy, sys',
      'print("C18C_GUARD_CWD=" + os.path.realpath(os.getcwd()), flush=True)',
      'sys.argv = ["derive_fiscal_risk_monitor.py", "--test"]',
      'runpy.run_path("derive_fiscal_risk_monitor.py", run_name="__main__")',
    ].join('; ');
    const result = await run('wsl.exe', ['-d', distro, '--cd', wslRoot, '--',
      'python3', '-B', '-c', pythonEntry]);
    process.stdout.write(result.stdout);
    process.stderr.write(result.stderr);
    console.log(`C18C_WINDOWS_ROOT=${root}`);
    console.log(`C18C_WSL_ROOT=${wslRoot}`);
    const actualCwd = result.stdout.match(/^C18C_GUARD_CWD=(.+)$/m)?.[1]?.trim();
    const cwdMatches = actualCwd === wslRoot;
    if (!cwdMatches) {
      console.error(`C18C execution path mismatch: cwd=${actualCwd || '<missing>'} expected=${wslRoot}`);
    }
    console.log(`C18C_PYTHON_EXIT derive_fiscal_risk_monitor.py=${String(result.code)}`);
    process.exitCode = succeeded(result) && cwdMatches ? 0 : 1;
  }
}

function succeeded(result) {
  return result.code === 0 && result.signal == null && result.error == null;
}

function showFailure(label, result) {
  process.stdout.write(result.stdout || '');
  process.stderr.write(result.stderr || '');
  console.error(`${label} failed: exit=${String(result.code)} signal=${result.signal || '-'} error=${result.error?.message || '-'}`);
}

function run(command, args) {
  return new Promise(resolve => {
    let stdout = '';
    let stderr = '';
    let settled = false;
    let child;
    try {
      child = spawn(command, args, { cwd: root, shell: false });
    } catch (error) {
      resolve({ code: null, signal: null, error, stdout, stderr });
      return;
    }
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr += chunk; });
    child.once('error', error => {
      if (!settled) {
        settled = true;
        resolve({ code: null, signal: null, error, stdout, stderr });
      }
    });
    child.once('close', (code, signal) => {
      if (!settled) {
        settled = true;
        resolve({ code, signal, error: null, stdout, stderr });
      }
    });
  });
}
