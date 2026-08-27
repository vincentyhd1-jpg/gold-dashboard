// Native-Windows bridge for C17 Python guards. The child exit code comes
// directly from wsl.exe; no shell, pipeline, `$?`, or `$LASTEXITCODE` layer can
// turn a red Python guard into a false green.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const wslRoot = '/mnt/d/VScode/test/gold-dashboard';
const scripts = ['fetch_treasury_fiscal.py', 'derive_fiscal_stress.py'];
let failed = false;

for (const script of scripts) {
  const result = await run('wsl.exe', ['-d', 'Ubuntu-22.04', '--cd', wslRoot,
    '--', 'python3', '-B', script, '--test']);
  process.stdout.write(result.stdout);
  process.stderr.write(result.stderr);
  console.log(`C17_PYTHON_EXIT ${script}=${String(result.code)}`);
  if (result.code !== 0 || result.signal || result.error) failed = true;
}

process.exitCode = failed ? 1 : 0;

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
