// Native-Windows bridge for the C18B Python guard. The child exit code comes
// directly from wsl.exe; no shell or pipeline can turn a red guard green.
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const result = await run('wsl.exe', ['-d', 'Ubuntu-22.04', '--cd',
  '/mnt/d/VScode/test/gold-dashboard', '--', 'python3', '-B',
  'derive_cbo_scenario_basis.py', '--test']);
process.stdout.write(result.stdout);
process.stderr.write(result.stderr);
console.log(`C18B_PYTHON_EXIT derive_cbo_scenario_basis.py=${String(result.code)}`);
process.exitCode = result.code === 0 && !result.signal && !result.error ? 0 : 1;

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
