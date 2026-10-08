#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S3b 运行器（自写，替代 probe_s3b_launch.py）。

在**同一进程内**以 run_acceptance.py 完全相同的硬约束拉起 ComfyUI（runpy），
并开启后台线程同时做两件事：
  1) 采样（只读，绝不 monkeypatch、绝不改被测源码）：
       comfy.model_management.TOTAL_PIN_CACHE_MEMORY   （XPU 已缓存未注册字节）
       comfy.model_management.TOTAL_PINNED_MEMORY      （已注册字节；XPU 应恒 0）
       comfy.model_management.MAX_PINNED_MEMORY        （RAM 缓存预算）
       sum(model.loaded_ram_size() for dynamic loaded models)  （RAM 层真实占用）
     写 CSV。
  2) 驱动：等 HTTP 就绪 -> 提交 PROMPT_small(.json) [+ PROMPT_h3(.json)] -> 轮询
     /history -> 校验产物体积 -> 落 result JSON -> 结束进程。

为什么必须进程内：采样要读的是 ComfyUI 进程内的模块全局量，外部进程读不到。
若只采样不提交 prompt（原 probe_s3b_launch.py 的做法），负载永远不会到顶，
且仓库内没有“只提交 prompt”的脚本可配套 —— 故此处自带提交器。

启动硬约束：cwd=portable 根；sys.argv 不含 --enable-dynamic-vram；AIMDO_XPU_ENABLED
被剥离。AIMDO_XPU_RAM_CACHE_GB 由调用方在环境里控制（默认不设 = 默认预算；
=0 回滚；=N 收紧预算以触顶）。

用法：
  python.exe -s -u yan_s3b_run.py --port 8196 --out-csv logs/s3b_on.csv \
      --out-json logs/s3b_on_result.json [--small-only] -- \
      --windows-standalone-build --disable-auto-launch --port 8196
