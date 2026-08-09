#!/usr/bin/env python3
"""
端到端注入：三个采集脚本的校验闸是否真的拒绝落盘 + 非零退出？

单元测试只证明 validate_*() 会返回失败原因，不证明 main() 真的拦住了写盘。
这里 monkeypatch 掉网络层，喂坏数据，断言三件事：
  1. exit code 正确（1 = 校验失败 / 2 = 上游未更新 / 0 = 正常）
  2. 目标 JSON 未被覆盖（内容哈希不变）
  3. data/quarantine/ 下生成了对应文件

用法：python3 tools/verify-fetch-gates.py
"""

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
QUAR = os.path.join(DATA, "quarantine")

# 读 data/*.json 一律经 unwrap()，容裸格式与信封两种形状。
# 直读顶层结构（real_st[-1] / cot["weekly"]）会在对应源信封化时崩，而那个
# 断裂**本地不暴露** —— 磁盘上的文件要等下一次 Actions 跑才变形，本地全绿。
sys.path.insert(0, ROOT)
from data_envelope import unwrap                                    # noqa: E402

passed = failed = 0

# label -> warnings，最后横向比对三个 gold 用例的文案是否真的可区分
NULL_WARNS = {}


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}  {detail}")


def sha(path):
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _price_values(data):
    """从任意形状的业务数据里挖出所有 price 值（用于「不得含价格」断言）。"""
    out = []
    if isinstance(data, list):
        for r in data:
            if isinstance(r, dict) and "price" in r:
                out.append(r["price"])
    elif isinstance(data, dict):
        for v in data.values():
            out.extend(_price_values(v))
    return out


def check_null_landed(target_json, label, expect_distinct_prefix=None):
    """
    data:null 规格断言（取代旧的「sha 不变 / 拒绝落盘」）。

    旧断言编码的是「校验失败就不写盘、保持上一份」。现在规格改为落
    data:null，文件必然变化，sha 断言与规格直接互斥。但「坏数据不得进产物」
    这层保护不能一起丢掉 —— 由 d) 接管：data 段不得残留任何价格值。
    """
    path = os.path.join(DATA, target_json)
    with open(path, encoding="utf-8") as f:
        env = json.load(f)
    warns = env.get("warnings") or []
    check(f"{label}: data is None", env.get("data", "MISSING") is None,
          f"data={str(env.get('data'))[:80]}")
    check(f"{label}: coverage is None",
          env.get("coverage", "MISSING") is None,
          f"coverage={str(env.get('coverage'))[:80]}")
    check(f"{label}: warnings 非空", bool(warns), f"warnings={warns}")
    vals = _price_values(env.get("data"))
    check(f"{label}: data 不含任何价格值", not vals,
          f"残留 {len(vals)} 个价格：{vals[:3]}")
    if expect_distinct_prefix:
        hit = [w for w in warns if w.startswith(expect_distinct_prefix)]
        check(f"{label}: warnings 含判据 {expect_distinct_prefix}", bool(hit),
              f"实际={warns}")
    return warns


