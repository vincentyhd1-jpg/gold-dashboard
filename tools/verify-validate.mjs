// validate() 的 JS 等价移植 + fetch_oi.py --test 里那批断言的实跑。
// Python 本地不可用（Store 占位符 + Application Control 拦截），这是次优替代：
// 等价性靠人工逐行比对保证，不能替代 CI 上的 `python fetch_oi.py --test`。

const CME_HOLIDAYS = new Set([
  "2027-01-01","2027-01-18","2027-02-15","2027-03-26",
  "2027-05-31","2027-07-05","2027-09-06","2027-11-25","2027-12-24",
  "2026-01-01","2026-01-19","2026-02-16","2026-04-03",
  "2026-05-25","2026-07-03","2026-09-07","2026-11-26","2026-12-25",
  "2025-01-01","2025-01-20","2025-02-17","2025-04-18",
  "2025-05-26","2025-07-04","2025-09-01","2025-11-27","2025-12-25",
]);
const YEARS = [...CME_HOLIDAYS].map(d => +d.slice(0, 4));
const MIN_Y = Math.min(...YEARS), MAX_Y = Math.max(...YEARS);

const iso = d => d.toISOString().slice(0, 10);
const mk  = s => new Date(s + "T00:00:00Z");
const add = (d, n) => { const x = new Date(d); x.setUTCDate(x.getUTCDate() + n); return x; };

const isTradingDay = d => {
  const w = d.getUTCDay();
  return w !== 0 && w !== 6 && !CME_HOLIDAYS.has(iso(d));
};
const isCalendarCovered = d => d.getUTCFullYear() >= MIN_Y && d.getUTCFullYear() <= MAX_Y;
const prevTradingDay = d => { let x = add(d, -1); while (!isTradingDay(x)) x = add(x, -1); return x; };
const latestOnOrBefore = d => { let x = new Date(d); while (!isTradingDay(x)) x = add(x, -1); return x; };
const tradingDaysBetween = (a, b) => {
  let c = 0; for (let x = add(a, 1); x < b; x = add(x, 1)) if (isTradingDay(x)) c++; return c;
};

const PUBLISH_HOUR_UTC = 14;
const MAX_STALE_TRADING_DAYS = 0;
const MAX_CONTRACT_COUNT_DELTA = 3;

function expectedTradeDate(now) {
  const today = mk(iso(now));
  let exp = isTradingDay(today) ? prevTradingDay(today) : latestOnOrBefore(today);
  if (now.getUTCHours() < PUBLISH_HOUR_UTC) exp = prevTradingDay(exp);
  return exp;
}

function monthsIdentical(a, b) {
  if (a.length !== b.length) return false;
  const k = r => r.month;
  const A = [...a].sort((x, y) => k(x) < k(y) ? -1 : 1);
  const B = [...b].sort((x, y) => k(x) < k(y) ? -1 : 1);
  return A.every((x, i) =>
    x.month === B[i].month && x.settle === B[i].settle && x.oi === B[i].oi);
}

function validate(entry, records, now) {
  const failures = [];
  const months = entry.months || [];
  const parsed = mk(entry.date);

  if (entry.date_unparsed) {
    failures.push(`a) PDF 内未解析到 Trade Date，${entry.date} 为占位日期 —— PDF 版式可能已变更`);
  } else if (!isTradingDay(parsed)) {
    failures.push(`a) PDF 内 Trade Date ${entry.date} 不是交易日（周末或 CME 假日）`);
  } else if (!isCalendarCovered(parsed)) {
    failures.push(`a) ${entry.date} 超出假日表覆盖范围`);
  } else {
    const want = expectedTradeDate(now);
    if (parsed > want) {
      failures.push(`a) Trade Date ${entry.date} 晚于预期 ${iso(want)} —— 发布时刻模型或假日表可能需要修正`);
    } else if (parsed < want) {
      const lag = tradingDaysBetween(parsed, want) + 1;
      if (lag > MAX_STALE_TRADING_DAYS) {
        failures.push(`a) Trade Date 陈旧：PDF 为 ${entry.date}，预期 ${iso(want)}，滞后 ${lag} 个交易日 —— CME 'current' 文件未更新`);
      }
    }
  }

  let prevRec = null;
  for (const r of [...records].sort((x, y) => x.date < y.date ? 1 : -1)) {
    if (r.date < entry.date && (r.months || []).length) { prevRec = r; break; }
  }

  if (prevRec && monthsIdentical(months, prevRec.months)) {
    failures.push(`b) 全部 ${months.length} 个交割月的结算价与持仓与 ${prevRec.date} 完全相同 —— 重复/陈旧数据`);
  }

  if (prevRec) {
    const delta = months.length - prevRec.months.length;
    if (Math.abs(delta) > MAX_CONTRACT_COUNT_DELTA) {
      failures.push(`c) 交割月数量 ${prevRec.months.length} → ${months.length}，超出阈值 ±${MAX_CONTRACT_COUNT_DELTA}`);
    }
  }

  if (!months.length) {
    failures.push("d) 未解析到任何交割月");
  } else {
    const front = months.reduce((a, b) => (b.oi || 0) > (a.oi || 0) ? b : a, months[0]);
    if (!front.oi) failures.push(`d) 主力月 ${front.month} 持仓为 ${front.oi} —— 解析失败`);
  }

  return failures;
}

