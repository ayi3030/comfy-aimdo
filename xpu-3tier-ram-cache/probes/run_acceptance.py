#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""P3 acceptance runner for the comfy-aimdo XPU backend on Intel Arc B580.

Sequence:
  1. Launch ComfyUI with the EXACT gate command:
       python_embeded\\python.exe -s ComfyUI\\main.py --windows-standalone-build
           --disable-auto-launch --port 8196
     cwd = portable root. AIMDO_XPU_ENABLED is FORCIBLY REMOVED from the child
     env (any value would short-circuit condition 1 and mask the defect under
     test). --enable-dynamic-vram is NOT passed.
  2. Wait for /system_stats (fallback /).
  3. Deploy guard: if --expect-version / --expect-commit are given, compare the
     runtime version (log line "comfy-aimdo version: X") and the on-disk
     _version.py commit. Mismatch -> stop immediately, exit 6.
  4. Assert REQUIRED positive markers present and FORBIDDEN negative markers
     absent. Any failure -> exit 2 (gate fail).
  5. POST PROMPT_small.json, then PROMPT_h3.json; poll /history; verify
     artifacts ON DISK (image >100KB, video >1MB).
  6. H3 bounded fallback (retried AT MOST ONCE): only when the primary H3 run
     fails with a MODEL/GRAPH/SAMPLING-level error. If the failure is
     AIMDO-gate/backend-level, we do NOT retry (that is the defect under test).
     Fallback = PROMPT_h3_fallback.json (hybrid model + steps=20, rest equal).
  7. Always: kill the process tree, and write acc_<ts>_aimdo_lines.txt with
     every comfy-aimdo / native-callback line (offload/pressure evidence).

Exit codes: 0 all green (PRIMARY config) | 2 gate markers | 3 small fail
            4 h3 fail (incl. AIMDO-level, or fallback also failed) | 5 timeout
            6 deploy-version mismatch | 7 primary failed but fallback succeeded
            (7 = PASS-WITH-FALLBACK: the primary config was NOT validated)

stdlib only (urllib).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error

PORTABLE = r"E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable"
PYEXE = os.path.join(PORTABLE, "python_embeded", "python.exe")
MAIN_REL = os.path.join("ComfyUI", "main.py")
AIMDO_VERSION_FILE = os.path.join(PORTABLE, "python_embeded", "Lib", "site-packages",
                                  "comfy_aimdo", "_version.py")
HERE = os.path.dirname(os.path.abspath(__file__))
DEF_OUTDIR = os.path.join(HERE, "logs")

POS_MARKERS = [
    "comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend",
    "comfy-aimdo XPU backend ready (mode=native_hook)",
    "DynamicVRAM support detected and enabled",
]
NEG_MARKERS = [
    "XPU backend not requested",
    "No working comfy-aimdo install detected",
]
# Specific gate/backend-level failure signals -> classify an H3 failure as AIMDO
# (the defect under test) and therefore DO NOT fall back / retry.
AIMDO_FAIL_SIGNALS = [
    "XPU backend not requested",
    "No working comfy-aimdo install detected",
    "SYCL queue registry could not be published",
    "ABI mismatch between torch and aimdo_xpu.dll",
    "using native PyTorch XPU allocator",
]

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".avi"}
AUDIO_EXT = {".flac", ".wav", ".mp3", ".ogg"}
MIN_IMAGE_BYTES = 100 * 1024
MIN_VIDEO_BYTES = 1 * 1024 * 1024

# keywords hinting the offload path is actually doing work under pressure
PRESSURE_KW = ["evict", "offload", "pressure", "vbar", "budget", "residen",
               "oom", "out of memory", "headroom", "reclaim", "unload",
               "release", "paging", "swap"]

_ARTIFACTS = []


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def log(msg):
    print(msg, flush=True)


# --------------------------------------------------------------------------- #
# deploy guard
# --------------------------------------------------------------------------- #
def deployed_version_file():
    txt = read_text(AIMDO_VERSION_FILE)
    m = re.search(r"__version__\s*=\s*version\s*=\s*'([^']+)'", txt)
    c = re.search(r"__commit_id__\s*=\s*commit_id\s*=\s*'([^']+)'", txt)
    return (m.group(1) if m else "?"), (c.group(1) if c else "?")


