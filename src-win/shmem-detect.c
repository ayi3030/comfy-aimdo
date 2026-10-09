#include "plat.h"
#include "aimdo-time.h"

#include <windows.h>
#include <dxgi1_4.h>

#if defined(__HIP_PLATFORM_AMD__)
typedef union {
    struct {
        char name[256];
        char uuid[16];
        char luid[8];
    };
    void *ptr;
    unsigned long long ull;
    unsigned char bytes[4096];
} AimdoHipDeviceProp;
#endif

/* True Level-Zero device free bytes from the most recent cuMemGetInfo / NVML
 * poll. Exposed (via extern) to src-xpu/stubs.c so the Windows eviction path
 * can reason about the real physical free instead of the optimistic WDDM
 * budget. Updated only on a successful poll. */
uint64_t last_free_vram = 0;

bool aimdo_wddm_init(CUdevice dev)
{
    int fail_code = 1;
    LUID cuda_luid;
    char adapter_name[256];
    IDXGIFactory4 *factory;
    IDXGIAdapter1 *adapter;
    UINT i;

    factory = NULL;
    adapter = NULL;
    adapter_name[0] = '\0';
    if (g_wddm_adapter) {
        g_wddm_adapter->lpVtbl->Release(g_wddm_adapter);
        g_wddm_adapter = NULL;
    }

#if defined(__HIP_PLATFORM_AMD__)
    AimdoHipDeviceProp hip_props = {0};

    if (!g_device_get_properties ||
        !CHECK_CU(g_device_get_properties(hip_props.bytes, dev))) {
        goto fail;
    }
    memcpy(&cuda_luid, hip_props.luid, sizeof(cuda_luid));
#else
    unsigned int node_mask;
    if (!CHECK_CU(cuDeviceGetLuid((char *)&cuda_luid, &node_mask, dev))) {
        goto fail;
    }
#endif

    fail_code++;

    if (FAILED(CreateDXGIFactory1(&IID_IDXGIFactory4, (void **)&factory))) {
        goto fail;
    }

    for (i = 0; factory->lpVtbl->EnumAdapters1(factory, i, &adapter) != DXGI_ERROR_NOT_FOUND; ++i) {
        DXGI_ADAPTER_DESC1 desc;
        adapter->lpVtbl->GetDesc1(adapter, &desc);

        if (desc.AdapterLuid.LowPart == cuda_luid.LowPart &&
            desc.AdapterLuid.HighPart == cuda_luid.HighPart) {
            if (!WideCharToMultiByte(CP_UTF8, 0, desc.Description, -1, adapter_name,
                                     sizeof(adapter_name), NULL, NULL)) {
                strcpy(adapter_name, "<unknown>");
            }

            if (FAILED(adapter->lpVtbl->QueryInterface(adapter, &IID_IDXGIAdapter3, (void **)&g_wddm_adapter))) {
                adapter->lpVtbl->Release(adapter);
                break;
            }

            log(INFO,
                "comfy-aimdo WDDM adapter match: %s runtime_luid=%08lx:%08lx dxgi_luid=%08lx:%08lx\n",
                adapter_name,
                (unsigned long)(unsigned int)cuda_luid.HighPart,
                (unsigned long)cuda_luid.LowPart,
                (unsigned long)(unsigned int)desc.AdapterLuid.HighPart,
                (unsigned long)desc.AdapterLuid.LowPart);

            adapter->lpVtbl->Release(adapter);
            factory->lpVtbl->Release(factory);
            return true;
        }
        adapter->lpVtbl->Release(adapter);
    }

fail:
    g_wddm_adapter = NULL;
    if (factory) {
        factory->lpVtbl->Release(factory);
    }
    log(WARNING, "comfy-aimdo WDDM init failed (%d). aimdo is blind to the driver sysmem fallback policy\n", fail_code);
    return false;
}

/* Apparently this is still too small for all common graphics VRAM spikes.
 * However we can't pad too much on the smaller cards, and its not the end
 * of the world if we page out a little bit because it will adapt and correct
 * quickly.
 */

/* FIXME: This should be 0 if sysmem fallback is disabled by the user */
#define WDDM_BUDGET_HEADROOM (512 * 1024 * 1024)
#define CUDA_BUDGET_HEADROOM (192 * 1024 * 1024)
#define NVML_BUDGET_HEADROOM (512 * 1024 * 1024)

/* SAFE_FREE_FLOOR — the Windows/XPU admission gate's real-free guard. We deny
 * (deficit_real > 0) ONLY when the TRUE Level-Zero device free drops below this
 * floor. This implicitly covers the system/driver overhead the external ledger
 * (Book A / total_vram_usage) never sees — Level-Zero queue & descriptor pools,
 * the WDDM resident working set, and other reservations the XPU path does not
 * account for (empirically ~940 MB on Arc B580) — by keeping a fixed reserve of
 * device free, rather than adding a pad to Book A.
 *
 * Why a FLOOR instead of (Book A + margin) - free: on a 12 GiB Arc B580 the
 * model pins Book A at ~6500 MB, so free normally sits at 3000-5500 MB. The
 * form (6500 + 1024) - free is structurally positive for any free < ~7.5 GB,
 * i.e. it denies EVERY allocation once the model is loaded and H3 can never
 * run. The floor form denies only when free < 4096 MB; the observed crash fired
 * at free=3221 MB during a transient kernel commit (torch.lerp, model.py:777),
 * which this floor catches (3221 < 4096 -> deficit_real = +1875 -> gate trips
 * -> P1 hard-deny on Windows returns synthetic OOM -> torch frees caches ->
 * retry succeeds). With a healthy free of 5500-8000 MB the deficit is negative
 * and allocation proceeds normally. See RAM_LAYER_CRASH_ROOTCAUSE.md sec.12-13.
 *
 * 4096 MiB is the lead-approved ceiling; do NOT exceed it. A smaller floor
 * (3072/2048 MiB) would NOT catch the 3221 MB crash and is therefore unsafe. */