// ── 断言（与 fetch_oi.py run_tests 一一对应）─────────────────────────────
let pass = 0, fail = 0;
const check = (name, cond, detail = "") => {
  if (cond) { pass++; console.log(`  PASS  ${name}`); }
  else { fail++; console.log(`  FAIL  ${name}  ${JSON.stringify(detail)}`); }
};

const now = new Date("2026-07-27T22:00:00Z");
const GOOD_DATE = "2026-07-24";
const prev = { date: "2026-07-23", months: [
  { month: "AUG26", settle: 4100.0, oi: 200000 },
  { month: "DEC26", settle: 4180.0, oi: 150000 },
  { month: "FEB27", settle: 4200.0, oi: 20000 },
]};
const records = [prev];
const at = (d, m) => ({ date: d, months: m });
const good = [
  { month: "AUG26", settle: 4090.0, oi: 170000 },
  { month: "DEC26", settle: 4175.0, oi: 176000 },
  { month: "FEB27", settle: 4195.0, oi: 20500 },
];

console.log("[validate]");
check("正常数据 → 无失败", validate(at(GOOD_DATE, good), records, now).length === 0,
      validate(at(GOOD_DATE, good), records, now));

let f = validate({ ...at(GOOD_DATE, good), date_unparsed: true }, records, now);
check("a) Trade Date 未解析（占位日期=预期值）→ 命中", f.some(x => x.includes("未解析到 Trade Date")), f);

f = validate(at("2026-07-23", good), records, now);
check("a) Trade Date 滞后 1 个交易日 → 命中", f.some(x => x.includes("陈旧")), f);

f = validate(at("2026-07-27", good), records, now);
check("a) Trade Date 晚于预期 → 命中", f.some(x => x.includes("晚于预期")), f);

f = validate(at("2026-07-26", good), records, now);
check("a) 非交易日 → 命中", f.some(x => x.includes("不是交易日")), f);

f = validate(at(GOOD_DATE, prev.months), records, now);
check("b) 逐字段完全相同 → 命中", f.some(x => x.startsWith("b)")), f);

const almost = prev.months.map(m => ({ ...m }));
almost[0].oi += 1;
f = validate(at(GOOD_DATE, almost), records, now);
check("b) 单合约变动 1 手 → 不命中", !f.some(x => x.startsWith("b)")), f);

const priceOnly = prev.months.map(m => ({ ...m, settle: m.settle + 0.1 }));
f = validate(at(GOOD_DATE, priceOnly), records, now);
check("b) 仅结算价变动 → 不命中", !f.some(x => x.startsWith("b)")), f);

const many = [...good, ...Array.from({ length: 5 }, (_, i) =>
  ({ month: `X${String(i).padStart(2, "0")}`, settle: 1.0, oi: 1 }))];
f = validate(at(GOOD_DATE, many), records, now);
check("c) 合约数 3 → 8 → 命中", f.some(x => x.startsWith("c)")), f);

f = validate(at(GOOD_DATE, good.slice(0, 2)), records, now);
check("c) 合约数 3 → 2（阈值内）→ 不命中", !f.some(x => x.startsWith("c)")), f);

f = validate(at(GOOD_DATE, good.map(m => ({ ...m, oi: 0 }))), records, now);
check("d) 主力月 OI = 0 → 命中", f.some(x => x.startsWith("d)")), f);

f = validate(at(GOOD_DATE, good.map(m => ({ ...m, oi: null }))), records, now);
check("d) 主力月 OI = None → 命中", f.some(x => x.startsWith("d)")), f);

f = validate(at(GOOD_DATE, []), records, now);
check("d) 无交割月 → 命中", f.some(x => x.startsWith("d)")), f);

f = validate(at(GOOD_DATE, good), [], now);
check("首次运行（库为空）→ 无失败", f.length === 0, f);

// 真实数据回放：现有 21 条逐条过校验，模拟每天在 T+1 23:00 UTC 抓到它
console.log("\n[真实数据回放] data/oi.json 21 条");
const fs = await import("fs");
const oi = JSON.parse(fs.readFileSync("data/oi.json", "utf8")).filter(r => (r.months || []).length);
let falsePos = 0;
for (let i = 0; i < oi.length; i++) {
  const runDay = add(mk(oi[i].date), 1);
  const runAt = new Date(iso(runDay) + "T23:00:00Z");
  const fs2 = validate(oi[i], oi.slice(0, i), runAt);
  if (fs2.length) { falsePos++; console.log(`  ${oi[i].date}  ${JSON.stringify(fs2)}`); }
}
check(`21 条历史好数据全部通过（无误隔离）`, falsePos === 0, `${falsePos} 条被误判`);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
