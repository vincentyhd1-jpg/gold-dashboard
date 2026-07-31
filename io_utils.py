#!/usr/bin/env python3
"""
落盘骨架：原子写 / 去重追加 / 保留窗口 / 隔离区写入。

四源共用的**纯机械**操作。三条硬约束，违反任一条即是把源特定语义
渗进了骨架：

━━━ 硬约束 1：禁止任何 `value or 0` / `x if x else 0` 形式的兜底 ━━━

P0 修的正是这类静默归零：fetch_cot 的 `float(row.get(k) or 0)` 把字段改名
变成 0，fetch_stocks 的 `_to_float()` 把解析异常变成 0.0，一路落盘、经
cot_index 粉饰成中性 50%，页面上完全看不出异常。

骨架只搬运字节，不填默认值、不做类型转换、不把 None 变 0。
`0` 与 `null` 语义严格区分：0 是「确实为零」，null 是「不可知」。
这里出现任何兜底就等于新开一个静默归零点。

━━━ 硬约束 2：不调 sys.exit()，一次都不 ━━━

exit code 三态（0 正常 / 1 校验失败需人工介入 / 2 上游未更新）的映射由
各调用方决定 —— fetch_stocks 的 WAF 封锁是 2，fetch_gold 的两源皆败目前
是 1（已记 TODO 待分离），fetch_cot 的网络异常直接崩。这些差异是真实的。
骨架抛异常或返回值，由调用方决定那意味着哪个 exit code。

━━━ 硬约束 3：不做任何「失败 / 无新数据」的语义判断 ━━━

- quarantine 触发条件 → 各脚本的 validate_*() 自己判，判据数与内容完全
  不同（oi 4 条 / cot 5 条 / gold 5 条 / stocks 5 条），P0 已证明不可收敛。
  本模块只提供 quarantine_write() 这个「写」动作。
- 「无新数据」判定 → 周频源在日频 workflow 里每周 6 次拿到相同数据是常态，
  日频源同 date 重抓可能是源站修订。upsert_by_key 的 merge 回调必须由调用方
  传入，骨架不内置任何「内容是否相同」的比较。
- 「该不该截断 / 窗口多大」→ max_items 由调用方传，None 表示不截断。
"""

import glob
import json
import os
import tempfile

# 幂等/合并的三态返回值。由调用方的 merge 回调返回，骨架只按它分派。
#
# TAKE_NEW 必须带原因 —— 「值变了」（源站修订）与「缺失变有」（首次补全）
# 是两件不同的事，info 要如实区分，不能把补全说成修订。
KEEP_OLD = "keep_old"
TAKE_NEW = "take_new"

TMP_PREFIX = ".tmp-"


# ── 原子写 ────────────────────────────────────────────────────────────────────

def _atomic_write(path: str, write_body, *,
                  _hook_after_fsync=None) -> None:
    """
    原子替换的公共实现：临时文件 → flush → fsync → os.replace。

    os.replace 在同一文件系统上是原子的：崩在中途只会留下临时文件，
    目标文件保持完整的旧版本，绝不会出现半截内容。

    临时文件与目标同目录（不能用 /tmp —— 跨文件系统 os.replace 会失败）。

    _hook_after_fsync 仅供测试注入「fsync 后、replace 前崩」这一时刻，
    生产路径不传。
    """
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        dir=d, prefix=TMP_PREFIX + os.path.basename(path) + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            write_body(f)
            f.flush()
            os.fsync(f.fileno())
        if _hook_after_fsync is not None:
            _hook_after_fsync(tmp)
        os.replace(tmp, path)
    except BaseException:
        # 目标文件此刻仍是旧版本 —— 不删临时文件，留给 sweep_stale_tmp
        # 与人工排查（半截内容本身是诊断线索）。异常原样上抛，
        # 由调用方决定 exit code（硬约束 2）。
        raise


def atomic_write_json(path: str, payload, *, compact: bool = True) -> None:
    """
    原子写 JSON。

    compact=True 派生文件（机器读、体积敏感）；False 采集文件（人 review diff）。
    payload 原样序列化，不填默认值、不改类型（硬约束 1）。
    """
    def body(f):
        if compact:
            text = json.dumps(payload, ensure_ascii=False,
                              separators=(",", ":"))
        else:
            text = json.dumps(payload, ensure_ascii=False, indent=2)
        f.write(text.encode("utf-8"))

    _atomic_write(path, body)


def atomic_write_bytes(path: str, data: bytes) -> None:
    """原子写二进制。供 quarantine 存原始 PDF / XLS。"""
    _atomic_write(path, lambda f: f.write(data))


def read_json_or(path: str, default):
    """
    读 JSON；文件不存在或解析失败返回 default，不抛。

    default 原样返回 —— 调用方传 [] 就得 []，传 None 就得 None。
    骨架不把它「修正」成别的东西（硬约束 1）。
    """
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def sweep_stale_tmp(path: str) -> list[str]:
    """
    清理上次崩溃留下的自己的临时文件，返回被清掉的路径列表。

    只匹配本模块的命名规范 `.tmp-<basename>.*`，不碰别人的临时文件。
    """
    d = os.path.dirname(path) or "."
    pattern = os.path.join(d, TMP_PREFIX + os.path.basename(path) + ".*")
    removed = []
    for f in glob.glob(pattern):
        try:
            os.remove(f)
            removed.append(f)
        except OSError:
            pass
    return removed


