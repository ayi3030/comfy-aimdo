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

/* Real-free admission guard: a SIZE-AWARE FIT CHECK with a capacity-derived
 * reserve. An allocation is refused only when it does not fit in the real free
 * memory (last_free_vram) minus this reserve. See real_free_fit_deficit() below
 * and RAM_LAYER_CRASH_ROOTCAUSE.md sec.12-13.
 *
 * Why NOT a fixed free-memory floor. An earlier revision denied whenever free
 * fell below a hardcoded 4096 MiB. That constant was only meaningful for one
 * card: far too strict on a 24 GB board, too lax on a 6 GB one. Worse, it
 * cannot work at all on B580 -- the observed DEVICE_LOST fired at free=3221 MB
 * while the same log series recorded a NORMAL healthy band of 3000-5500 MB, so
 * 3221 sits INSIDE that band. No threshold separates healthy from about-to-crash;
 * retuning the constant (3072/2048) was never going to be sound, and any value
 * low enough to catch the crash also fires on healthy steady-state allocations.
 * That is what produced the nondeterministic, card-dependent behaviour.
 *
 * Why a fit check instead. Whether a request can be served depends on its SIZE
 * relative to what is actually free, not on free crossing a magic line. The
 * check is therefore size-aware, and the reserve is expressed as a capped share
 * OF CAPACITY (capacity/16, capped at 512 MiB) so one rule holds from a 4 GB
 * laptop dGPU to a 96 GB board, on integrated GPUs, and on the XPU path, with no
 * per-vendor or per-SKU constant anywhere. The reserve covers the driver-side
 * pools and a transient kernel's working set that no ledger sees.
 *
 * The vendor headroom term (deficit_cuda) below is deliberately left intact so
 * NVIDIA/ROCm behaviour on this shared Windows path is unchanged; the fit check
 * is an additional, separate term MAXed in by plat.h:budget_deficit, and can
 * only ever tighten the gate for a request that genuinely does not fit. */
#define REAL_FREE_RESERVE_DEN  16
#define REAL_FREE_RESERVE_CAP  (512ULL * 1024 * 1024)

uint64_t last_free_vram = 0;
uint64_t last_total_vram = 0;

/* Deliberately NOT SHARED_EXPORT: this is internal to the DLL and
 * nothing outside links against it. Keeping it out of the export
 * table avoids spending an export slot on a private helper.
 *
 * It MUST keep external linkage (no `static`): src-xpu/stubs.c calls it via
 * an `extern` declaration, and both translation units are linked into the same
 * DLL. Marking it static would break that call with a link error. The plat.h
 * declaration is Windows-only; the #else branch there supplies a stub for
 * platforms that never compile this file. */
ssize_t real_free_fit_deficit(uint64_t size) {
    uint64_t capacity, reserve, available;
    if (last_free_vram == 0) {
        return 0;
    }
    capacity = last_total_vram ? last_total_vram : vram_capacity;
    reserve = capacity / REAL_FREE_RESERVE_DEN;
    if (reserve > REAL_FREE_RESERVE_CAP) {
        reserve = REAL_FREE_RESERVE_CAP;
    }
    available = (last_free_vram > reserve) ? (last_free_vram - reserve) : 0;
    if (size <= available) {
        return 0;
    }
    return (ssize_t)(size - available);
}

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
        /* The vendor headroom term keeps its original meaning and precedence;
         * record the true free/total alongside it so the capacity-derived
         * size-aware fit check (real_free_fit_deficit) and the Windows
         * eviction path can both see the real physical state. */
        ssize_t headroom = used_nvml ? NVML_BUDGET_HEADROOM : CUDA_BUDGET_HEADROOM / 2;
        ssize_t deficit_cuda = headroom - (ssize_t)free_vram;
        last_free_vram = free_vram;
        last_total_vram = total_vram;

        log(DEBUG,
            "%s: device memory free=%zu MB total=%zu MB deficit_cuda=%zd MB\n",
            __func__, free_vram / M, total_vram / M, deficit_cuda / (ssize_t)M);

        if (deficit_cuda > deficit_sync) {
            deficit_sync = deficit_cuda;
            *prevailing_deficit_method = used_nvml ? "NVML (Windows)" : "cuMemGetInfo (Windows)";
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
