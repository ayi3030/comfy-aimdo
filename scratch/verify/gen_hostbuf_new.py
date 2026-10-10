#!/usr/bin/env python3
"""Rewrite _HB_HOSTBUF_NEW in apply-ram-rootcause-patch.py with the
xpu-pinned-host-fix safe hostbuf design (per-buffer, budget-gated pin; real
release). Deterministic + idempotent at the source level: it replaces the
single assignment line starting with `_HB_HOSTBUF_NEW = `.
"""
import sys

PATCH = ".github/scripts/apply-ram-rootcause-patch.py"

C_BLOCK = r'''#if defined(AIMDO_XPU)
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
#endif'''

NEW_ASSIGNMENT = "_HB_HOSTBUF_NEW = '''\n" + C_BLOCK + "\n'''\n"


def main():
    with open(PATCH, "r", encoding="utf-8") as f:
        lines = f.readlines()
    # Locate and remove the entire existing _HB_HOSTBUF_NEW assignment, whether
    # it is the original single-line escaped string or an already-expanded
    # multi-line ''' block. This makes the rewrite idempotent.
    start = None
    for i, line in enumerate(lines):
        if line.startswith("_HB_HOSTBUF_NEW = "):
            start = i
            break
    if start is None:
        sys.exit("[FAIL] _HB_HOSTBUF_NEW assignment not found")
    end = start
    if "'''" in lines[start]:
        # multi-line block: consume up to and including the matching close.
        for j in range(start + 1, len(lines)):
            end = j
            if lines[j].strip() == "'''":
                break
    # else: single-line assignment, end == start.
    out = lines[:start] + NEW_ASSIGNMENT.splitlines(keepends=True) + lines[end + 1:]
    with open(PATCH, "w", encoding="utf-8") as f:
        f.writelines(out)
    # sanity: the new value must still carry the 6-shape-check anchors
    import re
    val = C_BLOCK
    for anchor in ("g_hostbuf_pinned", "cuMemAllocHost(&p, size)",
                  "VirtualAlloc(NULL, size, MEM_RESERVE, PAGE_NOACCESS)"):
        if anchor not in val:
            sys.exit("[FAIL] anchor missing after rewrite: " + anchor)
    print("[ok] _HB_HOSTBUF_NEW rewritten; anchors present")


if __name__ == "__main__":
    main()
