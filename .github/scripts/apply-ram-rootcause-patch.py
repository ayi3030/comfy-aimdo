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
import re
import sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "native"

# Relative paths inside the community-fork checkout.
FILES = {
    "plat_h": os.path.join(BASE, "src", "plat.h"),
    "control_c": os.path.join(BASE, "src", "control.c"),
    "shmem_c": os.path.join(BASE, "src-win", "shmem-detect.c"),
    "hostbuf_plat_c": os.path.join(BASE, "src-win", "hostbuf-plat.c"),
}


# Original hostbuf-plat.c address-space functions (community cc3729f),
# kept verbatim for the non-XPU #else branch of the injected block.
_HB_HOSTBUF_ORIG = 'void *hostbuf_reserve_address_space(size_t size) {\n    return VirtualAlloc(NULL, size, MEM_RESERVE, PAGE_NOACCESS);\n}\n\nbool hostbuf_commit_address_space(void *ptr, size_t size) {\n    return VirtualAlloc(ptr, size, MEM_COMMIT, PAGE_READWRITE) == ptr;\n}\n\nbool hostbuf_decommit_address_space(void *ptr, size_t size) {\n    return VirtualFree(ptr, size, MEM_DECOMMIT);\n}\n\nvoid hostbuf_release_address_space(void *ptr, size_t size) {\n    if (ptr) {\n        VirtualFree(ptr, 0, MEM_RELEASE);\n    }\n}'

