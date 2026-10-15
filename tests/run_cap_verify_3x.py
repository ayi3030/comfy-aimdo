#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_cap_verify_3x.py -- 3-round G7 regression driver for the Arc B580 XPU build.

WHY THIS EXISTS
---------------
G7 acceptance (agreed 2026-10-15) is: run the user-side harness
``run_cap_verify.py`` (listens on PORT=8196; covers OOM / DLL-load-failure /
device-lost regression cases) on the B580 machine **3 times, all PASS**. This
driver wraps that harness so the bar is enforced mechanically instead of by
hand, and leaves a per-round + summary evidence trail for the G7 gate record.

IMPORTANT -- THIS IS NOT THE HARNESS, AND IT DOES NOT RUN HERE
-------------------------------------------------------------
* ``run_cap_verify.py`` lives on the B580 test machine, NOT in this repo. Pass
  its path with ``--harness``.
* Run this driver ON THE B580 MACHINE with the freshly built
  ``comfy-aimdo-xpu`` wheel already deployed. It will fail loudly if the
  harness is missing.
* A round is PASS iff the harness subprocess exits 0. Any non-zero exit,
  timeout, or crash is FAIL. Final exit code is 0 ONLY when every round passed.

USAGE
-----
    python run_cap_verify_3x.py
    python run_cap_verify_3x.py --harness /path/to/run_cap_verify.py --rounds 3
    python run_cap_verify_3x.py --harness run_cap_verify.py --harness-args "--port 8196"

Outputs (under tests/cap_verify_results/, timestamped):
    round_1_<ts>.log ... round_N_<ts>.log   (harness stdout/stderr, captured)
    summary_<ts>.txt                         (per-round PASS/FAIL + verdict)
"""
import argparse
import datetime
import os
import subprocess
import sys

DEFAULT_ROUNDS = 3
DEFAULT_HARNESS = "run_cap_verify.py"
DEFAULT_TIMEOUT = 1800  # 30 min hard bound per round
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cap_verify_results")


def ts_now():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def run_round(harness, harness_args, round_no, timeout, results_dir):
    cmd = [sys.executable, harness] + harness_args
    stamp = ts_now()
    log_path = os.path.join(results_dir, "round_%d_%s.log" % (round_no, stamp))
    print("\n===== ROUND %d @ %s =====" % (round_no, stamp))
    print("CMD: %s" % " ".join(cmd))
    print("LOG: %s" % log_path)
    try:
        with open(log_path, "w", encoding="utf-8", errors="replace") as lf:
            lf.write("CMD: %s\n\n" % " ".join(cmd))
            lf.flush()
            p = subprocess.run(cmd, timeout=timeout, stdout=lf, stderr=subprocess.STDOUT)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        with open(log_path, "a", encoding="utf-8", errors="replace") as lf:
            lf.write("\n[DRIVER] round timed out after %ds\n" % timeout)
        print("[FAIL] round %d TIMED OUT after %ds" % (round_no, timeout))
        return False, log_path
    except FileNotFoundError:
        print("[FAIL] harness not found: %s" % harness)
        return False, log_path
    except OSError as e:
        print("[FAIL] could not launch harness: %s" % e)
        return False, log_path

    verdict = "PASS" if rc == 0 else "FAIL"
    print("[%s] round %d (rc=%d)" % (verdict, round_no, rc))
    return (rc == 0), log_path


def main():
    ap = argparse.ArgumentParser(
        description="3-round G7 regression driver for the B580 XPU build.")
    ap.add_argument("--harness", default=DEFAULT_HARNESS,
                    help="path to run_cap_verify.py (default: %(default)s)")
    ap.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                    help="number of rounds to run (default: %(default)s)")
    ap.add_argument("--harness-args", default="",
                    help="extra args forwarded to the harness (space-separated)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help="per-round timeout in seconds (default: %(default)s)")
    args = ap.parse_args()

    if args.rounds < 1:
        print("[DRIVER] --rounds must be >= 1")
        return 2

    harness_args = args.harness_args.split() if args.harness_args.strip() else []

    os.makedirs(RESULTS_DIR, exist_ok=True)
    stamp = ts_now()
    summary_path = os.path.join(RESULTS_DIR, "summary_%s.txt" % stamp)

    print("G7 3-round driver: harness=%s rounds=%d timeout=%ds"
          % (args.harness, args.rounds, args.timeout))

    results = []
    for i in range(1, args.rounds + 1):
        ok, log_path = run_round(args.harness, harness_args, i,
                                 args.timeout, RESULTS_DIR)
        results.append((i, ok, log_path))

    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    all_pass = (passed == total)

    lines = []
    lines.append("G7 3-round verification -- %s" % stamp)
    lines.append("harness: %s" % args.harness)
    lines.append("rounds:  %d   passed: %d   failed: %d"
                 % (total, passed, total - passed))
    lines.append("verdict: %s" % ("GREEN (all PASS)" if all_pass else "RED (>=1 FAIL)"))
    lines.append("")
    for i, ok, log_path in results:
        lines.append("  round %d: %s   (%s)" % (i, "PASS" if ok else "FAIL", log_path))
    summary = "\n".join(lines)
    print("\n" + summary)
    with open(summary_path, "w", encoding="utf-8") as sf:
        sf.write(summary + "\n")

    print("SUMMARY: %s" % summary_path)
    # Exit 0 ONLY when every round passed -- this is the gate signal.
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
