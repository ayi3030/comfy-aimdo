#!/usr/bin/env python3
"""Deterministic apply-path test for apply-ram-rootcause-patch.py.

Builds minimal synthetic "community base" stubs containing exactly the anchor
substrings the script looks for, runs the real patcher, and asserts the new
forms appear in the correct scope/order (file-scope vs in-function, and
declaration before use). Does NOT depend on the local patched file's formatting.
"""
import os
import subprocess
import sys
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "apply-ram-rootcause-patch.py")
PY = sys.executable
NATIVE = os.path.join(ROOT, "_rt_native")


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def main():
    # Synthetic community-base stubs (only the anchor shapes matter).
    plat = (
        "extern int64_t simple_vram_headroom;\n"
        "static inline ssize_t budget_deficit(size_t size) {\n"
        "    poll_budget_deficit(&prevailing_deficit_method);\n"
        "    deficit_simple = (ssize_t)(total_vram_usage + size) + (ssize_t)simple_vram_headroom - (ssize_t)vram_capacity;\n"
        "    deficit_delta = deficit_sync + (ssize_t)total_vram_usage - (ssize_t)total_vram_last_check + (ssize_t)size;\n"
        "    return deficit;\n"
        "}\n"
    )
    ctrl = (
        "uint64_t get_total_vram_usage(void *devctx) {\n"
        "    return total_vram_usage;\n"
        "}\n"
        "void cleanup(void) {\n"
        "    teardown();\n"
        "}\n"
    )
    shmem = (
        "bool poll_budget_deficit(const char **m) {\n"
        "    effective_budget = info.Budget;\n"
        "    deficit_sync = (ssize_t)(total_vram_usage + WDDM_BUDGET_HEADROOM) - (ssize_t)effective_budget;\n"
        "    return true;\n"
        "}\n"
    )
    write(os.path.join(NATIVE, "src", "plat.h"), plat)
    write(os.path.join(NATIVE, "src", "control.c"), ctrl)
    write(os.path.join(NATIVE, "src-win", "shmem-detect.c"), shmem)

    r = subprocess.run([PY, SCRIPT, "_rt_native"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("PATCH RUN FAILED:\n", r.stdout, r.stderr)
        sys.exit(1)

    fails = []

    def check(path, must_have, must_not_have, order=None):
        with open(path, "r", encoding="utf-8") as f:
            s = f.read()
        for m in must_have:
            if m not in s:
                fails.append(f"{os.path.basename(path)}: missing {m!r}")
        for m in must_not_have:
            if m in s:
                fails.append(f"{os.path.basename(path)}: still present {m!r}")
        if order:
            for a, b in order:
                if a in s and b in s and s.index(a) > s.index(b):
                    fails.append(f"{os.path.basename(path)}: order wrong ({a!r} should precede {b!r})")

    check(os.path.join(NATIVE, "src", "plat.h"),
          ["effective_book_a(void)", "extern uint64_t external_vram_usage;",
           "size_t book_a = effective_book_a();", "book_a + size", "book_a -"],
          ["total_vram_usage + size", "total_vram_usage -"],
          order=[("effective_book_a(void)", "budget_deficit(size_t size)"),
                 ("effective_book_a(void)", "size_t book_a = effective_book_a();")])

    check(os.path.join(NATIVE, "src", "control.c"),
          ["aimdo_set_external_vram_usage", "uint64_t external_vram_usage = 0;",
           "void aimdo_set_external_vram_usage(uint64_t usage)"],
          [],
          order=[("aimdo_set_external_vram_usage", "void cleanup(void)")])

    check(os.path.join(NATIVE, "src-win", "shmem-detect.c"),
          ["sysmem fallback detected (DXGI budget", "if (effective_budget > vram_capacity)",
           "effective_book_a() + WDDM_BUDGET_HEADROOM"],
          ["total_vram_usage + WDDM_BUDGET_HEADROOM"],
          order=[("effective_budget = info.Budget", "if (effective_budget > vram_capacity)"),
                 ("if (effective_budget > vram_capacity)", "effective_book_a() + WDDM_BUDGET_HEADROOM")])

    if fails:
        print("APPLY-PATH TEST FAILED:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("APPLY-PATH TEST OK: all 5 edits applied in correct scope/order; no stale anchors remain.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(NATIVE, ignore_errors=True)
