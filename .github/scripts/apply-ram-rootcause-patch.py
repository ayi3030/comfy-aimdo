#!/usr/bin/env python3
"""
Apply the RAM-layer root-cause fix (Arc B580 sysmem-fallback DEVICE_LOST crash)
onto the community-fork native checkout before the DLL is built.

WHY THIS EXISTS
---------------
The CI builds aimdo_xpu.dll from the community fork xiangyuT/comfy-aimdo-xpu
(pinned COMMUNITY_FORK_SHA) and only overlays THIS repo's src-xpu/ on top.
The root-cause fix for the B580 crash lives in src/plat.h, src/control.c and
src-win/shmem-detect.c -- NONE of which are overlaid today. Instead of widening
the overlay to the whole src/ tree (which would silently drop the fork's L2/L3
unload layer, src/model-vbar.c, and break the overlay-contract gate), this
script surgically re-applies the exact 5 edits onto the freshly-checked-out
community files using stable anchor strings.

SAFETY
------
* Every edit is idempotent: if the NEW form is already present the edit is
  skipped (so re-runs and already-patched trees are no-ops).
* If an expected OLD anchor is missing AND the NEW form is not present either,
  the script fails loudly (non-zero exit) -- turning "the community fork
  changed the code shape" into a red build instead of a silently incomplete
  DLL. The overlay-contract gate (exported symbols) then independently proves
  model-vbar.c is still compiled in.

Usage: python apply-ram-rootcause-patch.py <community-checkout-dir>
  e.g. python apply-ram-rootcause-patch.py native
"""
import os
import sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "native"

# Relative paths inside the community-fork checkout.
FILES = {
    "plat_h": os.path.join(BASE, "src", "plat.h"),
    "control_c": os.path.join(BASE, "src", "control.c"),
    "shmem_c": os.path.join(BASE, "src-win", "shmem-detect.c"),
}


def read(p):
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


def write(p, s):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(s)


def _find_line(s, anchor_substr):
    for i, line in enumerate(s.split("\n")):
        if anchor_substr in line:
            return i
    return None


def insert_before(label, path, anchor_substr, block, already_marker=None):
    """Insert `block` (string ending in newline) immediately BEFORE the first
    line containing `anchor_substr` (file-scope insertions)."""
    s = read(path)
    if already_marker is not None and already_marker in s:
        print(f"  [skip] {label}: already applied in {path}")
        return
    idx = _find_line(s, anchor_substr)
    if idx is None:
        print(f"  [FAIL] {label}: anchor line not found in {path}\n"
              f"         expected substring:\n{anchor_substr!r}", file=sys.stderr)
        sys.exit(1)
    lines = s.split("\n")
    lines.insert(idx, block.rstrip("\n"))
    write(path, "\n".join(lines))
    print(f"  [ok]   {label}: patched {path}")


def insert_after(label, path, anchor_substr, block, already_marker=None):
    """Insert `block` (string ending in newline) immediately AFTER the first
    line containing `anchor_substr` (in-function insertions)."""
    s = read(path)
    if already_marker is not None and already_marker in s:
        print(f"  [skip] {label}: already applied in {path}")
        return
    idx = _find_line(s, anchor_substr)
    if idx is None:
        print(f"  [FAIL] {label}: anchor line not found in {path}\n"
              f"         expected substring:\n{anchor_substr!r}", file=sys.stderr)
        sys.exit(1)
    lines = s.split("\n")
    lines.insert(idx + 1, block.rstrip("\n"))
    write(path, "\n".join(lines))
    print(f"  [ok]   {label}: patched {path}")


def apply_edit(label, path, old, new, already_marker=None):
    """Replace first occurrence of `old` with `new` (substring)."""
    s = read(path)
    if already_marker is not None and already_marker in s:
        print(f"  [skip] {label}: already applied in {path}")
        return
    if old not in s:
        print(f"  [FAIL] {label}: anchor not found in {path}\n"
              f"         expected:\n{old!r}", file=sys.stderr)
        sys.exit(1)
    s = s.replace(old, new, 1)
    write(path, s)
    print(f"  [ok]   {label}: patched {path}")


