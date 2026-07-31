#!/usr/bin/env python3
"""
破坏注入：把 roll_noise 改错值 / 退回 round(4)，derive --test 必须变红。

roll_noise 的护栏在 derive --test（前端不读这个字段，无 verify 覆盖）。
这里注入两类破坏：
  1. 值算错     → 全精度断言应命中
  2. 退回 round(4) → 反舍入断言应命中（这是本次改动要防的回归）

用法：python3 tools/verify-noise-injection.py
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(ROOT, "derive_term_structure.py")
BACKUP = TARGET + ".noise-injection-backup"

CASES = [
    ("退回 round(abs(fv) / total, 4)",
     "    return abs(fv) / total",
     "    return round(abs(fv) / total, 4)"),
    ("roll_noise_ma 退回 round(..., 4)",
     "        f[\"roll_noise_ma\"] = sum(w) / len(w) if w else None",
     "        f[\"roll_noise_ma\"] = round(sum(w) / len(w), 4) if w else None"),
    ("roll_noise 值算错（乘 2）",
     "    return abs(fv) / total",
     "    return abs(fv) / total * 2"),
]


def run():
    r = subprocess.run(
        ["wsl", "-d", "Ubuntu-22.04", "--", "bash", "-c",
         "cd /mnt/d/VScode/test/gold-dashboard && "
         "python3 derive_term_structure.py --test 2>&1"],
        capture_output=True, text=True,
    )
    return r


def show(label, r):
    red = r.returncode != 0
    print(f"=== {label} ===")
    print(f"  exit={r.returncode}  {'红' if red else '绿'}")
    for line in r.stdout.splitlines():
        if "NOISE" in line or "FAILURES" in line or "All tests" in line:
            print("   ", line.strip()[:130])
    return red


shutil.copyfile(TARGET, BACKUP)
try:
    show("注入前（基线）", run())

    for label, old, new in CASES:
        with open(BACKUP, encoding="utf-8") as f:
            src = f.read()
        assert old in src, f"锚点未找到: {old!r}"
        with open(TARGET, "w", encoding="utf-8") as f:
            f.write(src.replace(old, new, 1))
        print()
        show(f"注入：{label}", run())

    shutil.copyfile(BACKUP, TARGET)
    print()
    show("改回后", run())
finally:
    shutil.copyfile(BACKUP, TARGET)
    os.remove(BACKUP)
