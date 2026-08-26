import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = path.join(ROOT, 'dist');
const CONFIG = path.join(ROOT, 'wrangler.jsonc');
const ROOT_PAGES = ['index.html', 'macro.html', 'term-3d.html'];
const PUBLIC_DIRS = ['assets', 'css', 'js'];
const REQUIRED_PATHS = [
  ...ROOT_PAGES,
  'assets/favicon.svg',
  'css/chart.css',
  'js/data-helpers.js',
  'data/cot.json',
  'data/derived/macro_debt.json',
];
const FORBIDDEN_PATHS = [
  'AGENTS.md', 'CLAUDE.md', 'README.md',
  'fetch_fred.py', 'fetch_treasury_debt.py',
  'tools', 'docs', '.github', '.git', 'data/quarantine',
];

let passed = 0;
let failed = 0;

function check(name, ok, detail = '') {
  if (ok) {
    passed++;
    console.log(`PASS ${name}${detail ? `  ${detail}` : ''}`);
  } else {
    failed++;
    console.log(`FAIL ${name}${detail ? `  ${detail}` : ''}`);
  }
}

function slash(relativePath) {
  return relativePath.split(path.sep).join('/');
}

function listFiles(root) {
  if (!fs.existsSync(root)) return [];
  const files = [];
  const visit = directory => {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        files.push(`${slash(path.relative(root, absolute))}::SYMLINK`);
      } else if (entry.isDirectory()) {
        visit(absolute);
      } else if (entry.isFile()) {
        files.push(slash(path.relative(root, absolute)));
      }
    }
  };
  visit(root);
  return files.sort();
}

function hash(file) {
  return crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex');
}

function sourceManifest() {
  const files = [...ROOT_PAGES];
  for (const directory of PUBLIC_DIRS) {
    files.push(...listFiles(path.join(ROOT, directory))
      .map(file => `${directory}/${file}`));
  }
  files.push(...listFiles(path.join(ROOT, 'data'))
    .filter(file => !file.split('/').includes('quarantine')
      && path.extname(file).toLowerCase() === '.json')
    .map(file => `data/${file}`));
  return files.sort();
}

let config;
try {
  config = JSON.parse(fs.readFileSync(CONFIG, 'utf8'));
  check('wrangler.jsonc 是可解析配置', true);
} catch (error) {
  check('wrangler.jsonc 是可解析配置', false, error.message);
}
check('wrangler name 固定为 gold-dashboard', config?.name === 'gold-dashboard',
  String(config?.name));
check('wrangler assets.directory 固定为 ./dist',
  config?.assets?.directory === './dist', String(config?.assets?.directory));
check('纯静态 assets 配置没有 Worker main',
  config && !Object.hasOwn(config, 'main'));
check('wrangler 无多余 bindings', config
  && Object.keys(config).sort().join(',') === 'assets,compatibility_date,name'
  && Object.keys(config.assets || {}).join(',') === 'directory');

check('dist 目录存在', fs.existsSync(DIST) && fs.statSync(DIST).isDirectory());
for (const required of REQUIRED_PATHS) {
  check(`dist 必需文件 ${required}`, fs.existsSync(path.join(DIST, required)));
}

const actual = listFiles(DIST);
const expected = sourceManifest();
const missing = expected.filter(file => !actual.includes(file));
const extra = actual.filter(file => !expected.includes(file));
check('dist 完整复制白名单 manifest', missing.length === 0,
  missing.length ? `missing=${missing.join(',')}` : `${expected.length} files`);
check('dist 不包含白名单外文件', extra.length === 0,
  extra.length ? `extra=${extra.join(',')}` : 'no extras');

const byteMismatches = expected.filter(file => fs.existsSync(path.join(DIST, file))
  && hash(path.join(ROOT, file)) !== hash(path.join(DIST, file)));
check('dist 文件与源文件逐字节一致', byteMismatches.length === 0,
  byteMismatches.join(','));

for (const forbidden of FORBIDDEN_PATHS) {
  check(`dist 不公开 ${forbidden}`, !fs.existsSync(path.join(DIST, forbidden)));
}
check('dist 不包含 Python/Markdown/PDF/临时文件', actual.every(file =>
  !/\.(?:py|md|pdf|tmp|bak)$/i.test(file) && !file.endsWith('::SYMLINK')),
  actual.filter(file => /\.(?:py|md|pdf|tmp|bak)$/i.test(file)
    || file.endsWith('::SYMLINK')).join(','));

console.log(`${passed} passed, ${failed} failed`);
process.exitCode = failed ? 1 : 0;