def run_injected(script, patch_code, expect_exit, target_json,
                 quar_prefix, label, null_expect=None):
    """
    以子进程跑采集脚本，先注入 patch_code 替换网络层。
    比对目标 JSON 的哈希与 quarantine 产物。

    null_expect 非 None 时改用 data:null 规格断言（见 check_null_landed），
    并断言 warnings 里含该判据前缀 —— 三个 gold 用例必须各自可区分。
    """
    target = os.path.join(DATA, target_json)
    before = sha(target)

    # 隔离区指向 tmpdir，避免污染 data/quarantine/
    tmpdir = tempfile.mkdtemp()
    quar_tmp = os.path.join(tmpdir, "quarantine")
    os.makedirs(quar_tmp, exist_ok=True)

    runner = f"""
import sys, json
sys.path.insert(0, {ROOT!r})
import {script} as mod
mod.QUARANTINE_DIR = {quar_tmp!r}
{patch_code}
try:
    mod.main()
except SystemExit as e:
    sys.exit(e.code if e.code is not None else 0)
sys.exit(0)
"""
    r = subprocess.run([sys.executable, "-c", runner],
                       capture_output=True, text=True, cwd=ROOT)
    after = sha(target)
    quar_files = os.listdir(quar_tmp) if os.path.isdir(quar_tmp) else []
    new_quar = [f for f in quar_files if f.startswith(quar_prefix)]

    print(f"\n--- {label} ---")
    print(f"    exit={r.returncode}（期望 {expect_exit}）")
    check(f"{label}: exit={expect_exit}", r.returncode == expect_exit,
          f"实际 {r.returncode}; stderr={r.stderr[-200:]}")
    if null_expect is None:
        check(f"{label}: {target_json} 未被覆盖", before == after,
              "文件内容已变化")
    else:
        warns = check_null_landed(target_json, label, null_expect)
        NULL_WARNS[label] = warns
        # 落了 data:null 就必须还原，否则污染下一个用例的输入
        subprocess.run(["git", "checkout", "--", f"data/{target_json}"],
                       cwd=ROOT, capture_output=True)
    if expect_exit == 1:
        check(f"{label}: 生成隔离文件", bool(new_quar), f"新增={new_quar}")

    # 清理 tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)
    return r


# ── 1. fetch_cot：全期归零 ───────────────────────────────────────────────
print("\n===== fetch_cot =====")

ZERO_ROWS = json.dumps([
    {"report_date_as_yyyy_mm_dd": f"2025-{(i % 12) + 1:02d}-15"}
    for i in range(52)
])
run_injected(
    "fetch_cot",
    f"mod.fetch_api = lambda limit=52: json.loads({ZERO_ROWS!r})",
    1, "cot.json", "cot-",
    "字段全缺 → 三项归零",
)

# 正常数据应放行（用真实 cot.json 的 weekly 反推成 API 行）
with open(os.path.join(DATA, "cot.json"), encoding="utf-8") as f:
    real_cot = unwrap(json.load(f), strict=True)
GOOD_ROWS = json.dumps([
    {"report_date_as_yyyy_mm_dd": w["date"],
     "m_money_positions_long_all": str(max(w["mf_net"], 0) + 100000),
     "m_money_positions_short_all": str(100000 - min(w["mf_net"], 0) - w["mf_net"] + max(w["mf_net"], 0)),
     "prod_merc_positions_long": "50000",
     "prod_merc_positions_short": str(50000 - w["comm_net"]),
     "swap_positions_long_all": "0",
     "swap__positions_short_all": "0",
     "open_interest_all": str(w["open_interest"])}
    for w in real_cot["weekly"]
])
run_injected(
    "fetch_cot",
    f"mod.fetch_api = lambda limit=52: json.loads({GOOD_ROWS!r})\n"
    f"mod.OUT_PATH = mod.OUT_PATH + '.injectiontest'",
    0, "cot.json", "cot-",
    "正常数据 → 放行（写到临时路径）",
)
tmp = os.path.join(DATA, "cot.json.injectiontest")
if os.path.exists(tmp):
    os.remove(tmp)


# ── 2. fetch_gold：全 null ───────────────────────────────────────────────
print("\n===== fetch_gold =====")

# fetch_stooq 现在返回 (price_map, raw_meta) 二元组。stub 必须同形，
# 且 raw_meta 五个键齐全 —— 缺键会让 quarantine 的 raw 段写出残缺内容而不报错。
STUB_META = json.dumps({
    "source": "stooq.com",
    "requested_url": "https://stooq.com/q/d/l/?s=xauusd&i=w",
    "final_url": "https://stooq.com/q/d/l/?s=xauusd&i=w",
    "status": 200,
    "body": "Date,Open,High,Low,Close\n1999-01-01,300,300,300,300\n",
})
STUB_STOOQ = (f"mod.fetch_stooq = lambda: ({{'1999-01-01': 300.0}}, "
              f"json.loads({STUB_META!r}))")

run_injected(
    "fetch_gold",
    # 返回合法但日期完全对不上的 price_map → 全部对齐失败
    STUB_STOOQ,
    1, "gold_price.json", "gold-",
    "日期完全错位 → 52 周全 null",
    null_expect="a)",
)