def runtime_version_from_log(child_log):
    ms = re.findall(r"comfy-aimdo version:\s*([0-9A-Za-z._+-]+)", read_text(child_log))
    return ms[-1] if ms else "?"


# --------------------------------------------------------------------------- #
# launch / supervise
# --------------------------------------------------------------------------- #
def build_env():
    env = dict(os.environ)
    env.pop("AIMDO_XPU_ENABLED", None)          # hard requirement
    env["PATH"] = os.path.join(PORTABLE, "python_embeded", "Library", "bin") + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    assert "AIMDO_XPU_ENABLED" not in env, "AIMDO_XPU_ENABLED must not be in the child env"
    return env


def launch(port, child_log):
    os.makedirs(os.path.dirname(child_log), exist_ok=True)
    cmd = [PYEXE, "-s", MAIN_REL, "--windows-standalone-build",
           "--disable-auto-launch", "--port", str(port)]
    log(">> launching: " + " ".join(cmd))
    lf = open(child_log, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        cmd, cwd=PORTABLE, stdout=lf, stderr=subprocess.STDOUT,
        env=build_env(), creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    return proc, lf


def kill_tree(proc):
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True, text=True, errors="replace")
    except Exception:
        pass
    try:
        proc.wait(timeout=15)
    except Exception:
        pass


def http_get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except Exception:
        return None, None


def wait_ready(proc, port, timeout):
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    t0 = time.time()
    while time.time() < deadline:
        if proc.poll() is not None:
            log(f"!! process exited early rc={proc.returncode}")
            return False, f"exited(rc={proc.returncode})"
        for path in ("/system_stats", "/"):
            status, _ = http_get(base + path)
            if status == 200:
                log(f">> ready in {time.time() - t0:.1f}s via {path}")
                return True, "ready"
        time.sleep(3)
    return False, "startup-timeout"


# --------------------------------------------------------------------------- #
# prompt submission / polling / verification
# --------------------------------------------------------------------------- #
def post_prompt(port, graph, client_id):
    base = f"http://127.0.0.1:{port}"
    payload = json.dumps({"prompt": graph, "client_id": client_id}).encode("utf-8")
    req = urllib.request.Request(base + "/prompt", data=payload,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}"
    except Exception as e:
        return None, f"request failed: {e}"
    if "prompt_id" not in body:
        return None, f"no prompt_id in response: {body}"
    return body["prompt_id"], None


def poll_history(port, prompt_id, timeout):
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, text = http_get(f"{base}/history/{prompt_id}", timeout=10)
        if status == 200 and text:
            try:
                data = json.loads(text)
            except Exception:
                data = {}
            entry = data.get(prompt_id) if isinstance(data, dict) else None
            if entry:
                last = entry
                st = (entry.get("status") or {}).get("status_str")
                if st in ("success", "error"):
                    return entry, "done"
        time.sleep(3)
    return last, "timeout"


def collect_files(obj, acc):
    if isinstance(obj, dict):
        if isinstance(obj.get("filename"), str):
            acc.append(obj)
        for v in obj.values():
            collect_files(v, acc)
    elif isinstance(obj, list):
        for v in obj:
            collect_files(v, acc)


def resolve_disk_path(fdict):
    fname = fdict.get("filename")
    sub = fdict.get("subfolder") or ""
    typ = fdict.get("type") or "output"
    root = {"temp": os.path.join(PORTABLE, "ComfyUI", "temp"),
            "input": os.path.join(PORTABLE, "ComfyUI", "input")}.get(
        typ, os.path.join(PORTABLE, "ComfyUI", "output"))
    return os.path.join(root, sub, fname)


def classify(path):
    ext = os.path.splitext(path)[1].lower()
    return "image" if ext in IMAGE_EXT else "video" if ext in VIDEO_EXT \
        else "audio" if ext in AUDIO_EXT else "other"


