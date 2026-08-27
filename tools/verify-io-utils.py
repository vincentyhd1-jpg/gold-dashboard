#!/usr/bin/env python3
"""
io_utils.py 六个函数的单元测试 + 破坏注入。

只喂 fixture，不碰真实采集脚本、不碰 data/ 下任何文件（全部在 tmpdir 里）。

覆盖 P1 计划的注入清单：
  A 表  原子写中途崩（fsync 后崩 / 磁盘满 / os.replace 权限失败）
  B 表  去重追加误吞合法修订
  C 表  保留窗口误删

用法：python3 tools/verify-io-utils.py
"""

import hashlib
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import io_utils as io
from io_utils import (
    atomic_write_json, atomic_write_bytes, read_json_or,
    upsert_by_key, apply_retention, quarantine_write, sweep_stale_tmp,
    KEEP_OLD, TAKE_NEW, TMP_PREFIX,
)

passed = failed = 0


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


def tmps(d, target):
    """目录下属于 target 的临时文件。"""
    return [f for f in os.listdir(d)
            if f.startswith(TMP_PREFIX + os.path.basename(target))]


# ══ atomic_write_json ══════════════════════════════════════════════════════
print("\n[atomic_write_json]")
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "x.json")

    atomic_write_json(p, {"a": 1, "b": None})
    check("首次写入成功", read_json_or(p, None) == {"a": 1, "b": None})
    check("null 原样保留（不被兜底成 0）",
          read_json_or(p, None)["b"] is None)
    check("compact 默认无空格", open(p, encoding="utf-8").read() == '{"a":1,"b":null}')

    atomic_write_json(p, {"a": 2}, compact=False)
    check("compact=False 带缩进", "\n  " in open(p, encoding="utf-8").read())

    check("写完无临时文件残留", tmps(d, p) == [])

    # 中文与深层嵌套原样
    atomic_write_json(p, {"名": "值", "n": [1, [2, {"k": None}]]})
    check("中文与嵌套 null 原样",
          read_json_or(p, None) == {"名": "值", "n": [1, [2, {"k": None}]]})

# ── A 表：fsync 后崩 → 目标哈希不变 + 临时文件残留 ──────────────────────
print("\n[A 表：原子写中途崩]")
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "x.json")
    atomic_write_json(p, {"good": 1})
    before = sha(p)

    class Boom(Exception):
        pass

    raised = None
    try:
        io._atomic_write(
            p, lambda f: f.write(b'{"bad":1}'),
            _hook_after_fsync=lambda tmp: (_ for _ in ()).throw(Boom("crash")),
        )
    except Boom as e:
        raised = e

    check("fsync 后崩：异常上抛（不静默）", raised is not None)
    check("fsync 后崩：目标哈希不变", sha(p) == before,
          f"{before} → {sha(p)}")
    check("fsync 后崩：目标内容仍是旧版", read_json_or(p, None) == {"good": 1})
    leftover = tmps(d, p)
    check("fsync 后崩：临时文件残留（供排查）", len(leftover) == 1, leftover)

    # 之后正常跑一次：sweep 清残留 + 写入成功
    swept = sweep_stale_tmp(p)
    check("sweep_stale_tmp 清掉残留", len(swept) == 1, swept)
    atomic_write_json(p, {"good": 2})
    check("清理后写入正常", read_json_or(p, None) == {"good": 2})
    check("清理后无残留", tmps(d, p) == [])

# ── A 表：磁盘满（write 抛 OSError）→ 不产生半截目标 ────────────────────
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "x.json")
    atomic_write_json(p, {"good": 1})
    before = sha(p)

    def disk_full(f):
        f.write(b'{"half')
        raise OSError(28, "No space left on device")

    raised = None
    try:
        io._atomic_write(p, disk_full)
    except OSError as e:
        raised = e

    check("磁盘满：OSError 上抛", raised is not None and raised.errno == 28)
    check("磁盘满：目标哈希不变", sha(p) == before)
    check("磁盘满：目标未变成半截 JSON",
          read_json_or(p, None) == {"good": 1})
    check("磁盘满：半截内容留在临时文件而非目标", len(tmps(d, p)) == 1)