run_injected(
    "fetch_gold",
    # 价格取到成交量列（越界）
    STUB_STOOQ + "\n"
    "_orig = mod.align_price\n"
    "mod.align_price = lambda d, m: 999999.0",
    1, "gold_price.json", "gold-",
    "价格越界（999999）",
    null_expect="d)",
)

run_injected(
    "fetch_gold",
    # 序列退化：所有周同一价格
    STUB_STOOQ + "\n"
    "mod.align_price = lambda d, m: 4000.0",
    1, "gold_price.json", "gold-",
    "序列退化（全为 4000.0）",
    null_expect="e)",
)

# 三个用例的 warnings 必须互不相同 —— 否则「可区分」只是前缀断言的假象
_gold_labels = ["日期完全错位 → 52 周全 null", "价格越界（999999）",
                "序列退化（全为 4000.0）"]
_sets = [tuple(NULL_WARNS.get(k, [])) for k in _gold_labels]
check("三个 gold 用例 warnings 两两不同",
      len(set(_sets)) == 3 and all(_sets),
      f"实际={[list(s)[:1] for s in _sets]}")


# ── 3. fetch_stocks：明细归零 / WAF ─────────────────────────────────────
print("\n===== fetch_stocks =====")

# 经 unwrap 读：stocks.json 信封化前后都取到同一份业务数据（日频数组）。
# 直读 real_st[-1] 会在信封化后崩成 KeyError。
with open(os.path.join(DATA, "stocks.json"), encoding="utf-8") as f:
    real_st = unwrap(json.load(f), strict=True)
LATEST = real_st[-1]

# 最小仓库静默归零
zeroed = json.loads(json.dumps(LATEST))
zeroed["date"] = "2026-07-30"
small = min(zeroed["depositories"], key=lambda d: d["total"])
for d in zeroed["depositories"]:
    if d["name"] == small["name"]:
        d["registered"] = d["eligible"] = d["total"] = 0
run_injected(
    "fetch_stocks",
    f"mod.download = lambda: b'x'\n"
    f"mod.parse = lambda c: json.loads({json.dumps(zeroed)!r})",
    1, "stocks.json", "stocks-",
    f"最小仓库 {small['name']} 归零（{small['total']:,} oz）",
)

# 总量归零
allzero = json.loads(json.dumps(LATEST))
allzero.update(date="2026-07-30", registered=0, eligible=0, total=0,
               depositories=[])
run_injected(
    "fetch_stocks",
    f"mod.download = lambda: b'x'\n"
    f"mod.parse = lambda c: json.loads({json.dumps(allzero)!r})",
    1, "stocks.json", "stocks-",
    "registered 与 eligible 同时归零",
)

# WAF 封锁 → exit 2（不是 0）
run_injected(
    "fetch_stocks",
    "mod.download = lambda: None",
    2, "stocks.json", "stocks-",
    "WAF 封锁 → exit 2（原为 exit 0）",
)

# 无 depositories 字段 → d) SKIP，正常放行
# strict 模式下，注入测试前需预置信封格式文件
no_dep = json.loads(json.dumps(LATEST))
no_dep["date"] = "2026-07-30"
del no_dep["depositories"]

# 预置完整信封格式文件到临时路径
import tempfile
test_stocks_tmpdir = tempfile.mkdtemp()
test_stocks_path = os.path.join(test_stocks_tmpdir, "stocks.json.injectiontest")
with open(test_stocks_path, "w", encoding="utf-8") as f:
    from data_envelope import envelope
    json.dump(envelope(
        source="cme_gold_stocks",
        freq="daily",
        data=[LATEST]
    ), f)

run_injected(
    "fetch_stocks",
    f"mod.download = lambda: b'x'\n"
    f"mod.parse = lambda c: json.loads({json.dumps(no_dep)!r})\n"
    f"mod.OUT_PATH = {test_stocks_path!r}",
    0, "stocks.json", "stocks-",
    "无 depositories 字段 → d) 跳过，exit 0 不误杀",
)
tmp = os.path.join(DATA, "stocks.json.injectiontest")
if os.path.exists(tmp):
    os.remove(tmp)


print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