"""
import argparse
import datetime
import json
import os
import sys
import threading
import time
import runpy
import urllib.request

PORTABLE = r"E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable"
COMFY = os.path.join(PORTABLE, "ComfyUI")
MAIN = os.path.join(COMFY, "main.py")
HERE = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(HERE, "logs")

sys.path.insert(0, HERE)
import run_acceptance as acc  # 复用其 post_prompt / poll_history / verify_artifacts

CSV_HEADER = ("t_epoch,total_pin_cache_mb,total_pinned_mb,loaded_ram_mb,"
              "max_pinned_mb,n_dyn_models\n")


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
        try:
            model = lm.model            # ModelPatcher (weakref-backed property)
        except Exception:
            model = None
        if model is None:
            continue
        try:
            if not model.is_dynamic():
                continue
        except Exception:
            continue
        try:
            ram += int(model.loaded_ram_size())   # 方法在 ModelPatcher 上
            n += 1
        except Exception:
            pass
    return (_mb(cache), _mb(reg), _mb(ram),
            _mb(budget) if budget and budget > 0 else "", n)


def _sampler(out_path, interval, stop):
    mm = None
    warned = 0
    while not stop.is_set():
        if mm is None:
            mm = sys.modules.get("comfy.model_management")
            if mm is None:
                time.sleep(0.2)
                continue
        try:
            cache, reg, ram, budget, n = _sample_once(mm)
            with open(out_path, "a", encoding="ascii", newline="") as fh:
                fh.write(f"{time.time():.3f},{cache},{reg},{ram},{budget},{n}\n")
                fh.flush()
        except Exception as e:
            # 绝不静默吞采样错误：前 5 次写到 stderr（进 run 日志）便于诊断。
            if warned < 5:
                warned += 1
                sys.stderr.write(f"!! S3b sampler error ({warned}/5): {e!r} "
                                 f"path={out_path}\n")
                sys.stderr.flush()
        time.sleep(interval)


def _wait_ready(port, timeout):
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    t0 = time.time()
    while time.time() < deadline:
        for path in ("/system_stats", "/"):
            st, _ = acc.http_get(base + path)
            if st == 200:
                sys.stderr.write(f"== S3b driver: server ready in {time.time()-t0:.1f}s via {path} ==\n")
                return True
        time.sleep(3)
    return False


def _submit_one(port, name, graph_path, timeout, client_id, outdir, ts):
    graph = json.load(open(graph_path, "r", encoding="utf-8"))
    sys.stderr.write(f"== S3b driver: submitting {name} ({os.path.basename(graph_path)}) ==\n")
    rec = {"name": name, "graph": os.path.basename(graph_path), "ok": False,
           "why": None, "artifacts": []}
    pid, err = acc.post_prompt(port, graph, client_id)
    if err:
        rec["why"] = f"submit-failed: {err}"
        sys.stderr.write(f"!! {name}: {rec['why']}\n")
        return rec
    rec["prompt_id"] = pid
    sys.stderr.write(f"== {name}: prompt_id={pid} ==\n")
    entry, why = acc.poll_history(port, pid, timeout)
    hist_path = os.path.join(outdir, f"s3b_{ts}_{name}_hist.json")
    with open(hist_path, "w", encoding="utf-8", errors="replace") as fh:
        json.dump(entry or {"_none": True}, fh, indent=2, ensure_ascii=False)
    rec["history"] = hist_path
    if why == "timeout" or entry is None:
        rec["why"] = "timeout"
        return rec
    st = (entry.get("status") or {}).get("status_str")
    rec["status"] = st
    if st != "success":
        rec["why"] = f"status={st}"
        return rec
    need_kind, min_bytes = (("image", acc.MIN_IMAGE_BYTES) if name == "small"
                            else ("video", acc.MIN_VIDEO_BYTES))
    # 收集产物文件（复用 run_acceptance 的解析）
    files = []
    acc.collect_files(entry.get("outputs", {}), files)
    for fd in files:
        p = acc.resolve_disk_path(fd)
        exists = os.path.isfile(p)
        size = os.path.getsize(p) if exists else -1
        kind = acc.classify(p)
        rec["artifacts"].append({"kind": kind, "path": p, "exists": exists, "size": size})
    prim = [a for a in rec["artifacts"] if a["kind"] == need_kind]
    rec["ok"] = any(a["exists"] and a["size"] >= min_bytes for a in prim)
    if not rec["ok"]:
        rec["why"] = f"no {need_kind} >= {min_bytes}B"
    return rec


def _driver(port, jobs, out_json, stop, hard_deadline):
    result = {"port": port, "jobs": [], "ok": True, "started": time.time()}
    try:
        if not _wait_ready(port, 600):
            result["ok"] = False
            result["error"] = "startup-timeout"
            return
        client_id = f"s3b-{int(time.time())}"
        for name, graph_path, timeout in jobs:
            if time.time() > hard_deadline:
                result["jobs"].append({"name": name, "ok": False, "why": "hard-deadline"})
                result["ok"] = False
                break
            rec = _submit_one(port, name, graph_path, timeout, client_id, LOGS,
                              datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
            result["jobs"].append(rec)
            if not rec.get("ok"):
                result["ok"] = False
    except Exception as e:
        result["ok"] = False
        result["error"] = repr(e)
    finally:
        result["finished"] = time.time()
        try:
            with open(out_json, "w", encoding="utf-8") as fh:
                json.dump(result, fh, indent=2, ensure_ascii=False)
        except Exception:
            pass
        # 让 stdout/stderr 与 CSV 落盘后终止进程（runpy 的服务器在 main 线程里阻塞，
        # 只能由 driver 线程强制结束进程）。
        stop.set()
        time.sleep(2.0)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:
            pass
        os._exit(0 if result.get("ok") else 0)  # 结论由 result JSON 判定，退出码固定 0


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--port", type=int, default=8196)
    ap.add_argument("--out-csv", default=os.path.join(LOGS, "s3b_pin_cache.csv"))
    ap.add_argument("--out-json", default=os.path.join(LOGS, "s3b_result.json"))
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--small-timeout", type=int, default=300)
    ap.add_argument("--h3-timeout", type=int, default=1800)
    ap.add_argument("--small-only", action="store_true")
    ap.add_argument("--hard-timeout", type=int, default=2700,
                    help="driver 总硬超时秒（超时即判失败并结束）")
    opts, passthrough = ap.parse_known_args()

    if not os.path.isfile(MAIN):
        sys.stderr.write(f"!! main.py not found: {MAIN}\n")
        return 2

    # 启动硬约束
    if "AIMDO_XPU_ENABLED" in os.environ:
        sys.stderr.write("!! stripping AIMDO_XPU_ENABLED from child env (must be unset)\n")
        os.environ.pop("AIMDO_XPU_ENABLED", None)
    assert "AIMDO_XPU_ENABLED" not in os.environ
    sys.stderr.write(f"== S3b env: AIMDO_XPU_RAM_CACHE_GB="
                     f"{os.environ.get('AIMDO_XPU_RAM_CACHE_GB', '<unset>')} ==\n")

    os.makedirs(LOGS, exist_ok=True)
    # 关键：main() 稍后会 os.chdir(PORTABLE)；采样/结果路径必须先转绝对路径，
    # 否则相对路径会落到 ComfyUI 根下（目录不存在 -> 异常被吞 -> CSV 只剩表头）。
    opts.out_csv = os.path.abspath(opts.out_csv)
    opts.out_json = os.path.abspath(opts.out_json)
    with open(opts.out_csv, "w", encoding="ascii", newline="") as fh:
        fh.write(CSV_HEADER)

    jobs = [("small", os.path.join(HERE, "PROMPT_small.json"), opts.small_timeout)]
    if not opts.small_only:
        jobs.append(("h3", os.path.join(HERE, "PROMPT_h3.json"), opts.h3_timeout))

    os.chdir(PORTABLE)
    sys.path.insert(0, COMFY)
    passthrough = [a for a in passthrough if a != "--"]
    if "--enable-dynamic-vram" in passthrough:
        sys.stderr.write("!! refusing: --enable-dynamic-vram must NOT be passed\n")
        return 2
    sys.argv = [MAIN] + passthrough
    sys.stderr.write("== S3b will run: " + " ".join(sys.argv) + " ==\n")

    stop = threading.Event()
    threading.Thread(target=_sampler, args=(opts.out_csv, opts.interval, stop),
                     daemon=True).start()
    threading.Thread(target=_driver,
                     args=(opts.port, jobs, opts.out_json, stop,
                           time.time() + opts.hard_timeout),
                     daemon=True).start()
    sys.stderr.write(f"== S3b sampler -> {opts.out_csv} (interval {opts.interval}s) ==\n")
    try:
        runpy.run_path(MAIN, run_name="__main__")
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
