#!/usr/bin/env python3
"""Apply-path test against the REAL community-fork base (fetched from
xiangyuT/comfy-aimdo-xpu @ COMMUNITY_FORK_SHA via the API into _cfbase/).

Copies the real base into a temp native/ tree, runs the real patcher, and
asserts every root-cause edit landed in the correct place with no duplicate
symbol definitions (which would break the link). Does NOT use my local patched
files -- those are irrelevant to CI (the build patches the community base).
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, ".github", "scripts", "apply-ram-rootcause-patch.py")
PY = sys.executable
# _cfbase was produced by _ci_diag.py (real community base) at the workspace
# root. ROOT == <ws>/aimdo-xpu/fork, so dirname(dirname(ROOT)) == <ws>.
SRC_BASE = os.path.join(os.path.dirname(os.path.dirname(ROOT)), "_cfbase")
NATIVE = os.path.join(ROOT, "_rt_native")


def copy(src_rel, dst_rel):
    s = os.path.join(SRC_BASE, src_rel)
    d = os.path.join(NATIVE, dst_rel)
    assert os.path.isfile(s), f"missing real base file: {s}"
    os.makedirs(os.path.dirname(d), exist_ok=True)
    with open(s, "r", encoding="utf-8") as f:
        data = f.read()
    with open(d, "w", encoding="utf-8", newline="\n") as f:
        f.write(data)


def main():
    if not os.path.isdir(SRC_BASE):
        print(f"[FAIL] real community base not found at {SRC_BASE}; run _ci_diag.py first")
        sys.exit(1)
    copy("src/plat.h", "src/plat.h")
    copy("src/control.c", "src/control.c")
    copy("src-win/shmem-detect.c", "src-win/shmem-detect.c")

    r = subprocess.run([PY, SCRIPT, "_rt_native"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("PATCH RUN FAILED:\n", r.stdout, r.stderr)
        sys.exit(1)

    fails = []

    def check(path, must_have, must_one=None, order=None):
        with open(path, "r", encoding="utf-8") as f:
            s = f.read()
        for m in must_have:
            if m not in s:
                fails.append(f"{os.path.basename(path)}: missing {m!r}")
        if must_one:
            # exactly one occurrence required (no duplicate definitions)
            for m, n in must_one.items():
                c = s.count(m)
                if c != n:
                    fails.append(f"{os.path.basename(path)}: {m!r} count={c} expected={n}")
        if order:
            for a, b in order:
                if a in s and b in s and s.index(a) > s.index(b):
                    fails.append(f"{os.path.basename(path)}: order wrong ({a!r} should precede {b!r})")

    check(os.path.join(NATIVE, "src", "plat.h"),
          ["uint64_t effective_book_a(void)", "extern uint64_t external_vram_usage;",
           "uint64_t book_a = effective_book_a();", "book_a + size", "book_a -"],
          order=[("uint64_t effective_book_a(void)", "budget_deficit(size_t size)"),
                 ("uint64_t effective_book_a(void)", "uint64_t book_a = effective_book_a();")])

    check(os.path.join(NATIVE, "src", "control.c"),
          ["aimdo_set_external_vram_usage", "uint64_t external_vram_usage = 0;",
           "void aimdo_set_external_vram_usage(uint64_t usage)"],
          must_one={"uint64_t external_vram_usage = 0;": 1,          # exactly one definition
                    "void aimdo_set_external_vram_usage(uint64_t usage)": 1},
          order=[("aimdo_set_external_vram_usage", "void cleanup(void)")])

    check(os.path.join(NATIVE, "src-win", "shmem-detect.c"),
          ["sysmem fallback detected (DXGI budget", "if (effective_budget > vram_capacity)",
           "effective_usage = effective_book_a();",
           "effective_usage = (info.CurrentUsage > external_vram_usage ? info.CurrentUsage : external_vram_usage);"],
          order=[("effective_budget = info.Budget", "if (effective_budget > vram_capacity)"),
                 ("uint64_t effective_usage = effective_book_a();", "effective_usage = (info.CurrentUsage > external_vram_usage")])

    if fails:
        print("APPLY-PATH TEST FAILED:")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("APPLY-PATH TEST OK: root-cause edits applied to REAL community base; "
          "no duplicate symbol definitions; scopes/order correct.")


if __name__ == "__main__":
    try:
        main()
    finally:
        shutil.rmtree(NATIVE, ignore_errors=True)