# XPU page-locked host buffer branch (#if defined(AIMDO_XPU)); it wraps
# _HB_HOSTBUF_ORIG verbatim inside the #else arm.
_HB_HOSTBUF_NEW = '''
#if defined(AIMDO_XPU)
/* ==========================================================================
 * XPU (Intel Arc / oneAPI Level Zero) RAM-layer host buffer -- SAFETY REVISION
 * (xpu-pinned-host-fix) over the original eager whole-block pin.
 *
 *   * Per-buffer decision, not a process-wide latch. Each HostBuffer probes L0
 *     host-USM availability AND a host-commit budget INDEPENDENTLY and records
 *     its own mode in a base->mode table. One buffer that cannot be pinned
 *     (capability or budget) falls back to pageable VirtualAlloc for that
 *     buffer only -- it no longer forces the whole process pageable, nor does a
 *     single pin failure abort the RAM cache.
 *
 *   * Budget-gated pinning. Pinned L0 host USM is non-pageable and counts
 *     against the host commit budget; pinning multi-GiB RAM-cache buffers is
 *     what drove the ~13 GB host-commit inflation -> VRAM deficit ->
 *     DEVICE_LOST / OOR on B580. A buffer is pinned only when its reserved size
 *     fits inside a fraction of currently-available physical RAM, so aggregate
 *     pinned host commit stays bounded while small/medium buffers still get the
 *     DMA-visible pin.
 *
 *   * Real return. The pinned block is freed via cuMemFreeHost at release
 *     (F-1 NULL-safe), so a buffer's pinned memory is actually returned to the
 *     OS at end of life instead of leaking until process exit. L0 host USM is a
 *     single indivisible block (no reserve/commit split), so mid-life partial
 *     decommit is a no-op -- benign, because the budget gate keeps the pinned
 *     working set small.
 *
 * Mode recovery: commit/decommit/release receive a pointer that may be a
 * base+offset inside the buffer. We recover the allocation base with
 * VirtualQuery() and look it up in the per-buffer table, so no public API
 * signature changes (hostbuf.c / hostbuf-decommit.c are untouched).
 *
 * src-win/ is not overlaid by the build workflow, so this reaches the DLL only
 * through apply-ram-rootcause-patch.py -- which is why the edit lives here.
 * ======================================================================== */
extern int xpu_host_alloc_is_pinned(void);

/* -1 = not yet probed, 0 = L0 host USM unavailable, 1 = available. CAPABILITY
 * flag only (mirrors the dispatch-side probe); it is NOT the per-buffer pin
 * decision, which lives in g_pin_map below. The old process-wide g_hostbuf_pinned
 * mode mirror is gone -- this is purely a capability probe now. */
static int g_hostbuf_pinned = -1;

/* Per-buffer pin mode, keyed by the allocation base returned from reserve.
 * Replaces the old single process-wide latch. Entries are added on reserve and
 * removed on release, so the table stays bounded to live buffers (a handful),
 * and a statically-initialised lock makes it safe against the async decommit
 * worker touching it concurrently with a reserve on the main thread. */
typedef struct { void *base; int pinned; } PinMode;
#define HOSTBUF_PIN_MAP_MAX 256
static PinMode g_pin_map[HOSTBUF_PIN_MAP_MAX];
static int g_pin_map_n = 0;

#if defined(_WIN32) || defined(_WIN64)
static SRWLOCK g_pin_lock = SRWLOCK_INIT;
#define PIN_LOCK()   AcquireSRWLockExclusive(&g_pin_lock)
#define PIN_UNLOCK() ReleaseSRWLockExclusive(&g_pin_lock)
#else
#include <pthread.h>
static pthread_mutex_t g_pin_lock = PTHREAD_MUTEX_INITIALIZER;
#define PIN_LOCK()   pthread_mutex_lock(&g_pin_lock)
#define PIN_UNLOCK() pthread_mutex_unlock(&g_pin_lock)
#endif

static void hostbuf_pin_map_set(void *base, int pinned) {
    PIN_LOCK();
    if (g_pin_map_n < HOSTBUF_PIN_MAP_MAX) {
        g_pin_map[g_pin_map_n].base = base;
        g_pin_map[g_pin_map_n].pinned = pinned;
        g_pin_map_n++;
    }
    PIN_UNLOCK();
}

static int hostbuf_pin_map_get(void *base) {
    int found = -1;
    PIN_LOCK();
    for (int i = 0; i < g_pin_map_n; i++) {
        if (g_pin_map[i].base == base) {
            found = g_pin_map[i].pinned;
            break;
        }
    }
    PIN_UNLOCK();
    return found;  /* unknown base -> caller treats as pageable (safe) */
}

static void hostbuf_pin_map_clear(void *base) {
    PIN_LOCK();
    for (int i = 0; i < g_pin_map_n; i++) {
        if (g_pin_map[i].base == base) {
            g_pin_map[i] = g_pin_map[g_pin_map_n - 1];
            g_pin_map_n--;
            break;
        }
    }
    PIN_UNLOCK();
}

/* Recover the allocation base from any pointer inside the region. */
static void *hostbuf_alloc_base(void *ptr) {
    MEMORY_BASIC_INFORMATION mbi;
    if (ptr && VirtualQuery(ptr, &mbi, sizeof(mbi)) == sizeof(mbi)) {
        return mbi.AllocationBase;
    }
    return ptr;  /* fall back to the pointer itself (release receives the exact
                  * base, so this is exact for the release path) */
}

/* Decide whether THIS buffer may be pinned, forcing the one-time capability
 * probe if hostbuf reserves before any xpu_host_alloc() call. */
static int hostbuf_xpu_should_pin(size_t size) {
    if (g_hostbuf_pinned < 0) {
        /* Force xpu_host_alloc's one-time decision with a 1-byte
         * allocate/release, then read back the exact mode (g_host_use_l0). */
        void *p = NULL;
        if (cuMemAllocHost(&p, 1) == CUDA_SUCCESS && p) {
            cuMemFreeHost(p);
        }
        g_hostbuf_pinned = (xpu_host_alloc_is_pinned() == 1) ? 1 : 0;
    }
    if (g_hostbuf_pinned != 1) {
        return 0;  /* L0 host USM unavailable -> pageable */
    }
    MEMORYSTATUSEX ms;
    ms.dwLength = sizeof(ms);
    if (GlobalMemoryStatusEx(&ms)) {
        /* Pin a single buffer only if its reserved size is at most ~1/8 of
         * currently-available physical RAM. This bounds aggregate pinned host
         * commit well below the level that triggered the deficit on B580. */
        if ((ULONGLONG)size > ms.ullAvailPhys / 8ULL) {
            return 0;
        }
    }
    return 1;
}

void *hostbuf_reserve_address_space(size_t size) {
    if (hostbuf_xpu_should_pin(size)) {
        void *p = NULL;
        if (cuMemAllocHost(&p, size) == CUDA_SUCCESS && p) {
            hostbuf_pin_map_set(p, 1);
            return p;
        }
        /* Pin failed for THIS buffer: fall back to pageable, never mix. */
    }
    void *p = VirtualAlloc(NULL, size, MEM_RESERVE, PAGE_NOACCESS);
    hostbuf_pin_map_set(p, 0);
    return p;
}

bool hostbuf_commit_address_space(void *ptr, size_t size) {
    void *base = hostbuf_alloc_base(ptr);
    if (hostbuf_pin_map_get(base) == 1) {
        /* zeMemAllocHost already committed and pinned the whole block. */
        (void)ptr;
        (void)size;
        return true;
    }
    return VirtualAlloc(ptr, size, MEM_COMMIT, PAGE_READWRITE) == ptr;
}

bool hostbuf_decommit_address_space(void *ptr, size_t size) {
    void *base = hostbuf_alloc_base(ptr);
    if (hostbuf_pin_map_get(base) == 1) {
        /* USM cannot be partially released; returned whole at release. */
        (void)ptr;
        (void)size;
        return true;
    }
    return VirtualFree(ptr, size, MEM_DECOMMIT);
}

void hostbuf_release_address_space(void *ptr, size_t size) {
    (void)size;
    if (!ptr) {
        return;
    }
    int mode = hostbuf_pin_map_get(ptr);
    if (mode < 0) {
        mode = hostbuf_pin_map_get(hostbuf_alloc_base(ptr));
    }
    if (mode == 1) {
        /* cuMemFreeHost is NULL-safe across teardown (F-1 guard): the async
         * decommit worker or a HostBuffer.__del__ can still run after
         * aimdo_cuda_runtime_cleanup() zeroed the dispatch table. */
        cuMemFreeHost(ptr);
    } else {
        VirtualFree(ptr, 0, MEM_RELEASE);
    }
    hostbuf_pin_map_clear(ptr);
}
#else  /* CUDA / ROCm / other Windows backends: unchanged, byte for byte */
void *hostbuf_reserve_address_space(size_t size) {
    return VirtualAlloc(NULL, size, MEM_RESERVE, PAGE_NOACCESS);
}

bool hostbuf_commit_address_space(void *ptr, size_t size) {
    return VirtualAlloc(ptr, size, MEM_COMMIT, PAGE_READWRITE) == ptr;
}

bool hostbuf_decommit_address_space(void *ptr, size_t size) {
    return VirtualFree(ptr, size, MEM_DECOMMIT);
}

void hostbuf_release_address_space(void *ptr, size_t size) {
    if (ptr) {
        VirtualFree(ptr, 0, MEM_RELEASE);
    }
}
#endif
'''


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

    # ---- src/plat.h (F-1: NULL-safe host release across teardown) ----------
    # aimdo_cuda_runtime_cleanup() memsets g_cuda to zero. An async decommit
    # worker (src/hostbuf-decommit.c) or a HostBuffer.__del__ can still call
    # cuMemFreeHost / cuMemHostUnregister after that, and the macro dereferences
    # the slot at the CALL SITE -- so a zeroed slot is a NULL function-pointer
    # crash. Make the two release macros NULL-safe: on a torn-down table they
    # simply skip (a harmless leak reclaimed by the OS at exit). Only the
    # release side is guarded; allocation never runs after teardown.
    apply_edit(
        "plat.h:cuMemFreeHost NULL-safe",
        FILES["plat_h"],
        "#define cuMemFreeHost               g_cuda.p_cuMemFreeHost",
        "#define cuMemFreeHost(ptr)          (g_cuda.p_cuMemFreeHost ? "
        "g_cuda.p_cuMemFreeHost(ptr) : CUDA_SUCCESS)",
        already_marker="g_cuda.p_cuMemFreeHost ? g_cuda.p_cuMemFreeHost(ptr)",
    )
    apply_edit(
        "plat.h:cuMemHostUnregister NULL-safe",
        FILES["plat_h"],
        "#define cuMemHostUnregister         g_cuda.p_cuMemHostUnregister",
        "#define cuMemHostUnregister(ptr)    (g_cuda.p_cuMemHostUnregister ? "
        "g_cuda.p_cuMemHostUnregister(ptr) : CUDA_SUCCESS)",
        already_marker="g_cuda.p_cuMemHostUnregister ? g_cuda.p_cuMemHostUnregister(ptr)",
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
            "/* Deliberately NOT SHARED_EXPORT: this is internal to the DLL and\n"
            " * nothing outside links against it. Keeping it out of the export\n"
            " * table avoids spending an export slot on a private helper.\n"
            " *\n"
            " * It MUST keep external linkage (no `static`): src-xpu/stubs.c\n"
            " * calls it via an `extern` declaration, and both translation\n"
            " * units are linked into the same DLL. Marking it static would\n"
            " * break that call with a link error. */\n"
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
    #
    #    PLATFORM PLACEMENT (LNK2019 on non-Windows). real_free_fit_deficit is
    #    defined only in src-win/shmem-detect.c, which is compiled into the
    #    Windows build only. A declaration placed out here -- after the
    #    _WIN32/#endif block that ends around line 74 -- is visible to EVERY
    #    platform, so the Linux, ROCm and macOS builds would reference a symbol
    #    nothing defines and fail at link time with an undefined reference.
    #    src-posix/ has no shmem-detect.c at all, confirming there is no other
    #    definition to rely on.
    #
    #    So the declaration goes INSIDE the _WIN32 branch, next to
    #    poll_budget_deficit which has the same split, and the #else branch
    #    gets a static inline stub returning 0 -- "no real free reading, so no
    #    opinion". That matches how this header already handles
    #    aimdo_wddm_init / aimdo_wddm_cleanup / aimdo_wddm_force_poll /
    #    poll_budget_deficit, and it means the admission behaviour on those
    #    platforms is exactly what it was before this patch: the stub returns
    #    0, so the new term never contributes and cannot change any existing
    #    outcome. The `(void)size` keeps the unused-parameter warning quiet.
    #
    #    ANCHOR SHAPE (why these anchors are single-line). The *_before /
    #    *_after helpers locate their anchor with _find_line(), which tests one
    #    LINE AT A TIME (`anchor_substr in line`). A multi-line anchor is
    #    therefore unsatisfiable by construction, and the run dies with
    #    "anchor line not found" on a pristine community checkout. The previous
    #    revision of this edit used a two-line anchor ("/* cuda-detour.c */\n
    #    bool aimdo_setup_hooks();"), which reproduced exactly that: verified
    #    FAIL on a fresh checkout of COMMUNITY_FORK_SHA. Anchors here must stay
    #    single-line, and must be strings that cannot match anywhere else.
    #
    #    "bool poll_budget_deficit(const char **prevailing_deficit_method);"
    #    (with the trailing semicolon) matches ONLY the _WIN32 declaration: the
    #    #else definition of the same function ends in "{" on its opening line,
    #    so it can never match. _find_line returns the first hit, which is the
    #    declaration inside the _WIN32 branch -- the intended insertion point.
    insert_after(
        "plat.h:declare real_free_fit_deficit (Windows)",
        FILES["plat_h"],
        "bool poll_budget_deficit(const char **prevailing_deficit_method);",
        (
            "/* RAM-layer real-free fit check, defined in the compiled\n"
            "   src-win/shmem-detect.c translation unit (see\n"
            "   RAM_LAYER_CRASH_ROOTCAUSE.md sec.12-13). Returns 0 while no\n"
            "   successful device poll has happened, or when the request fits\n"
            "   in real free memory. Windows only -- see the #else stub below. */\n"
            "ssize_t real_free_fit_deficit(uint64_t size);\n"
        ),
        already_marker="ssize_t real_free_fit_deficit(uint64_t size);",
    )
    # The non-Windows stub rides along on the SAME edit as the #else
    # poll_budget_deficit definition it follows, because that whole definition
    # is a multi-line construct and only apply_edit() (plain substring replace
    # over the whole text, no line splitting) can rewrite it. Splitting this
    # into a second positional edit would need a file-scope anchor, and the only
    # candidate in that branch is "#endif", which also terminates the
    # unmap_workaround block near the top of the file and is therefore
    # ambiguous. Keeping the two #else pieces in one edit avoids that trap
    # entirely and makes the stub's position structural rather than incidental.
    apply_edit(
        "plat.h:real_free_fit_deficit stub (non-Windows)",
        FILES["plat_h"],
        "static inline bool poll_budget_deficit(const char **prevailing_deficit_method) {\n"
        "    return cuda_budget_deficit(prevailing_deficit_method);\n"
        "}",
        "static inline bool poll_budget_deficit(const char **prevailing_deficit_method) {\n"
        "    return cuda_budget_deficit(prevailing_deficit_method);\n"
        "}\n"
        "\n"
        "/* No real free reading is available on this platform (the definition\n"
        " * lives in src-win/shmem-detect.c), so the fit check has no opinion\n"
        " * and contributes nothing to the admission decision. Same shape as\n"
        " * the other non-Windows stubs above. */\n"
        "static inline ssize_t real_free_fit_deficit(uint64_t size) {\n"
        "    (void)size;\n"
        "    return 0;\n"
        "}",
        already_marker="static inline ssize_t real_free_fit_deficit(uint64_t size)",
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
    # NOTE: there is deliberately NO "declare deficit_fit" edit. deficit_fit is
    # introduced by the typed definition emitted above, which is the shape this
    # function already uses for mid-function locals (`ssize_t deficit;`,
    # `uint64_t book_a = ...`). Declaring it in the `deficit_simple,
    # deficit_delta` list AND defining it with a type is a redefinition
    # (MSVC C2086) and was removed -- see commit history for run 37996829467.
    apply_edit(
        "plat.h:budget_deficit log fit term",
        FILES["plat_h"],
        "            deficit_simple > deficit_delta ? \"simple\" : prevailing_deficit_method,",
        "            deficit_fit > MAX(deficit_simple, deficit_delta)\n"
        "                ? \"real-free-fit\"\n"
        "                : (deficit_simple > deficit_delta ? \"simple\" : prevailing_deficit_method),",
        already_marker="\"real-free-fit\"",
    )

    # ---- src-win/hostbuf-plat.c (XPU page-locked host buffer) -------------
    # The RAM layer's HostBuffer reserves its address space through
    # hostbuf_reserve_address_space(). On XPU we make that page-locked,
    # device-visible host USM (zeMemAllocHost via the cuMemAllocHost macro ->
    # g_cuda.p_cuMemAllocHost -> xpu_host_alloc) instead of pageable VirtualAlloc.
    # src-win/ is NOT overlaid by build-xpu-windows.yml, so this can reach the
    # DLL only through this patch script.
    apply_edit(
        "hostbuf-plat.c:XPU pinned host buffer",
        FILES["hostbuf_plat_c"],
        _HB_HOSTBUF_ORIG,
        _HB_HOSTBUF_NEW,
        already_marker="g_hostbuf_pinned",
    )

    verify_injected_shape()

    print("RAM-layer root-cause patch applied successfully.")


def verify_injected_shape():
    """Post-patch self-check on the RESULT, not on the edits.

    Every edit above is checked against an anchor string, so a successful run
    only proves the text was found -- not that the resulting C is valid. That
    distinction is not academic: run 37996829467 got all 13 "[ok]"s and still
    died at compile time with

        plat.h(182): error C2086: 'ssize_t deficit_fit': redefinition
        plat.h(172): note: see declaration of 'deficit_fit'

    because one edit declared the variable in an existing declaration list
    while another defined it with a type. Both anchors matched, so both edits
    reported success. The failure surfaced ~6 minutes later, in a different
    step, on a machine with no way to iterate quickly.

    These checks are cheap and run on the real product of the patch, so a
    malformed shape fails here in under a second with a pointed message
    instead of turning the build red somewhere downstream. Keep them focused
    on failure modes this script can actually introduce.
    """
    plat = read(FILES["plat_h"])
    shmem = read(FILES["shmem_c"])
    hostbuf = read(FILES["hostbuf_plat_c"])

    # 1. No variable may be named twice inside a function when one occurrence
    #    is part of a declaration list and another carries its own type: that
    #    is MSVC C2086.
    for path, src in ((FILES["plat_h"], plat), (FILES["shmem_c"], shmem)):
        for m in re.finditer(r"ssize_t\s+(\w+)\s*,\s*(\w+)\s*(?:,\s*(\w+))?\s*;", src):
            declared = [g for g in m.groups() if g]
            body_start = src.rfind("\n}\n", 0, m.start())
            body_end = src.find("\n}\n", m.end())
            body = src[body_start:body_end if body_end > 0 else len(src)]
            for name in declared:
                if re.search(r"(?<![\w])ssize_t\s+%s\s*=" % re.escape(name), body):
                    sys.exit(
                        "[FAIL] C2086 guard: '%s' is declared in a declaration list "
                        "and then defined with a type in the same function (%s). "
                        "MSVC rejects this at compile time." % (name, path))

    # 2. real_free_fit_deficit must be declared where it is used, defined
    #    exactly once in a compiled translation unit, and carry a matching
    #    signature. A mismatch here is LNK2019 at link time.
    decl = "ssize_t real_free_fit_deficit(uint64_t size);"
    if decl not in plat:
        sys.exit("[FAIL] real_free_fit_deficit is not declared in src/plat.h; "
                 "budget_deficit() would fail to compile.")
    if len(re.findall(r"ssize_t\s+real_free_fit_deficit\s*\(\s*uint64_t\s+size\s*\)\s*\{",
                      shmem)) != 1:
        sys.exit("[FAIL] real_free_fit_deficit must be defined exactly once in "
                 "src-win/shmem-detect.c (the translation unit that is actually "
                 "compiled into the DLL).")

    # 3. The symbol is internal to the DLL. A stray dllexport would add an
    #    export-table entry for a private helper and, because no declaration
    #    carries the matching dllimport, risks a link-time diagnostic.
    if re.search(r"SHARED_EXPORT\s*\n?\s*ssize_t\s+real_free_fit_deficit", shmem):
        sys.exit("[FAIL] real_free_fit_deficit must not be SHARED_EXPORT: it is "
                 "internal to the DLL and no declaration carries a dllimport.")

    # 4. The community's own signal must survive intact. Losing it would
    #    silently change NVIDIA/ROCm behaviour on this shared Windows path.
    if "deficit_cuda" not in shmem:
        sys.exit("[FAIL] deficit_cuda is missing from src-win/shmem-detect.c; the "
                 "vendor headroom signal must be preserved verbatim.")
    if "ssize_t deficit_cuda = headroom - (ssize_t)free_vram;" not in shmem:
        sys.exit("[FAIL] the deficit_cuda expression was altered; the vendor "
                 "headroom term must keep its original meaning and precedence.")

    # 5. F-1: the release macros must be NULL-safe, or an async hostbuf release
    #    after teardown would call through a zeroed (NULL) slot and crash.
    if "g_cuda.p_cuMemFreeHost ? g_cuda.p_cuMemFreeHost(ptr)" not in plat:
        sys.exit("[FAIL] cuMemFreeHost is not NULL-safe in src/plat.h; an async "
                 "release after aimdo_cuda_runtime_cleanup() would crash.")
    if "g_cuda.p_cuMemHostUnregister ? g_cuda.p_cuMemHostUnregister(ptr)" not in plat:
        sys.exit("[FAIL] cuMemHostUnregister is not NULL-safe in src/plat.h.")

    # 6. hostbuf-plat.c must carry the XPU pinned branch AND keep the original
    #    VirtualAlloc path for every other backend.
    if "g_hostbuf_pinned" not in hostbuf or "cuMemAllocHost(&p, size)" not in hostbuf:
        sys.exit("[FAIL] src-win/hostbuf-plat.c is missing the XPU page-locked "
                 "host-buffer branch; the RAM cache would stay pageable.")
    if "VirtualAlloc(NULL, size, MEM_RESERVE, PAGE_NOACCESS)" not in hostbuf:
        sys.exit("[FAIL] src-win/hostbuf-plat.c lost the original VirtualAlloc "
                 "reserve path (non-XPU branch).")

    print("  [ok]   hostbuf-plat.c XPU branch + plat.h NULL-safe release present")
    print("  [ok]   post-patch shape verified (no C2086, symbols consistent, "
          "vendor signal intact)")


if __name__ == "__main__":
    main()