#define SAFE_FREE_FLOOR (4096ULL * 1024 * 1024)

bool poll_budget_deficit(const char **prevailing_deficit_method)
{
    DXGI_QUERY_VIDEO_MEMORY_INFO info;
    uint64_t effective_budget = vram_capacity;
    size_t free_vram = 0, total_vram = 0;
    bool used_nvml = false;

    uint64_t now = GET_TICK();

    if (now - wddm_timestamp_last_check < 2000) {
        return true;
    }
    wddm_timestamp_last_check = now;
    total_vram_last_check = total_vram_usage;

    if (g_wddm_adapter) {
        if (SUCCEEDED(g_wddm_adapter->lpVtbl->QueryVideoMemoryInfo(g_wddm_adapter, 0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &info))) {
            effective_budget = info.Budget;
            /* Intel Arc / Battlemage default sysmem fallback lets the OS report a
             * DXGI LOCAL Budget larger than the physical VRAM. That "extra" is
             * system RAM, not VRAM, and a device tensor still has to fit in
             * physical VRAM -- so it must NOT be treated as headroom for device
             * allocations. Clamp the VRAM-admission budget at the physical
             * capacity so the admission gate (plat.h:budget_deficit) is never
             * lulled into over-admitting when sysmem fallback inflates the
             * reported Budget.
             *
             * Without this clamp, deficit_sync = (BookA + 512MiB) - Budget becomes
             * a large negative (optimistic) value; MAX() in budget_deficit then
             * selects the physical-capacity path while still trusting Book A
             * (total_vram_usage), which on XPU undercounts torch's real device
             * usage -> torch exceeds physical VRAM -> UR_OUT_OF_RESOURCES(40) ->
             * DEVICE_LOST. See RAM_LAYER_CRASH_ROOTCAUSE.md sec.12-13. */
            if (effective_budget > vram_capacity) {
                log(DEBUG,
                    "%s: sysmem fallback detected (DXGI budget %zu MB > physical VRAM %zu MB); "
                    "clamping VRAM-admission budget to physical capacity\n",
                    __func__, (size_t)(info.Budget / M), (size_t)(vram_capacity / M));
                effective_budget = vram_capacity;
            }
            log(DEBUG,
                "%s: WDDM budget=%zu MB usage=%zu MB reservation=%zu MB available=%zu MB\n",
                __func__, (size_t)(info.Budget / M), (size_t)(info.CurrentUsage / M),
                (size_t)(info.CurrentReservation / M),
                (size_t)(info.AvailableForReservation / M));
        } else {
            log(WARNING, "comfy-aimdo WDDM VRAM query failed. Using physical capacity as fallback\n");
        }
    }

    deficit_sync = (ssize_t)(effective_book_a() + WDDM_BUDGET_HEADROOM) - (ssize_t)effective_budget;
    *prevailing_deficit_method = "WDDM budget";

#if defined(AIMDO_CUDA)
    used_nvml = nvml_device && aimdo_nvml_memory_info(nvml_device, &free_vram, &total_vram);
#endif
    if (used_nvml || CHECK_CU(cuMemGetInfo(&free_vram, &total_vram))) {
        /* Capture the TRUE device free for the Windows eviction path. */
        last_free_vram = free_vram;

        /* Real-free method: a SAFE_FREE_FLOOR guard against the ACTUAL device
         * free (not the optimistic WDDM budget). Deny only when true device
         * free drops below the floor — this is safe against the over-restriction
         * bug (see SAFE_FREE_FLOOR comment above). The most-restrictive (largest)
         * deficit across the WDDM-budget and real-free methods still wins, so the
         * gate trips on whichever signal is tighter. */
        ssize_t deficit_real = (ssize_t)SAFE_FREE_FLOOR - (ssize_t)free_vram;

        log(DEBUG,
            "%s: device memory free=%zu MB total=%zu MB deficit_real=%zd MB\n",
            __func__, free_vram / M, total_vram / M, deficit_real / (ssize_t)M);

        if (deficit_real > deficit_sync) {
            deficit_sync = deficit_real;
            *prevailing_deficit_method = used_nvml ? "NVML real-free" : "cuMemGetInfo real-free";
        }
    }

    log(DEBUG, "%s: prevailing method %s\n", __func__, *prevailing_deficit_method);
    return true;
}

void aimdo_wddm_cleanup()
{
    if (g_wddm_adapter) {
        g_wddm_adapter->lpVtbl->Release(g_wddm_adapter);
        g_wddm_adapter = NULL;
    }
}
