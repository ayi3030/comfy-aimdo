#include "plat.h"
#include <stdlib.h>

/* True device free / total captured by poll_budget_deficit() in the compiled
 * shmem-detect translation unit, plus the shared size-aware fit check built on
 * them.  All three live in the DLL's real translation unit, so the admission
 * decision here and the one in budget_deficit() cannot drift apart.
 * Read-only from this side; never written here. */
extern uint64_t last_free_vram;
extern uint64_t last_total_vram;
extern ssize_t real_free_fit_deficit(uint64_t size);

/* Fresh (unthrottled) real-free read; defined in src-xpu/dispatch.cpp, same
 * translation unit set as this DLL. Used only to re-check a would-be denial
 * against the CURRENT device free instead of the sampled last_free_vram. */
extern size_t aimdo_xpu_fresh_free_bytes(void);

/* The XPU allocator does not record or replay allocation graphs. Keep the
 * shared allocator/VBAR call sites on their ordinary allocation and
 * synchronization paths without linking the CUDA/HIP memory compiler. */
bool malloc_graph_alloc(CUdeviceptr *ptr, size_t size, CUstream stream) {
    return false;
}

bool malloc_graph_free(CUdeviceptr ptr, CUstream stream, int *result) {
    return false;
}

bool malloc_graph_sync_paused(void) {
    return false;
}

bool free_rogue(CUdeviceptr ptr, int *result) {
    return false;
}

#if !defined(_WIN32) && !defined(_WIN64)
bool aimdo_setup_hooks(void) {
    log(DEBUG, "%s: XPU keeps the native Torch allocator; no allocator hooks installed\n",
        __func__);
    return true;
}

void aimdo_teardown_hooks(void) {
}
#endif

int aimdo_xpu_current_device(void) {
    return g_devctx ? g_devctx->_device_id : -1;
}

uint64_t aimdo_xpu_recorded_usage(void) {
    return g_devctx ? g_devctx->_total_vram_usage : 0;
}

/* 诊断读回：g_devctx->_vram_capacity，即 init 时 cuDeviceTotalMem 写入的
 * 物理显存上限（Arc B580 约 11875MB）。budget_deficit() 用它算预算赤字，
 * vbar_allocate 在非 XPU 路径也用它钳制 VBAR 页数，所以「运行时到底是多少」
 * 决定了 Book A 的合理上界——排查驻留超限时必须能直接读到真值。
 *
 * 之所以放在 src-xpu/ 而不是 src/control.c：XPU 构建只 overlay src-xpu/，
 * src/ 来自社区 fork，在 src/control.c 里加导出不会进入 DLL。 */
uint64_t aimdo_xpu_vram_capacity(void) {
    return g_devctx ? g_devctx->_vram_capacity : 0;
}

bool aimdo_xpu_sample_pressure(int device, size_t size) {
    if (!set_devctx_for_device(device)) {
        return false;
    }
#if defined(_WIN32) || defined(_WIN64)
    const char *deficit_method = "unknown";

    /* Read-only. Safe to call while the driver is servicing an allocation,
     * unlike reclaim, which mutates Level Zero physical memory. */
    if (size >= (size_t)1 << 30) {
        aimdo_wddm_force_poll();
    }
    poll_budget_deficit(&deficit_method);
#else
    (void)size;
#endif
    return true;
}

bool aimdo_xpu_prepare_allocation(int device, size_t size) {
    if (!set_devctx_for_device(device)) {
        return false;
    }
#if defined(_WIN32) || defined(_WIN64)
    ssize_t deficit;

    /* This can run under PyTorch's allocator/UMF locks.  Re-entering Level
     * Zero virtual-memory management here is enough to destabilize WDDM even
     * though the lower driver allocation call has returned.  Record pressure
     * only; the next VBAR/model-owner boundary performs the actual unmap. */
    deficit = budget_deficit(size);
    log(VVERBOSE, "%s: device=%d size=%zuk recorded=%zuk deficit=%zdk\n", __func__,
        device, size / K, (size_t)total_vram_usage / K, deficit / (ssize_t)K);
    if (deficit > 0) {
        vbars_request_reclaim(deficit);
    }
#else
    vbars_free(budget_deficit(size));
#endif
    return true;
}

bool aimdo_xpu_retry_allocation(int device, size_t size) {
    if (!set_devctx_for_device(device)) {
        return false;
    }
#if defined(_WIN32) || defined(_WIN64)
    /* Even the retry is still inside the allocator's call stack.  Force a
     * fresh pressure sample, but leave VBAR mutation to its owner. */
    aimdo_wddm_force_poll();
    vbars_request_reclaim((ssize_t)size);
#else
    vbars_free((ssize_t)size);
#endif
    return true;
}

bool aimdo_xpu_allocation_deficit(int device, size_t size, int64_t *deficit) {
    if (!deficit || !set_devctx_for_device(device)) {
        return false;
    }
    *deficit = (int64_t)budget_deficit(size);
    return true;
}