def main():
    for p in FILES.values():
        if not os.path.isfile(p):
            print(f"[FAIL] community checkout file missing: {p}", file=sys.stderr)
            sys.exit(1)

    print(f"Applying RAM-layer root-cause patch onto '{BASE}/' ...")

    # ---- src/plat.h -------------------------------------------------------
    # 1) Declare external_vram_usage + effective_book_a() at FILE SCOPE, before
    #    budget_deficit (so the inline is defined before its use).
    insert_before(
        "plat.h:declare effective_book_a",
        FILES["plat_h"],
        "budget_deficit(size_t size)",
        (
            "/* RAM-layer root-cause fix (B580 sysmem fallback): feed torch's real\n"
            "   device usage into the admission gate via effective_book_a(). See\n"
            "   RAM_LAYER_CRASH_ROOTCAUSE.md sec.12-13. */\n"
            "extern uint64_t external_vram_usage;\n"
            "static inline size_t effective_book_a(void) {\n"
            "    return total_vram_usage > external_vram_usage ? total_vram_usage : external_vram_usage;\n"
            "}\n"
        ),
        already_marker="effective_book_a(void)",
    )
    # 2) Introduce `book_a` right after poll_budget_deficit() (in-function).
    insert_after(
        "plat.h:book_a var",
        FILES["plat_h"],
        "poll_budget_deficit(&prevailing_deficit_method)",
        "    size_t book_a = effective_book_a();\n",
        already_marker="size_t book_a = effective_book_a();",
    )
    # 3) Use book_a in deficit_simple.
    apply_edit(
        "plat.h:deficit_simple",
        FILES["plat_h"],
        "total_vram_usage + size",
        "book_a + size",
        already_marker="book_a + size",
    )
    # 4) Use book_a in deficit_delta.
    apply_edit(
        "plat.h:deficit_delta",
        FILES["plat_h"],
        "total_vram_usage -",
        "book_a -",
        already_marker="book_a -",
    )

    # ---- src/control.c ----------------------------------------------------
    # Setter at FILE SCOPE, before cleanup().
    insert_before(
        "control.c:setter",
        FILES["control_c"],
        "void cleanup(void)",
        (
            "/* RAM-layer root-cause fix: expose torch's real device usage to the\n"
            "   admission gate (see plat.h:external_vram_usage / effective_book_a). */\n"
            "uint64_t external_vram_usage = 0;\n"
            "\n"
            "SHARED_EXPORT\n"
            "void aimdo_set_external_vram_usage(uint64_t usage) {\n"
            "    external_vram_usage = usage;\n"
            "}\n"
        ),
        already_marker="aimdo_set_external_vram_usage",
    )

    # ---- src-win/shmem-detect.c -------------------------------------------
    # A) Clamp DXGI budget to physical VRAM right after it is read (in-function).
    insert_after(
        "shmem-detect.c:clamp",
        FILES["shmem_c"],
        "effective_budget = info.Budget",
        (
            "            /* Intel Arc / Battlemage default sysmem fallback lets the OS report a\n"
            "             * DXGI LOCAL Budget larger than the physical VRAM. That \"extra\" is\n"
            "             * system RAM, not VRAM, and a device tensor still has to fit in\n"
            "             * physical VRAM -- so clamp the admission budget to capacity.\n"
            "             * See RAM_LAYER_CRASH_ROOTCAUSE.md sec.12-13. */\n"
            "            if (effective_budget > vram_capacity) {\n"
            "                log(DEBUG,\n"
            "                    \"%s: sysmem fallback detected (DXGI budget %zu MB > physical VRAM %zu MB); \"\n"
            "                    \"clamping VRAM-admission budget to physical capacity\\n\",\n"
            "                    __func__, (size_t)(info.Budget / M), (size_t)(vram_capacity / M));\n"
            "                effective_budget = vram_capacity;\n"
            "            }\n"
        ),
        already_marker="sysmem fallback detected (DXGI budget",
    )
    # B) Account for torch's real usage in deficit_sync.
    apply_edit(
        "shmem-detect.c:deficit_sync",
        FILES["shmem_c"],
        "total_vram_usage + WDDM_BUDGET_HEADROOM",
        "effective_book_a() + WDDM_BUDGET_HEADROOM",
        already_marker="effective_book_a() + WDDM_BUDGET_HEADROOM",
    )

    print("RAM-layer root-cause patch applied successfully.")


if __name__ == "__main__":
    main()