# ── A 表：os.replace 失败（PermissionError）→ 目标不变 + 报错可读 ────────
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "x.json")
    atomic_write_json(p, {"good": 1})
    before = sha(p)

    real_replace = os.replace
    os.replace = lambda a, b: (_ for _ in ()).throw(
        PermissionError(13, "被占用"))
    raised = None
    try:
        atomic_write_json(p, {"bad": 1})
    except PermissionError as e:
        raised = e
    finally:
        os.replace = real_replace

    check("replace 失败：PermissionError 上抛", raised is not None)
    check("replace 失败：报错可读（含原因）", "被占用" in str(raised),
          str(raised))
    check("replace 失败：目标哈希不变", sha(p) == before)
    check("replace 失败：目标内容仍是旧版",
          read_json_or(p, None) == {"good": 1})


# ══ atomic_write_bytes ═════════════════════════════════════════════════════
print("\n[atomic_write_bytes]")
with tempfile.TemporaryDirectory() as d:
    p = os.path.join(d, "raw.xls")
    blob = bytes(range(256)) * 4
    atomic_write_bytes(p, blob)
    check("二进制写入字节一致", open(p, "rb").read() == blob)
    check("写完无临时文件残留", tmps(d, p) == [])

    atomic_write_bytes(p, b"")
    check("空字节可写", open(p, "rb").read() == b"")


# ══ read_json_or ═══════════════════════════════════════════════════════════
print("\n[read_json_or]")
with tempfile.TemporaryDirectory() as d:
    missing = os.path.join(d, "nope.json")
    check("文件不存在 → 返回 default", read_json_or(missing, []) == [])
    check("default 原样返回（None 就是 None）",
          read_json_or(missing, None) is None)
    check("default 原样返回（不被改成 []）",
          read_json_or(missing, {"k": 1}) == {"k": 1})

    broken = os.path.join(d, "broken.json")
    open(broken, "w", encoding="utf-8").write("{not json")
    check("解析失败 → 返回 default，不抛", read_json_or(broken, "fallback") == "fallback")

    binary = os.path.join(d, "binary.json")
    open(binary, "wb").write(b"\xff\xfe\x00\x01")
    check("非 UTF-8 → 返回 default，不抛", read_json_or(binary, []) == [])

    good = os.path.join(d, "good.json")
    atomic_write_json(good, {"v": None})
    check("正常读取，null 不被兜底", read_json_or(good, {})["v"] is None)


# ══ upsert_by_key（B 表）═══════════════════════════════════════════════════
print("\n[B 表：去重追加]")

REC = [{"date": "2026-01-01", "v": 1}, {"date": "2026-01-02", "v": 2}]

# 不同 key → 追加
out, act, why = upsert_by_key(REC, {"date": "2026-01-03", "v": 3}, key="date")
check("不同 key → 追加", act == "appended" and len(out) == 3, (act, len(out)))
check("追加不改原列表（无副作用）", len(REC) == 2)

# 调用方的 merge：逐字段比对，区分 revised / backfilled
def stocks_like_merge(old, new):
    if old == new:
        return KEEP_OLD, None
    missing_before = [k for k in new if k not in old]
    if missing_before and all(old.get(k) == new.get(k)
                              for k in old if k in new):
        return TAKE_NEW, "backfilled"
    return TAKE_NEW, "revised"

# 同 key 内容相同 → KEEP_OLD 幂等
out, act, why = upsert_by_key(
    REC, {"date": "2026-01-02", "v": 2}, key="date", merge=stocks_like_merge)
check("同 key 内容相同 → KEEP_OLD", act == KEEP_OLD, (act, why))
check("KEEP_OLD 时列表对象原样返回（可据此跳过写盘）", out is REC)

# 同 key 值变了 → TAKE_NEW revised
out, act, why = upsert_by_key(
    REC, {"date": "2026-01-02", "v": 999}, key="date", merge=stocks_like_merge)