bool aimdo_xpu_evict_for_allocation(int device, int64_t deficit) {
    if (!set_devctx_for_device(device)) {
        return false;
    }
    if (deficit > 0) {
#if defined(_WIN32) || defined(_WIN64)
        /* The UR hook is above the driver call but remains inside the native
         * allocation stack.  Record the shortage, best-effort reclaim VBAR
         * pages, and let the caller decide.
         *
         * Denial policy (corrected).  An earlier revision returned false here
         * for ANY deficit > 0, so every pressured allocation was refused.  That
         * is only safe for the one caller that can actually recover from a
         * refusal: PyTorch, which responds by dropping its cache and retrying.
         * The detour already routes that case (torch-native request with a
         * non-empty cache) into its own arm_retry()/synthetic-OOM branch BEFORE
         * reaching us, and it deliberately does not consult this return value
         * (see ur-usm-detour.c:568).  So the unconditional false could only
         * ever fire on the paths with NO retry available -- a non-torch
         * allocation, or a torch request with nothing cached to surrender --
         * turning a recoverable pressure signal into a hard failure that
         * propagated out of the allocator.
         *
         * Deny only when the request genuinely does not fit in the real free
         * memory that was just polled.  That is the same size-aware,
         * capacity-derived test budget_deficit() already applies, so this
         * function no longer needs its own private threshold and cannot drift
         * from the gate's; and a merely tight-but-satisfiable device still
         * proceeds to the driver exactly as it does on CUDA/ROCm, where
         * vbars_free() is called and the request is admitted.
         *
         * Both are best-effort and safe to call under the allocator locks:
         * vbars_request_reclaim only records a target for the next
         * owner-side VBAR boundary, unlike vbars_free() which unmaps
         * Level-Zero physical memory right here. */
        int64_t shortfall = (int64_t)real_free_fit_deficit((uint64_t)(deficit < 0 ? 0 : deficit));
        if (shortfall > 0) {
            /* The sampled last_free_vram can catch a momentary dip: during
             * non-load phases the device free swings by gigabytes within a
             * second (observed 5531 -> 164 -> 9108 MB), so one low sample can
             * refuse a small request that actually fits. Re-read the device
             * before refusing and deny only when the FRESH free also cannot
             * satisfy the request under the SAME capacity-derived reserve the
             * sampled check uses. A genuine persistent shortfall is unchanged
             * (fresh is also low, or the request is large); only the
             * transient-dip false denial is removed, matching the
             * "tight-but-satisfiable still proceeds to the driver" intent
             * stated above. Recomputed here rather than reusing the sampler's
             * value so the two decisions cannot drift. */
            uint64_t cap = last_total_vram;
            uint64_t reserve = cap / 16;
            uint64_t fresh = (uint64_t)aimdo_xpu_fresh_free_bytes();
            uint64_t need = (uint64_t)(deficit < 0 ? 0 : deficit);
            uint64_t avail;
            if (reserve > (512ULL * 1024 * 1024)) {
                reserve = 512ULL * 1024 * 1024;
            }
            avail = (fresh > reserve) ? (fresh - reserve) : 0;
            log(DEBUG,
                "%s: sampled free=%zu MB -> deny; fresh free=%zu MB -> %s\n",
                __func__, (size_t)last_free_vram / M, (size_t)fresh / M,
                (need <= avail) ? "admitting (stale/transient sample)"
                                : "denying (real shortfall)");
            if (fresh != 0 && need <= avail) {
                shortfall = 0;
            }
        } else {
            log(DEBUG,
                "%s: Windows shortage=%zd bytes; true device free=%zu MB total=%zu MB; %s\n",
                __func__, (ssize_t)deficit, (size_t)last_free_vram / M,
                (size_t)last_total_vram / M, "reclaim only, admitting");
        }
        vbars_request_reclaim((ssize_t)deficit);
        return shortfall <= 0;
#else
        vbars_free((ssize_t)deficit);
#endif
    }
    return true;
}

bool aimdo_xpu_account_allocation(int device, int64_t delta) {
    if (!set_devctx_for_device(device)) {
        return false;
    }
    if (delta >= 0) {
        total_vram_usage += (uint64_t)delta;
    } else {
        uint64_t released = (uint64_t)(-delta);
        total_vram_usage = released < total_vram_usage
            ? total_vram_usage - released
            : 0;
    }
    /* Book A over-count localization probe (default OFF, revertible).
     * Logs the authoritative ledger (total_vram_usage, "Book A") against the
     * sampled WDDM device usage at every torch-USM accounting step, so a field
     * run can see WHERE the recorded-vs-WDDM gap grows. Book A exceeding WDDM
     * usage is impossible if Book A is a subset of the device footprint (the
     * design intent, plat.h:171-193), so a growing gap is a native ledger bug.
     * Uses the SAMPLED last_free_vram/last_total_vram (cheap globals refreshed
     * by poll_budget_deficit), never the fresh DXGI traversal, to avoid
     * disturbing the allocator hot path. Gate: AIMDO_XPU_LEDGER_TRACE.
     * Read the gap branch: free with book_a NOT dropping (WDDM drops) => a free
     * path skipped its decrement; alloc with book_a rising more than WDDM =>
     * double-count / phantom; torch-USM balanced yet gap grows => VBAR/VMM/
     * vrambuf (instrument those src/ paths next, via the patch script). */
    if (getenv("AIMDO_XPU_LEDGER_TRACE") != NULL) {
        int64_t wddm_usage = (int64_t)last_total_vram - (int64_t)last_free_vram;
        log(DEBUG,
            "LEDGER acct: device=%d delta=%lldMB book_a=%lluMB wddm_free=%lluMB "
            "wddm_usage=%lldMB gap=%lldMB\n",
            device, (long long)delta / (long long)M,
            (unsigned long long)total_vram_usage / (unsigned long long)M,
            (unsigned long long)last_free_vram / (unsigned long long)M,
            (long long)wddm_usage / (long long)M,
            ((int64_t)total_vram_usage - wddm_usage) / (long long)M);
    }
    return true;
}
