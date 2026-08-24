#!/usr/bin/env python3
"""
破坏注入：把 roll_noise 改错值 / 退回 round(4)，derive --test 必须变红。

项目约定的 Windows + WSL 用法：
  wsl -d Ubuntu-22.04 --cd /mnt/d/VScode/test/gold-dashboard -- \
    python3 -B tools/verify-noise-injection.py

脚本已在 WSL Python 中，因此子测试直接使用 sys.executable；不再反向调用
Windows 的 wsl.exe，也不经过 bash / PowerShell，returncode 就是目标 Python
进程的真实退出码。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "derive_term_structure.py"

CASES = [
    (
        "退回 round(abs(fv) / total, 4)",
        "    return abs(fv) / total",
        "    return round(abs(fv) / total, 4)",
        "NOISE fixture: roll_noise 应为",
    ),
    (
        "roll_noise_ma 退回 round(..., 4)",
        '        f["roll_noise_ma"] = sum(w) / len(w) if w else None',
        '        f["roll_noise_ma"] = round(sum(w) / len(w), 4) if w else None',
        "NOISE fixture: roll_noise_ma 应为",
    ),
    (
        "roll_noise 值算错（乘 2）",
        "    return abs(fv) / total",
        "    return abs(fv) / total * 2",
        "NOISE fixture: roll_noise 应为",
    ),
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_target() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(TARGET), "--test"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )


def show(label: str, result: subprocess.CompletedProcess[str]) -> None:
    red = result.returncode != 0
    print(f"=== {label} ===")
    print(f"  exit={result.returncode}  {'红' if red else '绿'}")
    for line in result.stdout.splitlines():
        if "NOISE" in line or "FAILURES" in line or "passed," in line:
            print("   ", line.strip()[:160])


def restore(backup: Path, original_hash: str) -> None:
    shutil.copyfile(backup, TARGET)
    restored_hash = digest(TARGET.read_bytes())
    if restored_hash != original_hash:
        raise RuntimeError(
            f"恢复失败：derive_term_structure.py hash={restored_hash}，"
            f"预期 {original_hash}"
        )


def main() -> int:
    original = TARGET.read_bytes()
    original_hash = digest(original)
    source = original.decode("utf-8")
    failures: list[str] = []

    with tempfile.TemporaryDirectory(prefix="gold-dashboard-noise-") as tmpdir:
        backup = Path(tmpdir) / "derive_term_structure.py"
        backup.write_bytes(original)

        try:
            baseline = run_target()
            show("注入前（基线）", baseline)
            if baseline.returncode != 0:
                failures.append(
                    f"基线应为 exit 0，实际为 {baseline.returncode}；未执行注入"
                )
            else:
                for label, old, new, expected_failure in CASES:
                    if source.count(old) != 1:
                        failures.append(
                            f"{label}: 注入锚点应恰好命中 1 次，实际 {source.count(old)} 次"
                        )
                        continue

                    injected = source.replace(old, new, 1).encode("utf-8")
                    TARGET.write_bytes(injected)
                    if digest(TARGET.read_bytes()) == original_hash:
                        failures.append(f"{label}: 注入未落地，文件 hash 未变化")
                        continue

                    result = run_target()
                    print()
                    show(f"注入：{label}", result)
                    if result.returncode == 0:
                        failures.append(f"{label}: 目标测试仍为 exit 0，护栏假绿")
                    if expected_failure not in result.stdout:
                        failures.append(
                            f"{label}: 未命中预期断言 {expected_failure!r}"
                        )

                    restore(backup, original_hash)

            restore(backup, original_hash)
            print()
            restored = run_target()
            show("恢复后", restored)
            if restored.returncode != 0:
                failures.append(f"恢复后应为 exit 0，实际为 {restored.returncode}")
        finally:
            restore(backup, original_hash)

    print(f"\n恢复校验：derive_term_structure.py sha256={original_hash}（一致）")
    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(" ", failure)
        print(f"\n{len(failures)} failed")
        return 1

    print(f"\n{len(CASES)} injections detected, baseline/restored passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