def verify_artifacts(entry, need_kind, min_bytes):
    files = []
    collect_files(entry.get("outputs", {}), files)
    if not files:
        return False, ["  no file entries in history outputs"]
    lines, ok = [], False
    for fd in files:
        p = resolve_disk_path(fd)
        kind = classify(p)
        exists = os.path.isfile(p)
        size = os.path.getsize(p) if exists else -1
        good = exists and (kind != need_kind or size >= min_bytes)
        if kind == need_kind and good:
            ok = True
        lines.append(f"  [{'PASS' if good else 'FAIL'}] {kind:5s} {size:>13,}B exists={exists}  {p}")
        _ARTIFACTS.append(f"{kind}\t{size}\t{exists}\t{p}")
    return ok, lines


def preflight_object_info(port, graph_paths):
    """Diagnostics only: validate graphs against the LIVE /object_info."""
    base = f"http://127.0.0.1:{port}"
    status, text = http_get(base + "/object_info", timeout=60)
    if status != 200 or not text:
        log(f">> preflight: /object_info unavailable (status={status}); skipping")
        return
    try:
        info = json.loads(text)
    except Exception as e:
        log(f">> preflight: /object_info not JSON ({e}); skipping")
        return
    for gp in graph_paths:
        graph = json.load(open(gp, "r", encoding="utf-8"))
        log(f">> preflight {os.path.basename(gp)} vs live /object_info:")
        for nid, node in graph.items():
            ct = node.get("class_type")
            spec = info.get(ct)
            if spec is None:
                log(f"   [FAIL] node {nid}: class_type {ct!r} not in /object_info")
                continue
            known = set(spec.get("input", {}).get("required", {})) | \
                set(spec.get("input", {}).get("optional", {}))
            bad = [k for k in node.get("inputs", {}) if k not in known]
            log(f"   [{'PASS' if not bad else 'FAIL'}] node {nid}:{ct}"
                + (f"  undeclared inputs {bad}" if bad else ""))


def run_one(port, name, graph_path, timeout, client_id, outdir, ts):
    """Returns (ok: bool, why: 'ok'|'fail'|'timeout'|'submit', entry|None)."""
    graph = json.load(open(graph_path, "r", encoding="utf-8"))
    log(f"\n>> submitting {name} ({os.path.basename(graph_path)}) timeout={timeout}s")
    pid, err = post_prompt(port, graph, client_id)
    if err:
        log(f"!! {name}: submit failed: {err}")
        return False, "submit", None
    log(f">> {name}: prompt_id={pid}")
    entry, why = poll_history(port, pid, timeout)
    hist_path = os.path.join(outdir, f"acc_{ts}_{name}_hist.json")
    with open(hist_path, "w", encoding="utf-8", errors="replace") as fh:
        json.dump(entry or {"_none": True}, fh, indent=2, ensure_ascii=False)
    log(f">> {name}: history -> {hist_path}")
    if why == "timeout" or entry is None:
        log(f"!! {name}: TIMEOUT after {timeout}s (no terminal status)")
        return False, "timeout", entry
    st = (entry.get("status") or {}).get("status_str")
    if st != "success":
        log(f"!! {name}: status={st} messages={(entry.get('status') or {}).get('messages')}")
        return False, "fail", entry
    log(f">> {name}: status=success")
    need_kind, min_bytes = ("image", MIN_IMAGE_BYTES) if name == "small" else ("video", MIN_VIDEO_BYTES)
    ok, lines = verify_artifacts(entry, need_kind, min_bytes)
    log(f">> {name}: artifact check (need a {need_kind} >= {min_bytes:,}B):")
    for l in lines:
        log(l)
    return ok, ("ok" if ok else "fail"), entry


