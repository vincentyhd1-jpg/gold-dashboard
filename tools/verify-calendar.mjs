// Python 本地不可用（Store 占位符 + Application Control 拦截），
// 这里用 JS 逐行等价移植 trading_calendar.py 与 expected_trade_date()，
// 对 20 次真实运行做回归。等价性靠人工逐行比对保证，不能替代 --test 实跑。

const CME_HOLIDAYS = new Set([
  "2027-01-01","2027-01-18","2027-02-15","2027-03-26",
  "2027-05-31","2027-07-05","2027-09-06","2027-11-25","2027-12-24",
  "2026-01-01","2026-01-19","2026-02-16","2026-04-03",
  "2026-05-25","2026-07-03","2026-09-07","2026-11-26","2026-12-25",
  "2025-01-01","2025-01-20","2025-02-17","2025-04-18",
  "2025-05-26","2025-07-04","2025-09-01","2025-11-27","2025-12-25",
]);

const iso = d => d.toISOString().slice(0, 10);
const mk  = s => new Date(s + "T00:00:00Z");
const add = (d, n) => { const x = new Date(d); x.setUTCDate(x.getUTCDate() + n); return x; };

function isTradingDay(d) {
  const w = d.getUTCDay();          // JS: 0=Sun..6=Sat
  if (w === 0 || w === 6) return false;   // py: weekday()>=5 → Sat/Sun
  return !CME_HOLIDAYS.has(iso(d));
}
function prevTradingDay(d) { let x = add(d, -1); while (!isTradingDay(x)) x = add(x, -1); return x; }
function latestTradingDayOnOrBefore(d) { let x = new Date(d); while (!isTradingDay(x)) x = add(x, -1); return x; }
function tradingDaysBetween(a, b) {     // 不含两端
  let c = 0;
  for (let x = add(a, 1); x < b; x = add(x, 1)) if (isTradingDay(x)) c++;
  return c;
}

const PUBLISH_HOUR_UTC = 14;

function expectedTradeDate(runIso, hour) {
  const today = mk(runIso);
  let expected = isTradingDay(today)
    ? prevTradingDay(today)
    : latestTradingDayOnOrBefore(today);
  if (hour < PUBLISH_HOUR_UTC) expected = prevTradingDay(expected);
  return expected;
}

// 取自 git log -- data/oi.json
const OBSERVED = [
  ["2026-07-28T23:09","2026-07-27"],["2026-07-25T19:12","2026-07-24"],
  ["2026-07-24T23:09","2026-07-23"],["2026-07-23T23:04","2026-07-22"],
  ["2026-07-22T23:10","2026-07-21"],["2026-07-21T23:04","2026-07-20"],
  ["2026-07-18T19:10","2026-07-17"],["2026-07-17T22:56","2026-07-16"],
  ["2026-07-16T23:04","2026-07-15"],["2026-07-15T23:04","2026-07-14"],
  ["2026-07-14T23:03","2026-07-13"],["2026-07-11T19:09","2026-07-10"],
  ["2026-07-10T23:06","2026-07-09"],["2026-07-09T23:19","2026-07-08"],
  ["2026-07-08T23:17","2026-07-07"],["2026-07-07T23:08","2026-07-06"],
  ["2026-07-03T23:13","2026-07-02"],["2026-07-02T23:16","2026-07-01"],
  ["2026-07-01T23:22","2026-06-30"],["2026-06-30T23:20","2026-06-29"],
];

const DOW = ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"];
let bad = 0;
console.log("=== 修正后模型（预期 = 上一个交易日）===");
for (const [run, want] of OBSERVED) {
  const [d, t] = run.split("T");
  const got = iso(expectedTradeDate(d, +t.slice(0, 2)));
  const ok = got === want;
  if (!ok) bad++;
  const note = !isTradingDay(mk(d)) ? "  (非交易日运行)" : "";
  console.log(`  ${d} ${DOW[mk(d).getUTCDay()]} ${t}  →  ${got}  ${ok ? "OK" : "期望 " + want}${note}`);
}
console.log(bad ? `\n${bad}/${OBSERVED.length} 不符` : `\n${OBSERVED.length}/${OBSERVED.length} 全部复现`);

// 对照：第一版按"当天"建模会误判多少
let wrong = 0;
for (const [run, want] of OBSERVED) {
  const [d, t] = run.split("T");
  const h = +t.slice(0, 2);
  let exp = latestTradingDayOnOrBefore(mk(d));
  if (h < 20 && isTradingDay(mk(d))) exp = prevTradingDay(exp);
  if (iso(exp) !== want) wrong++;
}
console.log(`\n=== 对照：第一版"当天"模型 ===\n  ${wrong}/${OBSERVED.length} 会被误判为陈旧 → 每天隔离好数据`);

// lag 计算与 MAX_STALE_TRADING_DAYS = 0 的判定
console.log("\n=== lag 判定（MAX_STALE_TRADING_DAYS = 0）===");
for (const [p, w, label] of [
  ["2026-07-23","2026-07-24","滞后 1 个交易日"],
  ["2026-07-22","2026-07-24","滞后 2 个交易日"],
  ["2026-07-02","2026-07-06","跨假日+周末滞后"],
]) {
  const lag = tradingDaysBetween(mk(p), mk(w)) + 1;
  console.log(`  ${p} vs 预期 ${w}  lag=${lag}  ${lag > 0 ? "判失败" : "通过"}  (${label})`);
}
