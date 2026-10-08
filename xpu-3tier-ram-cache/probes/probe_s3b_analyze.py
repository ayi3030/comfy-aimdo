#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S3b 分析器：读取 probe_s3b_launch.py 产出的 CSV，判定 R1（计数收敛）。

判定逻辑（不默认通过，逐档给出明确结论）：
  - 无有效样本                         -> INCONCLUSIVE（探针未采到）
  - max_cache == 0                     -> FAIL（RAM 层根本没有占用，R1 前提不成立）
  - max_cache > 0 且存在“显著回落”      -> PASS（预算驱逐生效 => 计数收敛，R1 成立）
  - max_cache > 0 且单调不减            -> WARN（两种可能，需进一步证伪：
         (a) 负载未触及预算； (b) 命中 R1 失败：free_pins 未能扣账。
         => 需核对 max_cache 是否贴近“预算”；若贴近且仍不回落，判 FAIL。）

用法：
    python probe_s3b_analyze.py <csv> [--drop-mb 64] [--plateau-frac 0.5]
退出码：0 PASS | 1 FAIL | 3 WARN | 4 INCONCLUSIVE
"""
import argparse
import csv
import sys


def load(path):
    rows = []
    with open(path, "r", encoding="ascii", errors="replace") as fh:
        rdr = csv.DictReader(fh)
        for r in rdr:
            def f(k):
                v = (r.get(k) or "").strip()
                return float(v) if v not in ("", "None") else None
            rows.append({
                "t": f("t_epoch"),
                "cache": f("total_pin_cache_mb") or 0.0,
                "reg": f("total_pinned_mb") or 0.0,
                "ram": f("loaded_ram_mb") or 0.0,
                "budget": f("max_pinned_mb"),
                "n": f("n_models") or 0.0,
            })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--drop-mb", type=float, default=64.0,
                    help="判定“回落”的最小下降幅度(MiB)")
    ap.add_argument("--plateau-frac", type=float, default=0.5,
                    help="max_cache / budget 达到该比例即认为“升到顶”")
    a = ap.parse_args()

    rows = load(a.csv)
    if not rows:
        print("INCONCLUSIVE: no samples")
        return 4

    caches = [r["cache"] for r in rows]
    max_cache = max(caches)
    argmax = caches.index(max_cache)
    tail_min = min(caches[argmax:]) if argmax < len(caches) else max_cache
    drop = max_cache - tail_min

    budgets = [r["budget"] for r in rows if r["budget"] is not None]
    budget = budgets[-1] if budgets else None
    plateau = (budget is not None and budget > 0 and max_cache >= a.plateau_frac * budget)

    print(f"samples={len(rows)}  max_cache={max_cache:.1f}MiB@#{argmax}  "
          f"tail_min={tail_min:.1f}MiB  drop={drop:.1f}MiB  budget={budget}")
    print(f"cache monotonic non-decreasing: {drop <= 1e-6}  "
          f"plateau_reached(>= {a.plateau_frac}*budget): {plateau}")

    if max_cache <= 0.0:
        print("FAIL: TOTAL_PIN_CACHE_MEMORY never became > 0 -> RAM layer never populated.")
        print("      -> check 'Enabled XPU RAM cache' log line, DISABLE_PINNED_MEMORY, is_intel_xpu().")
        return 1
    if drop >= a.drop_mb:
        print("PASS: observed a significant cache drop after peak -> eviction decrements the budget.")
        print("      (R1 converges: free_pins -> partially_unload_ram -> TOTAL_PIN_CACHE_MEMORY)")
        return 0
    # no drop
    if plateau:
        print("FAIL: cache rose to ~budget but NEVER dropped -> eviction not observed at the cap.")
        print("      Suspect R1: free_pins did not decrement. Check R5 subsets / models_for_pin_eviction.")
        return 1
    print("WARN: cache>0, monotonic, and did NOT reach budget -> workload may not have exceeded budget.")
    print("      Re-run with a larger model / longer run, or lower AIMDO_XPU_RAM_CACHE_GB, then re-analyze.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
