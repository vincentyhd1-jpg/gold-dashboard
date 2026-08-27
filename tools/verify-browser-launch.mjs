import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { BrowserEnvironmentError, launchChromium } from './_browser.mjs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
let passed = 0;
let failed = 0;

function check(name, condition, detail = '') {
  if (condition) {
    passed++;
    console.log(`  PASS  ${name}`);
  } else {
    failed++;
    console.log(`  FAIL  ${name}${detail ? `  ${detail}` : ''}`);
  }
}

const expectedBrowserOwners = new Set([
  'playback-check.mjs',
  'verify-contract-contango.mjs',
  'verify-cot-sentinel-strict.mjs',
  'verify-envelope-helper-raw-inputs.mjs',
  'verify-fiscal-stress-page.mjs',
  'verify-gapframe.mjs',
  'verify-isolation.mjs',
  'verify-live.mjs',
  'verify-macro-page.mjs',
  'verify-playback.mjs',
  'verify-schema-coupling.mjs',
  'verify-ui-fixes.mjs',
]);

const files = fs.readdirSync(__dirname)
  .filter(name => name.endsWith('.mjs'))
  .sort();
const sources = new Map(files.map(name => [
  name,
  fs.readFileSync(path.join(__dirname, name), 'utf8'),
]));

const directLaunchPattern = new RegExp(
  ['\\bchromium\\s*\\.', 'launch\\s*\\('].join(''),
);
const fixedRevisionPattern = new RegExp(['chromium', '\\d+'].join('-'), 'i');
const cacheToken = ['ms', 'playwright'].join('-');
const userCachePattern = /[A-Za-z]:[\\/]+Users[\\/]+[^\s'"`]+[\\/]+AppData[\\/]+Local/i;

const missingOwners = [...expectedBrowserOwners].filter(name => !sources.has(name));
check('12 个 browser-owning 脚本清单完整', missingOwners.length === 0,
      `missing=${missingOwners.join(',')}`);

const notMigrated = [...expectedBrowserOwners].filter(name => {
  const source = sources.get(name) || '';
  return !source.includes("from './_browser.mjs'")
    || !source.includes('launchChromium(');
});
check('12 个 browser-owning 脚本全部调用公共 helper', notMigrated.length === 0,
      `violations=${notMigrated.join(',')}`);

const directLaunchViolations = files.filter(name =>
  name !== '_browser.mjs' && directLaunchPattern.test(sources.get(name)));
check('普通脚本不直接调用 chromium.launch', directLaunchViolations.length === 0,
      `violations=${directLaunchViolations.join(',')}`);

const userPathViolations = files.filter(name => userCachePattern.test(sources.get(name)));
check('tools/*.mjs 无用户专属 Playwright 缓存绝对路径', userPathViolations.length === 0,
      `violations=${userPathViolations.join(',')}`);

const cacheScanViolations = files.filter(name =>
  sources.get(name).toLowerCase().includes(cacheToken));
check('tools/*.mjs 不扫描 Playwright 浏览器缓存目录', cacheScanViolations.length === 0,
      `violations=${cacheScanViolations.join(',')}`);

const revisionViolations = files.filter(name => fixedRevisionPattern.test(sources.get(name)));
check('tools/*.mjs 无固定 Chromium revision', revisionViolations.length === 0,
      `violations=${revisionViolations.join(',')}`);

const executablePathViolations = files.filter(name =>
  !new Set(['_browser.mjs', 'verify-browser-launch.mjs']).has(name)
  && sources.get(name).includes('executablePath'));
check('executablePath 只存在于 helper 与基础设施 guard',
      executablePathViolations.length === 0,
      `violations=${executablePathViolations.join(',')}`);

let browser;
try {
  browser = await launchChromium();
  check('公共 helper 返回真实 Browser 对象',
        typeof browser?.newContext === 'function' && Boolean(browser.version()),
        `version=${browser?.version?.()}`);
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.setContent('<!doctype html><body>browser-launch-ok</body>');
  const result = await page.evaluate(() => ({
    text: document.body.textContent,
    sum: 1 + 1,
  }));
  check('真实页面可执行 DOM 与 JavaScript',
        result.text === 'browser-launch-ok' && result.sum === 2,
        JSON.stringify(result));
  await context.close();
} catch (error) {
  check('公共 helper 真实启动 Chromium', false, error?.message || String(error));
} finally {
  if (browser) await browser.close();
}

const missingExecutable = process.platform === 'win32'
  ? 'X:\\definitely-not-existing\\chrome.exe'
  : '/definitely-not-existing/chrome';
let missingError;
try {
  await launchChromium({}, { executablePath: missingExecutable });
} catch (error) {
  missingError = error;
}
check('不存在 executable 必须抛 BrowserEnvironmentError',
      missingError instanceof BrowserEnvironmentError
      && missingError.stage === 'executable access'
      && missingError.code === 'ENOENT');
check('不存在 executable 的诊断含 browser/path/code 且不 fallback',
      missingError?.browserType === 'chromium'
      && missingError?.executablePath === missingExecutable
      && missingError?.message.includes('ENOENT'));

let launchCalls = 0;
const eperm = Object.assign(new Error('simulated access denied'), { code: 'EPERM' });
let epermError;
try {
  await launchChromium({}, {
    executablePath: missingExecutable,
    accessSync: () => { throw eperm; },
    browserType: {
      executablePath: () => missingExecutable,
      launch: async () => { launchCalls++; return {}; },
    },
  });
} catch (error) {
  epermError = error;
}
check('EPERM 被标记为浏览器环境错误并继续抛出',
      epermError instanceof BrowserEnvironmentError
      && epermError.code === 'EPERM'
      && epermError.stage === 'executable access');
check('EPERM 不启动 fallback 浏览器、不转 PASS/SKIP', launchCalls === 0,
      `launchCalls=${launchCalls}`);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