check("同 key 值变了 → TAKE_NEW/revised",
      act == TAKE_NEW and why == "revised", (act, why))
check("TAKE_NEW 取新值", out[1]["v"] == 999)
check("TAKE_NEW 不改原列表", REC[1]["v"] == 2)

# 同 key 字段从缺失变为有 → TAKE_NEW backfilled（不是 revised）
base = [{"date": "2026-01-01", "registered": 100}]
out, act, why = upsert_by_key(
    base, {"date": "2026-01-01", "registered": 100, "depositories": [1]},
    key="date", merge=stocks_like_merge)
check("同 key 缺失变有 → TAKE_NEW/backfilled（不是 revised）",
      act == TAKE_NEW and why == "backfilled", (act, why))

# merge=None → 保守 KEEP_OLD，不覆盖
out, act, why = upsert_by_key(REC, {"date": "2026-01-02", "v": 999}, key="date")
check("merge=None → KEEP_OLD 不覆盖（不传就别猜）",
      act == KEEP_OLD and out[1]["v"] == 2, (act, out[1]["v"]))

# merge 返回未知动作 → 抛错，不静默当成某一种
def bad_merge(old, new):
    return "whatever", None

raised = None
try:
    upsert_by_key(REC, {"date": "2026-01-02", "v": 9}, key="date",
                  merge=bad_merge)
except ValueError as e:
    raised = e
check("merge 返回未知动作 → 抛 ValueError 不静默", raised is not None)

# 骨架不内置内容比较：同 key 值变了但 merge 说 KEEP_OLD → 必须听 merge
out, act, why = upsert_by_key(
    REC, {"date": "2026-01-02", "v": 777}, key="date",
    merge=lambda o, n: (KEEP_OLD, "调用方决定不取"))
check("骨架不自作判断：merge 说 KEEP_OLD 就保留旧值",
      act == KEEP_OLD and out[1]["v"] == 2)


# ══ apply_retention（C 表）════════════════════════════════════════════════
print("\n[C 表：保留窗口]")

def recs(dates):
    return [{"date": d, "v": i} for i, d in enumerate(dates)]