# ── 去重追加 ──────────────────────────────────────────────────────────────────

def upsert_by_key(records: list[dict], entry: dict, *,
                  key: str,
                  merge=None) -> tuple[list[dict], str, str | None]:
    """
    按 key 去重追加。返回 (新列表, 动作, 原因)。

    动作取值：
      "appended"   无同键记录 → 追加
      KEEP_OLD     有同键记录且 merge 判定保留旧值 → 列表原样返回
      TAKE_NEW     有同键记录且 merge 判定取新值 → 替换

    merge(old, new) 由**调用方**提供，签名返回 (KEEP_OLD, None) 或
    (TAKE_NEW, reason)。骨架不内置任何「内容是否相同」的比较（硬约束 3）：
    stocks 要比 registered/eligible/total/depositories，oi 要比 months[]，
    两者字段完全不同。

    reason 用于让调用方如实记 info。至少要能区分：
      "revised"     值变了 → 源站修订
      "backfilled"  字段从缺失变为有 → 首次补全
    补全不是修订，info 不能掺假。

    merge 为 None 时：有同键记录一律 KEEP_OLD（最保守，不覆盖已有数据）。
    这是「不传就别猜」，不是「默认认为相同」。
    """
    idx = next((i for i, r in enumerate(records)
                if r.get(key) == entry.get(key)), None)
    if idx is None:
        return records + [entry], "appended", None

    if merge is None:
        return records, KEEP_OLD, None

    action, reason = merge(records[idx], entry)
    if action == TAKE_NEW:
        out = list(records)
        out[idx] = entry
        return out, TAKE_NEW, reason
    if action == KEEP_OLD:
        return records, KEEP_OLD, reason
    raise ValueError(
        f"merge 回调返回了未知动作 {action!r}，"
        f"只接受 io_utils.KEEP_OLD 或 io_utils.TAKE_NEW"
    )


# ── 保留窗口 ──────────────────────────────────────────────────────────────────

def apply_retention(records: list[dict], *,
                    key: str,
                    max_items: int | None) -> list[dict]:
    """
    按 key 升序排序后保留尾部 max_items 条。max_items=None 不截断。

    必须先排序再截断：按数组位置截断会在输入乱序时删错记录 ——
    实测风险是删掉日期最新的那条而留下最早的。

    窗口内出现重复 key 直接抛错，不静默保留两条同键记录 ——
    那会让下游的 date→记录映射产生歧义，且掩盖上游的去重失效。
    """
    seen = {}
    for r in records:
        k = r.get(key)
        if k in seen:
            raise ValueError(
                f"apply_retention: 发现重复 {key}={k!r} "
                f"—— 上游去重失效，不静默保留两条同键记录"
            )
        seen[k] = True

    ordered = sorted(records, key=lambda r: r.get(key))
    if max_items is None:
        return ordered
    if max_items < 0:
        raise ValueError(f"apply_retention: max_items 不能为负，得到 {max_items}")
    return ordered[-max_items:] if max_items else []


# ── 隔离区 ────────────────────────────────────────────────────────────────────

def quarantine_write(quar_dir: str, prefix: str, stamp: str, *,
                     reason: list[str],
                     payload: dict,
                     raw: bytes | None = None,
                     raw_ext: str | None = None) -> str:
    """
    按命名规范把坏数据写进隔离区，返回 JSON 路径。

    只负责「写」，**不判断该不该隔离**（硬约束 3）—— 触发条件在各脚本的
    validate_*() 里，判据各源不同。

    命名：<prefix>-<stamp>.json / <prefix>-<stamp>.<raw_ext>
    stamp 用数据自身的日期而非运行时刻：同一份坏文件反复抓到时覆盖而非堆积。

    隔离区要提交进仓库 —— CME 只提供当日文件、无历史归档，坏数据错过就
    永久拿不回来，事后排查全靠这份快照。
    """
    from data_envelope import utc_now_iso

    os.makedirs(quar_dir, exist_ok=True)
    json_path = os.path.join(quar_dir, f"{prefix}-{stamp}.json")

    raw_path = None
    if raw is not None:
        ext = raw_ext or "bin"
        raw_path = os.path.join(quar_dir, f"{prefix}-{stamp}.{ext}")
        # raw_ext="json" 会让 raw 覆盖掉 payload —— 两者同名，写完只剩一半
        # 证据，且哪一半留下取决于写入顺序。撞名必须抛，不能静默覆盖：
        # 隔离区是坏数据的唯一快照（CME 无历史归档），丢一半等于事后无法
        # 区分「API 返回就是坏的」与「parse 解析错了」。
        if os.path.abspath(raw_path) == os.path.abspath(json_path):
            raise ValueError(
                f"quarantine_write: raw_ext={raw_ext!r} 与 payload 的 .json "
                f"撞同一路径 {json_path!r} —— raw 会覆盖 payload，"
                f"隔离区只剩一半证据。改用 raw_ext='raw.json' 之类错开。"
            )

    atomic_write_json(json_path, {
        "quarantined_at": utc_now_iso(),
        "reason":         reason,
        **payload,
    }, compact=False)

    if raw_path is not None:
        atomic_write_bytes(raw_path, raw)

    return json_path
