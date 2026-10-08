#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""回滚探针：校验 `AIMDO_XPU_RAM_CACHE_GB=0` 是否退回“磁盘直通”。

输入一份**带 env=0 启动**的 ComfyUI 日志（例如 run_acceptance.py 的 acc_*.log），断言：
  - 出现  "XPU RAM cache disabled" + "disk passthrough mode"     （关闭行）
  - 不出现 "Enabled XPU RAM cache"                               （未启用）
  - 免 flag 门禁三正标记仍在、两负标记缺席                        （门禁未退化）
并打印判定。**不做任何“默认通过”**：缺证据即 FAIL。

用法：
    python probe_rollback_check.py <acc_log.txt>
退出码：0 PASS | 1 FAIL | 2 入参错
约束：只读日志；本文件为“预备工装”，reviewer 不代为运行。
"""
import re
import sys

POS_MARKERS = [
    "comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend",
    "comfy-aimdo XPU backend ready (mode=native_hook)",
    "DynamicVRAM support detected and enabled",
]
NEG_MARKERS = [
    "XPU backend not requested",
    "No working comfy-aimdo install detected",
]


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    text = open(sys.argv[1], "r", encoding="utf-8", errors="replace").read()

    disabled = bool(re.search(r"XPU RAM cache disabled.*disk passthrough", text, re.S))
    enabled = "Enabled XPU RAM cache" in text
    gate_pos = {m: (m in text) for m in POS_MARKERS}
    gate_neg = {m: (m in text) for m in NEG_MARKERS}

    print(f"disabled-line present : {disabled}")
    print(f"enabled-line  present : {enabled}  (must be False)")
    for m, hit in gate_pos.items():
        print(f"[{'PASS' if hit else 'FAIL'}] required  {m!r}")
    for m, hit in gate_neg.items():
        print(f"[{'PASS' if not hit else 'FAIL'}] forbidden {m!r} present={hit}")

    ok = (disabled and not enabled
          and all(gate_pos.values()) and not any(gate_neg.values()))
    print("ROLLBACK_OK" if ok else "ROLLBACK_FAIL")
    if not disabled:
        print("  -> 'XPU RAM cache disabled ... disk passthrough mode' line not found: env not honored.")
    if enabled:
        print("  -> 'Enabled XPU RAM cache' still logged: budget not disabled.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
