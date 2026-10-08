#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁日志检查器（自写）：对一份 ComfyUI 启动日志判定 P1（免 flag 门禁）与
RAM 缓存开关行。只读日志，不启动任何东西。

判据：
  三正（必现）：published 1 SYCL queue(s) / backend ready (mode=native_hook) /
                DynamicVRAM support detected and enabled
  两负（必缺）：XPU backend not requested / No working comfy-aimdo install detected
  新增行： RAM 开 -> "Enabled XPU RAM cache"；  RAM 关 -> "XPU RAM cache disabled ... disk passthrough mode"

用法：python yan_logcheck.py <log> [--expect-on|--expect-off]
退出码：0 全部满足 | 1 不满足 | 2 入参错
"""
import argparse
import sys

POS = [
    "comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend",
    "comfy-aimdo XPU backend ready (mode=native_hook)",
    "DynamicVRAM support detected and enabled",
]
NEG = [
    "XPU backend not requested",
    "No working comfy-aimdo install detected",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--expect-on", action="store_true", help="期望 RAM 缓存启用行")
    ap.add_argument("--expect-off", action="store_true", help="期望 RAM 缓存关闭行")
    a = ap.parse_args()

    text = open(a.log, "r", encoding="utf-8", errors="replace").read()

    enabled = "Enabled XPU RAM cache" in text
    disabled = ("XPU RAM cache disabled" in text and "disk passthrough mode" in text)
    # 抓取实际预算/关闭行原文，便于指证
    lines = [ln.strip() for ln in text.splitlines()
             if ("XPU RAM cache" in ln)]

    ok = True
    print(f"log = {a.log}")
    print("== RAM cache lines ==")
    for ln in lines:
        print("   " + ln[:220])
    print(f"[{'PASS' if enabled else '----'}] enabled-line present  = {enabled}")
    print(f"[{'PASS' if disabled else '----'}] disabled-line present = {disabled}")

    if a.expect_on:
        cond = enabled and not disabled
        ok &= cond
        print(f"  [{'PASS' if cond else 'FAIL'}] expect-on (enabled and not disabled)")
    if a.expect_off:
        cond = disabled and not enabled
        ok &= cond
        print(f"  [{'PASS' if cond else 'FAIL'}] expect-off (disabled and not enabled)")

    print("== gate markers ==")
    pos_hits = {}
    for m in POS:
        hit = m in text
        pos_hits[m] = hit
        ok &= hit
        print(f"  [{'PASS' if hit else 'FAIL'}] required  {m!r}")
    neg_hits = {}
    for m in NEG:
        hit = m in text
        neg_hits[m] = hit
        ok &= (not hit)
        print(f"  [{'PASS' if not hit else 'FAIL'}] forbidden {m!r} present={hit}")

    print("GATE_PASS" if ok else "GATE_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
