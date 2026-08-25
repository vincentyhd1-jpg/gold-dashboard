// 模块隔离：注入单个模块的渲染故障，验证其余模块照常渲染、异常带模块名打到
// console，且 footer 不谎报「数据文件未找到」。
//
// 判定机制：断言累加器 + 结束时按累计失败数退出。
// 此前本脚本只打印状态、无任何断言，退出码仅反映「脚本自身是否抛异常」——
// 三个模块全挂只要脚本不抛也是 exit 0，等于这条护栏一直不携带信息。
// 所有断言跑完才退出（不首个失败就 return），否则看不到全部问题。
import { launchChromium } from './_browser.mjs';

const browser = await launchChromium();

let pass = 0, fail = 0;
const check = (name, ok, detail = '') => {
  if (ok) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}  ${JSON.stringify(detail)}`); }
};

// 七个图表实例 → 所属模块。判定「谁该挂、谁不该挂」按模块聚合。
const CHART_MODULE = {
  cotDual:    'COT/金价',
  cotIndex:   'COT/金价',
  stocks:     'COT/金价',      // 库存图在 renderAll 里
  depotTrend: '仓库趋势',
  oiMain:     '期限结构',
  oiDelta:    '期限结构',
  oiRoll:     '期限结构',
};

async function probe(label, patch) {
  const page = await browser.newPage({ viewport: { width: 1400, height: 1000 } });
  const errors = [];
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  page.on('pageerror', e => errors.push('[pageerror] ' + e.message));

  // 拦 root HTML 与拆出的 js/*.js：被注入的函数可能定义在任一文件里
  // （initOIPlayback 已移到 js/playback.js，只拦 root 会让注入变成空操作）。
  await page.route(/^http:\/\/localhost:3001\/(|js\/[\w-]+\.js)$/, async route => {
    const response = await route.fetch();
    let body = await response.text();
    body = patch(body);
    await route.fulfill({ response, body });
  });

  // 不用 networkidle：Chart.js 走 CDN，网络不畅时该事件永不触发（实测超时 30s）。
  // 这里也不能等某个具体图表实例 —— 本脚本故意注入渲染故障，被打掉的图表
  // 本就不会出现。改等 footerNote 被改写：它在 .then 与 .catch 的开头都会执行，
  // 是「渲染流程已跑完」的可靠信号。
  await page.goto('http://localhost:3001', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await page.waitForFunction(
    () => typeof Chart !== 'undefined'
      && !/每周六自动更新/.test(document.getElementById('footerNote')?.textContent || ''),
    { timeout: 60000 });
  await page.waitForTimeout(1200);

  const state = await page.evaluate(() => ({
    footer: document.getElementById('footerNote')?.textContent.slice(0, 30),
    playDate: document.getElementById('oiPlayDate')?.textContent,
    sliderMax: document.getElementById('oiPlaySlider')?.max,
    front: document.getElementById('oiFrontMonth')?.textContent,
    // Chart.js 实例存在即视为该模块渲染成功
    cotDual:    !!Chart.getChart('cotDualChart'),
    cotIndex:   !!Chart.getChart('cotIndexChart'),
    stocks:     !!Chart.getChart('stocksChart'),
    depotTrend: !!Chart.getChart('depotTrendChart'),
    oiMain:     !!Chart.getChart('oiChart'),
    oiDelta:    !!Chart.getChart('oiDeltaChart'),
    oiRoll:     !!Chart.getChart('oiRollChart'),
  }));

  console.log(`\n=== ${label} ===`);
  console.log('state  :', JSON.stringify(state));
  console.log('errors :', errors.length ? errors.map(e => e.slice(0, 90)) : 'none');
  await page.close();
  return { state, errors };
}

/**
 * 断言一次探测的结果。
 * @param brokenModules 期望挂掉的模块名（空数组 = 基线，全都该活）
 */
function assertIsolation(label, { state, errors }, brokenModules) {
  const broken = new Set(brokenModules);

  // 1. 该挂的挂了，该活的活着 —— 逐个图表检查，失败信息带模块名
  for (const [chart, mod] of Object.entries(CHART_MODULE)) {
    const shouldLive = !broken.has(mod);
    check(`${label}: ${chart}（${mod}）${shouldLive ? '存活' : '已挂'}`,
          state[chart] === shouldLive,
          { chart, module: mod, expected: shouldLive, actual: state[chart] });
  }

  // 2. 每个被注入的模块都要在 console 里报出自己的名字 ——
  //    _safeRender 的意义就在于「异常带模块名打出来」，只挂不报等于静默降级
  for (const mod of brokenModules) {
    check(`${label}: console 报出「${mod}」渲染失败`,
          errors.some(e => e.includes(mod)),
          errors.map(e => e.slice(0, 70)));
  }

  // 3. footer 不得谎报「数据文件未找到」——
  //    渲染异常与数据缺失是两件事，混同会掩盖真实原因
  check(`${label}: footer 未谎报「数据文件未找到」`,
        !/数据文件未找到/.test(state.footer || ''),
        state.footer);

  // 4. 基线下不该有任何 console error
  if (brokenModules.length === 0) {
    check(`${label}: 无 console error`, errors.length === 0,
          errors.map(e => e.slice(0, 70)));
  }
}

const INJECT = {
  depotTrend: b => b.replace(
    'function renderDepotTrend(',
    'function renderDepotTrend() { throw new Error("INJECTED depot trend failure"); }\nfunction _unused_renderDepotTrend('),
  playback: b => b.replace(
    'function initOIPlayback(',
    'function initOIPlayback() { throw new Error("INJECTED playback failure"); }\nfunction _unused_initOIPlayback('),
};

// 1. 无注入：基线，七个图表全活
assertIsolation('baseline',
  await probe('baseline (no injection)', b => b), []);

// 2. 单模块故障：仓库趋势挂，其余六个图表照常
assertIsolation('renderDepotTrend throws',
  await probe('renderDepotTrend throws', INJECT.depotTrend), ['仓库趋势']);

// 3. 单模块故障：期限结构三图全挂，COT/金价 与 仓库趋势 照常
assertIsolation('initOIPlayback throws',
  await probe('initOIPlayback throws', INJECT.playback), ['期限结构']);

// 4. 双模块同时故障：两条都要报出来，验证累加器真在累加、
//    以及两个模块的失败互不掩盖
assertIsolation('both throw',
  await probe('renderDepotTrend + initOIPlayback throw',
              b => INJECT.playback(INJECT.depotTrend(b))),
  ['仓库趋势', '期限结构']);

await browser.close();

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
