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
script surgically re-applies the RAM-layer root-cause edits onto the
freshly-checked-out community files using stable anchor strings.

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


def insert_before_skip_prev(label, path, anchor_substr, block, prev_marker=None,
                            already_marker=None):
    """Insert `block` BEFORE `anchor_substr`, but if the line immediately
    preceding the anchor starts with `prev_marker`, insert before THAT line
    instead. Needed when a storage-class marker (e.g. SHARED_EXPORT) sits on its
    own line directly above the anchor and would otherwise be wrongly attached to
    our inserted definition."""
    s = read(path)
    if already_marker is not None and already_marker in s:
        print(f"  [skip] {label}: already applied in {path}")
        return
    idx = _find_line(s, anchor_substr)
    if idx is None:
        print(f"  [FAIL] {label}: anchor line not found in {path}\n"
              f"         expected substring:\n{anchor_substr!r}", file=sys.stderr)
        sys.exit(1)
    insert_at = idx
    lines = s.split("\n")
    if prev_marker is not None and idx > 0 and lines[idx - 1].strip().startswith(prev_marker):
        insert_at = idx - 1
    lines.insert(insert_at, block.rstrip("\n"))
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


def guard_no_preexisting_symbols(base):
    """Fail early if the community checkout already defines our injected symbols
    (would otherwise become a duplicate-definition link error).

    This guard exists to catch a symbol WE FAILED to patch, so it must not
    fire on a tree our own edits already patched -- that is the normal state of
    a re-run. The previous ordering made the script refuse to re-run at all on
    an already-patched tree, which defeated the documented idempotence guarantee
    and would break any CI job that reuses a cached checkout.

    Distinguish the two cases: a symbol sitting in a file this script patches,
    in the exact form we inject, is our own previous run echoing back. A symbol
    anywhere else, or in any other form, is genuinely pre-existing in the
    community tree and must still fail loudly.
    """
    patterns = [
        ("uint64_t external_vram_usage =", "definition of external_vram_usage"),
        ("void aimdo_set_external_vram_usage", "definition of aimdo_set_external_vram_usage"),
    ]
    # Files this script edits, and the exact text it writes into each.
    ours = {
        os.path.normpath(FILES["control_c"]): (
            "uint64_t external_vram_usage = 0;",
            "void aimdo_set_external_vram_usage(uint64_t usage) {",
        ),
    }
    exts = (".c", ".h", ".cpp", ".hpp", ".cc")
    hits = []
    for root, _dirs, files in os.walk(base):
        for f in files:
            if not f.lower().endswith(exts):
                continue
            p = os.path.join(root, f)
            try:
                data = open(p, "r", encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            expected = ours.get(os.path.normpath(p))
            for pat, desc in patterns:
                if pat not in data:
                    continue
                if expected and all(text in data for text in expected):
                    # Our own injected form in a file we own: an echo of a
                    # previous run, not a conflict.
                    continue
                hits.append((p, desc))
    if hits:
        print("[FAIL] pre-existing root-cause symbols found in community checkout "
              "-- the patch would create a duplicate definition (link error). "
              "Refusing to patch.", file=sys.stderr)
        for p, desc in hits:
            print(f"         {desc} already present in {p}", file=sys.stderr)
        sys.exit(1)
    print("  [ok]   no pre-existing root-cause symbols in community checkout")


def main():
    for p in FILES.values():
        if not os.path.isfile(p):
            print(f"[FAIL] community checkout file missing: {p}", file=sys.stderr)
            sys.exit(1)

    # Guard (review finding I-1): the anchor check only inspects 3 files, so a
    # pre-existing definition of our symbols elsewhere in the full community
    # checkout would slip through and cause a duplicate-definition link error.
    # Fail loudly -- and early -- if either symbol is already defined.
    guard_no_preexisting_symbols(BASE)

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
            "static inline uint64_t effective_book_a(void) {\n"
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
        "    uint64_t book_a = effective_book_a();\n",
        already_marker="uint64_t book_a = effective_book_a();",
    )
    # 3) Use book_a in deficit_simple.
    apply_edit(
        "plat.h:deficit_simple",
        FILES["plat_h"],
        "total_vram_usage + size",
        "book_a + size",
        already_marker="book_a + size",
    )
    # 4) deficit_delta MUST stay on the ledger's own increment, NOT on book_a.
    #
    #    Regression fix (B580 over-restriction). An earlier revision rewrote
    #    deficit_delta's growth term to book_a as well:
    #        deficit_delta = deficit_sync + book_a - total_vram_last_check + size
    #    That is semantically wrong, not merely conservative. total_vram_last_check
    #    is a snapshot of the *ledger* (total_vram_usage) taken at the previous
    #    poll; it is the baseline for measuring how much the ledger GREW since.
    #    Substituting book_a (= max(ledger, external_vram_usage)) against a
    #    ledger baseline adds the entire external term a second time, as if it
    #    were new growth:
    #        book_a - total_vram_last_check
    #          = (ledger - last_check)  +  max(0, external - ledger)
    #    On XPU, torch's reservation dwarfs the ledger, so the second term is
    #    large and CONSTANT -- deficit_delta becomes structurally positive on
    #    every poll, for every allocation size, regardless of real headroom.
    #    The gate then trips continuously, which surfaces as repeated synthetic
    #    OOM -> torch cache flush -> retry (the observed B580 "inconsistent"
    #    behaviour and its stalls).
    #
    #    On CUDA/ROCm external_vram_usage is never fed, so book_a == ledger and
    #    the two forms are identical -- which is exactly why the bug was
    #    invisible on NVIDIA and only appeared on B580.
    #
    #    The external feed is still fully effective: it participates in
    #    deficit_simple (the absolute ceiling term, below) and in the WDDM
    #    effective_usage clamp in shmem-detect.c. It must only be kept out of
    #    the *delta* term, which measures ledger growth against a ledger
    #    baseline.
    #
    #    Reversal is idempotent in both directions: a tree patched by the older
    #    revision is restored, a clean tree is left alone.
    apply_edit(
        "plat.h:deficit_delta (revert book_a growth term)",
        FILES["plat_h"],
        "deficit_sync + (ssize_t)book_a -",
        "deficit_sync + (ssize_t)total_vram_usage -",
        already_marker="deficit_sync + (ssize_t)total_vram_usage -",
    )

    # ---- src/control.c ----------------------------------------------------
    # Setter at FILE SCOPE, before cleanup(). The community base has a lone
    # `SHARED_EXPORT` line directly above `void cleanup(void)`; insert BEFORE
    # that marker (prev_marker) so SHARED_EXPORT stays attached to cleanup and is
    # NOT wrongly applied to our variable definition (which would make the
    # definition's storage class (dllexport) differ from the `extern` declaration
    # in plat.h and trigger MSVC C2370 "redefinition; different storage class").
    insert_before_skip_prev(
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
        prev_marker="SHARED_EXPORT",
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
    # B) Feed torch's real device usage (external_vram_usage) into the WDDM
    #    usage term so the gate never undercounts the XPU footprint. The
    #    community fork already prefers WDDM CurrentUsage over Book A; we take
    #    the max of that and the externally-fed torch reserved bytes.
    apply_edit(
        "shmem-detect.c:effective_usage init",
        FILES["shmem_c"],
        "uint64_t effective_usage = total_vram_usage;",
        "uint64_t effective_usage = effective_book_a();",
        already_marker="effective_usage = effective_book_a()",
    )
    apply_edit(
        "shmem-detect.c:effective_usage WDDM",
        FILES["shmem_c"],
        "effective_usage = info.CurrentUsage;",
        "effective_usage = (info.CurrentUsage > external_vram_usage ? info.CurrentUsage : external_vram_usage);",
        already_marker="info.CurrentUsage > external_vram_usage",
    )
    # C) Real-free FIT check -- size-aware, capacity-derived, vendor-neutral.
    #
    #    Why the previous floor guard had to be reverted, not just retuned.
    #    It denied whenever free < a fixed floor. But on B580 the observed
    #    DEVICE_LOST fired at free=3221 MB while the SAME log series recorded a
    #    normal, healthy operating band of 3000-5500 MB. 3221 sits INSIDE that
    #    band, so no threshold can separate "healthy" from "about to crash": any
    #    floor low enough to catch the crash also fires on healthy steady-state
    #    allocations. That is precisely the nondeterministic, card-dependent,
    #    one-SKU-only behaviour that had to be removed -- and it is why tuning
    #    the constant (4096, 3072, 2048 ...) was never going to be sound.
    #
    #    The sound signal is a FIT CHECK: an allocation is admissible iff it
    #    FITS in the real free memory, plus a small reserve for the driver-side
    #    pools and a transient kernel's working set that no ledger sees. This is
    #    size-aware, so a small transient buffer is admitted on a device with
    #    plenty free, while a genuinely oversized request is refused before it
    #    can push the device into an unrecoverable OUT_OF_RESOURCES/DEVICE_LOST.
    #
    #    Device- and vendor-neutral by construction:
    #      * it needs only `free_vram`, which every backend already reports;
    #      * the reserve is a capped share of CAPACITY, so one rule is correct
    #        from a 6 GB laptop dGPU to a 96 GB board, with no per-SKU constant;
    #      * the community's own deficit_cuda term is left completely intact, so
    #        NVIDIA/ROCm behaviour on this shared Windows path is unchanged.
    #
    #    Exported as real_free_fit_deficit() from the compiled translation unit
    #    and consumed by plat.h:budget_deficit, which is where `size` is known.
    insert_before(
        "shmem-detect.c:real_free_fit_deficit define",
        FILES["shmem_c"],
        "#define WDDM_BUDGET_HEADROOM",
        (
            "/* RAM-layer real-free fit check (see RAM_LAYER_CRASH_ROOTCAUSE.md\n"
            " * sec.12-13). Capacity-derived reserve; no per-vendor / per-SKU\n"
            " * constant, and the community deficit_cuda signal is untouched. */\n"
            "#define REAL_FREE_RESERVE_DEN  16\n"
            "#define REAL_FREE_RESERVE_CAP  (512ULL * 1024 * 1024)\n"
            "\n"
            "/* True device free / total from the most recent successful poll.\n"
            " * Exposed to plat.h (real_free_fit_deficit) and to src-xpu/stubs.c.\n"
            " * Defined HERE because this file is the translation unit that is\n"
            " * actually compiled into the DLL by apply-ram-rootcause-patch.py;\n"
            " * a definition in this repo's own src-win/shmem-detect.c would never\n"
            " * reach the build. Both are 0 until the first successful poll, which\n"
            " * is what makes the check below a no-op while unknown. */\n"
            "uint64_t last_free_vram = 0;\n"
            "uint64_t last_total_vram = 0;\n"
            "\n"
            "SHARED_EXPORT\n"
            "ssize_t real_free_fit_deficit(uint64_t size) {\n"
            "    uint64_t capacity, reserve, available;\n"
            "    if (last_free_vram == 0) {\n"
            "        return 0; /* no successful poll yet: contribute no opinion */\n"
            "    }\n"
            "    capacity = last_total_vram ? last_total_vram : vram_capacity;\n"
            "    reserve = capacity / REAL_FREE_RESERVE_DEN;\n"
            "    if (reserve > REAL_FREE_RESERVE_CAP) {\n"
            "        reserve = REAL_FREE_RESERVE_CAP;\n"
            "    }\n"
            "    available = (last_free_vram > reserve) ? (last_free_vram - reserve) : 0;\n"
            "    if (size <= available) {\n"
            "        return 0; /* fits: the device can serve this request */\n"
            "    }\n"
            "    return (ssize_t)(size - available);\n"
            "}\n"
        ),
        already_marker="real_free_fit_deficit(uint64_t size)",
    )
    # Keep the original deficit_cuda term byte-for-byte and only record the true
    # free/total for real_free_fit_deficit() and for the deny-path log.
    apply_edit(
        "shmem-detect.c:capture true free (keep deficit_cuda intact)",
        FILES["shmem_c"],
        "        ssize_t headroom = used_nvml ? NVML_BUDGET_HEADROOM : CUDA_BUDGET_HEADROOM / 2;\n"
        "        ssize_t deficit_cuda = headroom - (ssize_t)free_vram;",
        "        ssize_t headroom = used_nvml ? NVML_BUDGET_HEADROOM : CUDA_BUDGET_HEADROOM / 2;\n"
        "        ssize_t deficit_cuda = headroom - (ssize_t)free_vram;\n"
        "        last_free_vram = free_vram;\n"
        "        last_total_vram = total_vram;",
        already_marker="last_total_vram = total_vram;",
    )
    # The community's deficit_cuda log line and its selection against
    # deficit_sync are deliberately LEFT UNTOUCHED. An earlier revision rewrote
    # both to a replacement 'deficit_real' term, which silently changed the
    # vendor signal on this shared Windows path (CUDA/ROCm included). The new,
    # size-aware fit check is a separate term consumed by plat.h, so the
    # existing signal keeps its exact original meaning and precedence.

    # ---- src/plat.h (second pass) ----------------------------------------
    # D) Fold the size-aware real-free fit check into budget_deficit.
    #
    #    This is the only place that knows `size`, which is what makes the
    #    check a fit test rather than an arbitrary free-memory threshold. The
    #    term is MAXed against the existing ones, so it can only ever tighten
    #    the gate for a request that genuinely does not fit -- it can never
    #    make a healthy request fail, and it cannot alter the NVIDIA/ROCm
    #    outcome for any request that already passed.
    insert_before(
        "plat.h:declare real_free_fit_deficit",
        FILES["plat_h"],
        "static inline ssize_t budget_deficit(size_t size)",
        (
            "/* RAM-layer real-free fit check, defined in the compiled\n"
            "   src-win/shmem-detect.c translation unit (see\n"
            "   RAM_LAYER_CRASH_ROOTCAUSE.md sec.12-13). Returns 0 while no\n"
            "   successful device poll has happened, or when the request fits\n"
            "   in real free memory. No-op on builds without that TU. */\n"
            "ssize_t real_free_fit_deficit(uint64_t size);\n"
        ),
        already_marker="real_free_fit_deficit(uint64_t size);",
    )
    apply_edit(
        "plat.h:budget_deficit include fit check",
        FILES["plat_h"],
        "    deficit = MAX(deficit_simple, deficit_delta) + (ssize_t)extra_vram_headroom;",
        "    ssize_t deficit_fit = real_free_fit_deficit((uint64_t)size);\n"
        "    deficit = MAX(MAX(deficit_simple, deficit_delta), deficit_fit) +\n"
        "               (ssize_t)extra_vram_headroom;",
        already_marker="MAX(MAX(deficit_simple, deficit_delta), deficit_fit)",
    )
    apply_edit(
        "plat.h:budget_deficit declare deficit_fit",
        FILES["plat_h"],
        "    ssize_t deficit_simple, deficit_delta;",
        "    ssize_t deficit_simple, deficit_delta, deficit_fit;",
        already_marker="deficit_simple, deficit_delta, deficit_fit",
    )
    apply_edit(
        "plat.h:budget_deficit log fit term",
        FILES["plat_h"],
        "            deficit_simple > deficit_delta ? \"simple\" : prevailing_deficit_method,",
        "            deficit_fit > MAX(deficit_simple, deficit_delta)\n"
        "                ? \"real-free-fit\"\n"
        "                : (deficit_simple > deficit_delta ? \"simple\" : prevailing_deficit_method),",
        already_marker="\"real-free-fit\"",
    )

    print("RAM-layer root-cause patch applied successfully.")


if __name__ == "__main__":
    main()