# 91 条窗口 90 → 删的必须是 date 最早的
many = recs([f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}" for i in range(91)])
kept = apply_retention(many, key="date", max_items=90)
check("91 条窗口 90 → 保留 90 条", len(kept) == 90)
check("91 条窗口 90 → 删掉的是 date 最早的",
      kept[0]["date"] == sorted(r["date"] for r in many)[1],
      kept[0]["date"])
check("保留结果按 date 升序",
      [r["date"] for r in kept] == sorted(r["date"] for r in kept))

# 输入乱序 → 结果与有序输入一致（这是「按数组位置截断」会出错的地方）
shuffled = [many[50], many[0], many[90], *many[1:50], *many[51:90]]
kept2 = apply_retention(shuffled, key="date", max_items=90)
check("输入乱序 → 结果与有序输入逐条一致",
      [r["date"] for r in kept2] == [r["date"] for r in kept])
check("输入乱序 → 仍保留 date 最新的那条",
      kept2[-1]["date"] == max(r["date"] for r in many))

# max_items=None → 一条不删
allkept = apply_retention(many, key="date", max_items=None)
check("max_items=None → 一条不删", len(allkept) == 91)
check("max_items=None → 仍排序", allkept[0]["date"] <= allkept[1]["date"])

# 恰好等于窗口 → 一条不删
exact = apply_retention(many[:90], key="date", max_items=90)
check("恰好 90 条窗口 90 → 一条不删", len(exact) == 90)

# 少于窗口 → 一条不删
check("少于窗口 → 一条不删",
      len(apply_retention(many[:5], key="date", max_items=90)) == 5)

# 重复 date → 抛错，不静默留两条
raised = None
try:
    apply_retention(recs(["2026-01-01", "2026-01-01"]), key="date",
                    max_items=90)
except ValueError as e:
    raised = e
check("窗口内重复 date → 抛 ValueError 不静默保留两条", raised is not None)
check("重复 date 报错含具体键值",
      raised is not None and "2026-01-01" in str(raised), str(raised))

# 空列表
check("空列表 → 空列表", apply_retention([], key="date", max_items=90) == [])

# max_items=0
check("max_items=0 → 全删", apply_retention(many, key="date", max_items=0) == [])

# 负数 → 抛错（否则 ordered[-(-1):] 会静默返回错误切片）
raised = None
try:
    apply_retention(many, key="date", max_items=-1)
except ValueError as e:
    raised = e
check("max_items 负数 → 抛 ValueError", raised is not None)


# ══ sweep_stale_tmp 的 basename 隔离 ═══════════════════════════════════════
# 四源将来共用 io_utils，可能在同一 workflow 里先后跑、同目录并存临时文件。
# 隔离错了会删掉别源正在写的临时文件 —— 那是数据损坏，不是噪音。
print("\n[sweep_stale_tmp basename 隔离]")
with tempfile.TemporaryDirectory() as d:
    stocks_p = os.path.join(d, "stocks.json")
    cot_p    = os.path.join(d, "cot.json")
    oi_p     = os.path.join(d, "oi.json")

    # 手工造出三源各自的残留（模拟上次崩溃 / 别源正在写）
    made = {}
    for tag, target in (("stocks", stocks_p), ("cot", cot_p), ("oi", oi_p)):
        made[tag] = []
        for n in ("aaa", "bbb"):
            f = os.path.join(
                d, TMP_PREFIX + os.path.basename(target) + "." + n)
            open(f, "w", encoding="utf-8").write("half")
            made[tag].append(os.path.basename(f))

    # 无关的临时文件：不属于 io_utils 命名规范，也不该被碰
    alien = os.path.join(d, ".tmp-someone-else.txt")
    open(alien, "w", encoding="utf-8").write("not mine")
    vim_swap = os.path.join(d, "stocks.json.swp")
    open(vim_swap, "w", encoding="utf-8").write("editor swap")

    before = set(os.listdir(d))
    check("造出 3 源 × 2 个残留 + 2 个无关文件",
          len(made["stocks"]) == 2 and len(before) == 8, sorted(before))

    swept = sweep_stale_tmp(stocks_p)
    after = set(os.listdir(d))
    swept_names = sorted(os.path.basename(f) for f in swept)

    check("只清 stocks 的 2 个残留",
          swept_names == sorted(made["stocks"]), swept_names)
    check("cot 的残留一个不碰",
          all(f in after for f in made["cot"]),
          [f for f in made["cot"] if f not in after])
    check("oi 的残留一个不碰",
          all(f in after for f in made["oi"]),
          [f for f in made["oi"] if f not in after])
    check("非 io_utils 命名的临时文件不碰",
          os.path.basename(alien) in after)
    check("编辑器 swap 文件不碰",
          os.path.basename(vim_swap) in after)
    check("被删的恰好只有 stocks 的两个",
          before - after == set(made["stocks"]), before - after)

    # 反向：再清 cot，只动 cot 的
    swept2 = sweep_stale_tmp(cot_p)
    after2 = set(os.listdir(d))
    check("再清 cot → 只清 cot 的 2 个",
          sorted(os.path.basename(f) for f in swept2) == sorted(made["cot"]),
          swept2)
    check("再清 cot 后 oi 的残留仍在",
          all(f in after2 for f in made["oi"]))

    # 前缀相似不该误伤：stocks.json vs stocks.json.bak
    bak_p = os.path.join(d, "stocks.json.bak")
    bak_tmp = os.path.join(
        d, TMP_PREFIX + os.path.basename(bak_p) + ".ccc")
    open(bak_tmp, "w", encoding="utf-8").write("half")
    stocks_tmp = os.path.join(
        d, TMP_PREFIX + os.path.basename(stocks_p) + ".ddd")
    open(stocks_tmp, "w", encoding="utf-8").write("half")

    swept3 = [os.path.basename(f) for f in sweep_stale_tmp(bak_p)]
    left = set(os.listdir(d))
    check("清 stocks.json.bak 不误伤 stocks.json 的残留",
          swept3 == [os.path.basename(bak_tmp)]
          and os.path.basename(stocks_tmp) in left,
          {"swept": swept3, "stocks_tmp_still_there":
           os.path.basename(stocks_tmp) in left})

    # 无残留时返回空列表，不抛。
    # oi 此刻仍有 2 个残留 → 先清空，再断言第二次调用返回 []。
    first = sweep_stale_tmp(oi_p)
    check("oi 首次清理 → 清掉它自己的 2 个",
          sorted(os.path.basename(f) for f in first) == sorted(made["oi"]),
          first)
    check("oi 已无残留 → 再清返回空列表不抛",
          sweep_stale_tmp(oi_p) == [])
    swept4 = sweep_stale_tmp(os.path.join(d, "never-existed.json"))
    check("目标文件从不存在 → 空列表不抛", swept4 == [])


# ══ quarantine_write ═══════════════════════════════════════════════════════
print("\n[quarantine_write]")
with tempfile.TemporaryDirectory() as d:
    q = os.path.join(d, "quarantine")
    path = quarantine_write(q, "stocks", "2026-07-30",
                            reason=["a) 总量归零"],
                            payload={"entry": {"date": "2026-07-30"}},
                            raw=b"\xd0\xcf raw xls", raw_ext="xls")
    check("JSON 命名规范 <prefix>-<stamp>.json",
          os.path.basename(path) == "stocks-2026-07-30.json")
    body = read_json_or(path, None)
    check("含 quarantined_at", "quarantined_at" in body)
    check("含 reason 原文", body["reason"] == ["a) 总量归零"])
    check("payload 字段平铺进去", body["entry"]["date"] == "2026-07-30")
    raw_p = os.path.join(q, "stocks-2026-07-30.xls")
    check("原始字节按 raw_ext 落盘", open(raw_p, "rb").read() == b"\xd0\xcf raw xls")

    # 同 stamp 重复隔离 → 覆盖而非堆积
    quarantine_write(q, "stocks", "2026-07-30", reason=["b) 重复"],
                     payload={"entry": {}})
    files = sorted(f for f in os.listdir(q) if f.startswith("stocks-"))
    check("同 stamp 重复 → 覆盖不堆积", files == ["stocks-2026-07-30.json",
                                                  "stocks-2026-07-30.xls"], files)
    check("覆盖后 reason 是新的",
          read_json_or(path, None)["reason"] == ["b) 重复"])

    # raw=None → 不产生原始文件
    quarantine_write(q, "cot", "2026-07-21", reason=["x"], payload={})
    check("raw=None → 只写 JSON",
          not any(f.startswith("cot-2026-07-21.") and not f.endswith(".json")
                  for f in os.listdir(q)))
    check("隔离区写完无临时文件残留",
          not any(f.startswith(TMP_PREFIX) for f in os.listdir(q)))

    # raw_ext="json" 撞名必须抛，不能静默让 raw 覆盖 payload。
    # 实测踩到过：fetch_cot 存 Socrata 的 JSON 响应，顺手传 raw_ext="json"，
    # 两者同为 cot-<stamp>.json，写完隔离区只剩 raw —— parsed_weekly 没了，
    # 事后无法区分「API 返回就是坏的」与「parse_row 解析错了」。
    try:
        quarantine_write(q, "cot", "2026-07-22", reason=["x"],
                         payload={"parsed": [1, 2]},
                         raw=b'{"api": "resp"}', raw_ext="json")
        check("raw_ext='json' 撞名 → 抛 ValueError", False, "没抛")
    except ValueError as e:
        check("raw_ext='json' 撞名 → 抛 ValueError", True)
        check("撞名报错点明后果与改法",
              "覆盖" in str(e) and "raw.json" in str(e), str(e))
    check("撞名时不留半份证据（payload 未被写出）",
          not os.path.exists(os.path.join(q, "cot-2026-07-22.json")),
          sorted(os.listdir(q)))

    # 错开扩展名 → 两份并存
    quarantine_write(q, "cot", "2026-07-23", reason=["x"],
                     payload={"parsed": [1, 2]},
                     raw=b'{"api": "resp"}', raw_ext="raw.json")
    both = sorted(f for f in os.listdir(q) if f.startswith("cot-2026-07-23"))
    check("raw_ext='raw.json' → parsed 与 raw 两份并存",
          both == ["cot-2026-07-23.json", "cot-2026-07-23.raw.json"], both)
    check("两份内容各自正确",
          read_json_or(os.path.join(q, "cot-2026-07-23.json"),
                       {})["parsed"] == [1, 2]
          and open(os.path.join(q, "cot-2026-07-23.raw.json"),
                   "rb").read() == b'{"api": "resp"}')


# ══ D 表：信封读取端（unwrap / assert_envelope）═════════════════════════════
print("\n[D 表：信封读取端]")
from data_envelope import (
    envelope, unwrap, assert_envelope, is_envelope, upstream_ref,
    SCHEMA_VERSION, VALID_FREQ,
)

good = envelope("test_src", "daily", {"rows": [1, 2]},
                dates=["2026-01-01", "2026-01-02"])

check("合格信封 → assert 通过",
      assert_envelope(good) is None)
check("unwrap 取出 data", unwrap(good) == {"rows": [1, 2]})
check("is_envelope 识别信封", is_envelope(good))
check("is_envelope 不误判裸数组", not is_envelope([1, 2]))

def rejects(payload, want_kw):
    try:
        assert_envelope(payload)
        return False, "未抛错"
    except ValueError as e:
        return (want_kw in str(e)), str(e)

# 缺 schema_version
p = dict(good); del p["schema_version"]
ok, msg = rejects(p, "缺字段")
check("缺 schema_version → 拒绝", ok, msg)

# 缺 data
p = dict(good); del p["data"]
ok, msg = rejects(p, "缺字段")
check("信封齐全但缺 data → 拒绝", ok, msg)

# schema_version 是字符串（类型也是契约）
p = dict(good); p["schema_version"] = "0"
ok, msg = rejects(p, "必须是 int")
check('schema_version="0"（字符串）→ 拒绝', ok, msg)

# schema_version 是 bool（Python 里 True == 1，必须单独排除）
p = dict(good); p["schema_version"] = True
ok, msg = rejects(p, "必须是 int")
check("schema_version=True（bool）→ 拒绝", ok, msg)

# 未来版本
p = dict(good); p["schema_version"] = 999
ok, msg = rejects(p, "未知的 schema_version")
check("schema_version=999（未来版本）→ 拒绝，不按旧格式硬解", ok, msg)

# freq 非法
p = dict(good); p["freq"] = "hourly"
ok, msg = rejects(p, "freq")
check("freq='hourly' → 拒绝", ok, msg)

# freq 合法：quarterly（债务面板用季频，Treasury Bulletin 与 BEA GDP 都是季频）。
# 上面那条 hourly 只证明「白名单外的被拒」，白名单里少一项它照样绿 ——
# 所以必须有这条正向断言，否则 quarterly 被删掉不会有任何护栏出声。
p = dict(good); p["freq"] = "quarterly"
try:
    assert_envelope(p)
    check("freq='quarterly' → 接受", True)
except ValueError as e:
    check("freq='quarterly' → 接受", False, str(e))

# 白名单本身的锚：增或删任何一项都红。放宽校验不会让别的断言变红，
# 这条是唯一会拦住「悄悄多加一种频率」的地方。
check("VALID_FREQ 恰为 {daily,weekly,monthly,quarterly,annual} 五项",
      VALID_FREQ == frozenset({"daily", "weekly", "monthly", "quarterly", "annual"}),
      str(sorted(VALID_FREQ)))

# unwrap 对坏信封也必须拒绝（不能绕过 assert）
p = dict(good); p["schema_version"] = 999
try:
    unwrap(p)
    check("unwrap 坏信封 → 拒绝", False, "未抛错")
except ValueError:
    check("unwrap 坏信封 → 拒绝（不绕过 assert）", True)

# 裸格式：默认兼容，strict 下拒绝
check("裸数组 unwrap → 原样返回（过渡期兼容）",
      unwrap([{"date": "2026-01-01"}]) == [{"date": "2026-01-01"}])
try:
    unwrap([{"date": "x"}], strict=True)
    check("strict=True 裸格式 → 拒绝", False, "未抛错")
except ValueError:
    check("strict=True 裸格式 → 拒绝", True)

check("SCHEMA_VERSION 仍为 0（格式未冻结）", SCHEMA_VERSION == 0)

print("\n[upstream_ref strict]")
with tempfile.TemporaryDirectory() as td:
    path = os.path.join(td, "upstream.json")
    check("上游文件首次不存在 → None",
          upstream_ref(path, "fallback_source") is None)

    upstream = envelope("real_source", "weekly", [{"date": "2026-01-01"}],
                        dates=["2026-01-01"])
    atomic_write_json(path, upstream, compact=False)
    ref = upstream_ref(path, "fallback_source")
    check("合法上游信封摘取 source/generated_at/coverage/envelope",
          ref == {"source": upstream["source"],
                  "generated_at": upstream["generated_at"],
                  "coverage": upstream["coverage"],
                  "envelope": True}, ref)

    for label, bare in (("裸数组", [{"date": "2026-01-01"}]),
                        ("裸字典", {"weekly": [{"date": "2026-01-01"}]})):
        atomic_write_json(path, bare, compact=False)
        try:
            upstream_ref(path, "fallback_source")
            check(f"upstream_ref 拒绝{label}", False, "未抛错")
        except ValueError as e:
            check(f"upstream_ref 拒绝{label}", True, str(e))

    future = dict(upstream)
    future["schema_version"] = 999
    atomic_write_json(path, future, compact=False)
    try:
        upstream_ref(path, "fallback_source")
        check("upstream_ref 拒绝未知 schema_version", False, "未抛错")
    except ValueError as e:
        check("upstream_ref 拒绝未知 schema_version",
              "未知的 schema_version" in str(e), str(e))

    missing_key = dict(upstream)
    del missing_key["coverage"]
    atomic_write_json(path, missing_key, compact=False)
    try:
        upstream_ref(path, "fallback_source")
        check("upstream_ref 拒绝缺 required key", False, "未抛错")
    except ValueError as e:
        check("upstream_ref 拒绝缺 required key",
              "信封缺字段" in str(e), str(e))

print("\n[当前生产数据 strict]")
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
data_dir = os.path.join(repo_root, "data")
production_json = [
    os.path.join(data_dir, name)
    for name in os.listdir(data_dir)
    if name.endswith(".json") and os.path.isfile(os.path.join(data_dir, name))
] + [
    os.path.join(data_dir, "derived", name)
    for name in os.listdir(os.path.join(data_dir, "derived"))
    if name.endswith(".json")
]
strict_errors = []
for path in production_json:
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        unwrap(payload, strict=True)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        strict_errors.append(f"{os.path.relpath(path, repo_root)}: {exc}")
check("当前受跟踪生产 JSON 全部通过 schema v0 strict reader",
      bool(production_json) and not strict_errors, strict_errors)


# ══ 硬约束自检 ═════════════════════════════════════════════════════════════
print("\n[硬约束]")
src = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "io_utils.py"), encoding="utf-8").read()
code_lines = [l for l in src.splitlines()
              if not l.strip().startswith("#")]
code = "\n".join(code_lines)
# 去掉 docstring 段（硬约束说明里会提到这些字样）
import re
code_nodoc = re.sub(r'"""[\s\S]*?"""', "", code)

check("硬约束 1：无 `or 0` 兜底", " or 0" not in code_nodoc,
      [l for l in code_nodoc.splitlines() if " or 0" in l])
check("硬约束 2：不调 sys.exit", "sys.exit" not in code_nodoc)
check("硬约束 2：未 import sys", "import sys" not in code_nodoc)
check("硬约束 3：无 validate/should_ 之类语义判断",
      not re.search(r"def (validate|should_|is_stale|is_dup)", code_nodoc))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
