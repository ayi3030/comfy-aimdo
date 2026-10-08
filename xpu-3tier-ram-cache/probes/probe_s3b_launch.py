#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S3b 探针（R1 计数收敛实证）—— 非侵入式启动包装器。

作用：以与 run_acceptance.py **完全相同**的方式拉起 ComfyUI，并在**不修改任何
      ComfyUI 源码文件**的前提下，用后台线程周期性采样：
        - comfy.model_management.TOTAL_PIN_CACHE_MEMORY   （XPU“已缓存未注册”字节）
        - comfy.model_management.TOTAL_PINNED_MEMORY      （已注册字节；XPU 上应为 0）
        - sum(loaded_ram_size()) over current_loaded_models （RAM 层真实占用）
        - MAX_PINNED_MEMORY                                （预算，用于判定“升到顶”）
      写入 CSV，供 probe_s3b_analyze.py 判定 R1 是否收敛（升到预算顶后出现回落）。

判定含义：若 TOTAL_PIN_CACHE_MEMORY 只增不减、到顶后 ensure_pin_registerable 恒 False，
          则 RAM 层重新变死代码（R1 失败）。本探针提供“回落”这一直接证据。

用法（在 B580 XPU 机器上，cwd 任意）：
    python_embeded\\python.exe <此文件> --out logs\\s3b.csv -- \\
        --windows-standalone-build --disable-auto-launch --port 8196
    （“--” 之后全部透传给 ComfyUI/main.py；环境变量请自行控制，例如
      默认跑“RAM 缓存开”，回滚对照跑“AIMDO_XPU_RAM_CACHE_GB=0”）

约束：只读取模块属性，不 monkeypatch、不写 ComfyUI 目录。
注意：本文件为“预备工装”，由 runtime-verifier 在真机执行；reviewer 不代为运行。
"""
import argparse
import os
import runpy
import sys
import threading
import time

PORTABLE = r"E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable"
COMFY = os.path.join(PORTABLE, "ComfyUI")
MAIN = os.path.join(COMFY, "main.py")
HERE = os.path.dirname(os.path.abspath(__file__))

CSV_HEADER = ("t_epoch,total_pin_cache_mb,total_pinned_mb,loaded_ram_mb,"
              "max_pinned_mb,n_models\n")


def _mb(x):
    try:
        return round(float(x) / (1024.0 * 1024.0), 3)
    except Exception:
        return ""


def _sample_once(mm):
    cache = getattr(mm, "TOTAL_PIN_CACHE_MEMORY", 0)
    reg = getattr(mm, "TOTAL_PINNED_MEMORY", 0)
    budget = getattr(mm, "MAX_PINNED_MEMORY", -1)
    ram = 0
    n = 0
    for lm in list(getattr(mm, "current_loaded_models", []) or []):
        model = getattr(lm, "model", None)
        try:
            is_dyn = bool(model is not None and model.is_dynamic())
        except Exception:
            is_dyn = False
        if not is_dyn:
            continue
        try:
            ram += int(lm.loaded_ram_size())
            n += 1
        except Exception:
            pass
    return _mb(cache), _mb(reg), _mb(ram), _mb(budget) if budget and budget > 0 else "", n


def _sampler(out_path, interval, stop):
    mm = None
    while not stop.is_set():
        if mm is None:
            mm = sys.modules.get("comfy.model_management")
            if mm is None:
                time.sleep(0.25)
                continue
        try:
            cache, reg, ram, budget, n = _sample_once(mm)
            with open(out_path, "a", encoding="ascii", newline="") as fh:
                fh.write(f"{time.time():.3f},{cache},{reg},{ram},{budget},{n}\n")
                fh.flush()
        except Exception:
            pass
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--out", default=os.path.join(HERE, "logs", "s3b_pin_cache.csv"))
    ap.add_argument("--interval", type=float, default=2.0, help="采样间隔秒")
    opts, passthrough = ap.parse_known_args()

    if not os.path.isfile(MAIN):
        sys.stderr.write(f"!! main.py not found: {MAIN}\n")
        return 2

    os.makedirs(os.path.dirname(os.path.abspath(opts.out)), exist_ok=True)
    with open(opts.out, "w", encoding="ascii", newline="") as fh:
        fh.write(CSV_HEADER)

    # 与 run_acceptance.py 一致的启动前提
    if "AIMDO_XPU_ENABLED" in os.environ:
        sys.stderr.write("!! NOTE: AIMDO_XPU_ENABLED is set; the gate test wants it UNSET.\n")
    os.chdir(PORTABLE)                 # DITTO run_acceptance.py
    sys.path.insert(0, COMFY)          # 让 `import comfy.*` 可解析
    sys.argv = [MAIN] + [a for a in passthrough if a != "--"]

    stop = threading.Event()
    threading.Thread(target=_sampler, args=(opts.out, opts.interval, stop), daemon=True).start()
    sys.stderr.write(f"== S3b sampler started -> {opts.out} (interval {opts.interval}s) ==\n")
    try:
        runpy.run_path(MAIN, run_name="__main__")
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