def classify_h3_failure(entry, child_log):
    """'aimdo' = gate/backend-level (do NOT retry); 'model' = model/graph/sampling."""
    txt = read_text(child_log)
    for s in AIMDO_FAIL_SIGNALS:
        if s in txt:
            return "aimdo", f"found aimdo gate/backend signal: {s!r}"
    blob = json.dumps((entry or {}).get("status", {}), ensure_ascii=False)
    for s in ("xpu_set_queues", "SYCL queue", "comfy_aimdo", "comfy-aimdo", "aimdo_xpu"):
        if re.search(re.escape(s), blob, re.I):
            return "aimdo", f"history status references {s!r}"
    return "model", "no aimdo gate/backend signal in log or history"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8196)
    ap.add_argument("--startup-timeout", type=int, default=600)
    ap.add_argument("--small-timeout", type=int, default=300)
    ap.add_argument("--h3-timeout", type=int, default=1800)
    ap.add_argument("--outdir", default=DEF_OUTDIR)
    ap.add_argument("--small-only", action="store_true", help="run only PROMPT_small.json")
    ap.add_argument("--expect-version", default=None, help="required comfy-aimdo __version__")
    ap.add_argument("--expect-commit", default=None, help="required comfy-aimdo __commit_id__")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    ts = f"{datetime.datetime.now():%Y%m%d-%H%M%S}"
    child_log = os.path.join(args.outdir, f"acc_{ts}.log")
    runner_log = os.path.join(args.outdir, f"acc_{ts}_runner.log")
    art_path = os.path.join(args.outdir, f"acc_{ts}_artifacts.txt")
    aim_path = os.path.join(args.outdir, f"acc_{ts}_aimdo_lines.txt")

    tee = open(runner_log, "w", encoding="utf-8", errors="replace")
    orig = sys.stdout

    class _Tee:
        def write(self, s):
            orig.write(s)
            tee.write(s)
            tee.flush()

        def flush(self):
            orig.flush()
            tee.flush()

    sys.stdout = _Tee()
    proc = lf = None
    rc = 0
    try:
        fver, fcommit = deployed_version_file()
        log(f"== P3 acceptance run {ts}  (port {args.port}) ==")
        log(f">> deployed _version.py: version={fver} commit={fcommit}")
        if "AIMDO_XPU_ENABLED" in os.environ:
            log("!! NOTE: AIMDO_XPU_ENABLED is set in the PARENT env; build_env() strips "
                "it from the child, but remove it from the shell for a clean run.")

        proc, lf = launch(args.port, child_log)
        ready, why = wait_ready(proc, args.port, args.startup_timeout)
        if not ready:
            log(f"!! not ready: {why}  -> exit 5")
            rc = 5
        else:
            time.sleep(2)
            run_ver = runtime_version_from_log(child_log)
            log(f">> runtime log reports: comfy-aimdo version={run_ver}")

            # ---- deploy guard (stop early on mismatch) ----
            mism = []
            if args.expect_version and run_ver != args.expect_version:
                mism.append(f"version: expected {args.expect_version!r}, runtime {run_ver!r}")
            if args.expect_commit and fcommit != args.expect_commit:
                mism.append(f"commit: expected {args.expect_commit!r}, on-disk {fcommit!r}")
            if mism:
                for m in mism:
                    log(f"!! DEPLOY MISMATCH: {m}")
                log("!! stopping without running prompts -> exit 6")
                rc = 6
            else:
                if args.expect_version or args.expect_commit:
                    log("   deploy guard: version/commit MATCH expected")

                text = read_text(child_log)
                log("\n== gate markers ==")
                gate_ok = True
                for m in POS_MARKERS:
                    hit = m in text
                    gate_ok &= hit
                    log(f"  [{'PASS' if hit else 'FAIL'}] required:  {m!r}")
                for m in NEG_MARKERS:
                    hit = m in text
                    gate_ok &= (not hit)
                    log(f"  [{'PASS' if not hit else 'FAIL'}] forbidden: {m!r} present={hit}")

                if not gate_ok:
                    log("!! GATE MARKERS FAILED -> exit 2")
                    rc = 2
                else:
                    log("   gate markers: ALL GREEN")
                    graphs = [os.path.join(HERE, "PROMPT_small.json")]
                    if not args.small_only:
                        graphs.append(os.path.join(HERE, "PROMPT_h3.json"))
                    preflight_object_info(args.port, graphs)

                    client_id = f"p3-acc-{ts}"
                    ok_small, why_small, _ = run_one(args.port, "small",
                                                     os.path.join(HERE, "PROMPT_small.json"),
                                                     args.small_timeout, client_id, args.outdir, ts)
                    if why_small == "timeout":
                        rc = 5
                    elif not ok_small:
                        log("!! SMALL MODEL FAILED -> exit 3")
                        rc = 3
                    elif args.small_only:
                        log("\n== small-only requested: DONE, all green ==")
                        rc = 0
                    else:
                        ok_h3, why_h3, ent_h3 = run_one(args.port, "h3",
                                                        os.path.join(HERE, "PROMPT_h3.json"),
                                                        args.h3_timeout, client_id, args.outdir, ts)
                        if why_h3 == "timeout":
                            log("!! H3 TIMEOUT (no retry on timeout) -> exit 5")
                            rc = 5
                        elif ok_h3:
                            log("\n== ALL GREEN ==")
                            rc = 0
                        else:
                            # bounded fallback, ONLY for model/graph/sampling failures
                            kind, reason = classify_h3_failure(ent_h3, child_log)
                            log(f"\n>> H3 failure classification: {kind.upper()}  ({reason})")
                            if kind == "aimdo":
                                log("!! H3 failed at the AIMDO GATE/BACKEND level -> "
                                    "NO fallback/retry (this is the defect under test) -> exit 4")
                                rc = 4
                            else:
                                log(">> H3 failed at model/graph/sampling level -> ONE bounded "
                                    "fallback: hybrid model + steps=20 (PROMPT_h3_fallback.json)")
                                ok_fb, why_fb, _ = run_one(args.port, "h3retry",
                                                           os.path.join(HERE, "PROMPT_h3_fallback.json"),
                                                           args.h3_timeout, client_id, args.outdir, ts)
                                if why_fb == "timeout":
                                    log("!! H3 FALLBACK TIMEOUT -> exit 5")
                                    rc = 5
                                elif ok_fb:
                                    log("\n!! WARNING: PASS-WITH-FALLBACK - H3 PRIMARY CONFIG "
                                        "FAILED, but the FALLBACK (hybrid + steps=20) PASSED. "
                                        "The PRIMARY config was NOT validated.")
                                    log("!! exit 7 = PASS-WITH-FALLBACK (do NOT read as 0). "
                                        "Both histories / artifacts retained.")
                                    rc = 7
                                else:
                                    log("!! H3 PRIMARY AND FALLBACK BOTH FAILED -> exit 4")
                                    rc = 4
    finally:
        if proc is not None:
            kill_tree(proc)
        if lf is not None:
            lf.close()

        # ---- aimdo line extraction (offload/pressure evidence) ----
        all_lines = read_text(child_log).splitlines()
        aim_lines = [ln for ln in all_lines if re.search(r"aimdo", ln, re.I)]
        with open(aim_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(f"# every 'comfy-aimdo' / native set_log_callback line from {os.path.basename(child_log)}\n")
            fh.write(f"# total lines: {len(all_lines)} ; aimdo-related: {len(aim_lines)}\n")
            fh.write("\n".join(aim_lines) + "\n")
        pressure = [ln for ln in aim_lines if any(k in ln.lower() for k in PRESSURE_KW)]
        log(f"\n== aimdo lines: {len(aim_lines)} (of {len(all_lines)}) -> {aim_path}")
        if pressure:
            log(f"   pressure/offload-keyword lines: {len(pressure)}; sample:")
            for ln in pressure[:15]:
                log("     " + ln[:200])
        else:
            log("   pressure/offload-keyword lines: NONE found "
                "(report honestly: no offload/pressure log lines observed)")

        with open(art_path, "w", encoding="utf-8", errors="replace") as fh:
            fh.write("kind\tsize\texists\tpath\n")
            fh.write("\n".join(_ARTIFACTS) + "\n")

        sys.stdout = orig
        tee.close()
        log(f"== exit {rc} ==")
        log(f"   child log : {child_log}")
        log(f"   runner log: {runner_log}")
        log(f"   aimdo lines: {aim_path}")
        log(f"   artifacts : {art_path}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
