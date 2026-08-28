// Native-Windows bridge for the C18B Python guard. Repository paths are
// resolved from this verifier, converted by WSL itself, and passed as argv;
// no shell or pipeline can redirect the guard to a different checkout.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const distro = 'Ubuntu-22.04';
// wsl.exe consumes backslashes while forwarding argv to a Linux process.
// Forward slashes preserve the dynamically resolved Windows path verbatim.
const portableWindowsRoot = root.split(path.sep).join('/');
const converted = await run('wsl.exe', ['-d', distro, '--',
  'wslpath', '-a', portableWindowsRoot]);

if (!succeeded(converted) || !converted.stdout.trim()) {
  showFailure('Windows → WSL path conversion', converted);
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
      'print("C18B_GUARD_CWD=" + os.path.realpath(os.getcwd()), flush=True)',
      'sys.argv = ["derive_cbo_scenario_basis.py", "--test"]',
      'runpy.run_path("derive_cbo_scenario_basis.py", run_name="__main__")',
    ].join('; ');
    const result = await run('wsl.exe', ['-d', distro, '--cd', wslRoot, '--',
      'python3', '-B', '-c', pythonEntry]);
    process.stdout.write(result.stdout);
    process.stderr.write(result.stderr);
    console.log(`C18B_WINDOWS_ROOT=${root}`);
    console.log(`C18B_WSL_ROOT=${wslRoot}`);
    const actualCwd = result.stdout.match(/^C18B_GUARD_CWD=(.+)$/m)?.[1]?.trim();
    const cwdMatches = actualCwd === wslRoot;
    if (!cwdMatches) {
      console.error(`C18B execution path mismatch: cwd=${actualCwd || '<missing>'} expected=${wslRoot}`);
    }
    console.log(`C18B_PYTHON_EXIT derive_cbo_scenario_basis.py=${String(result.code)}`);
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
