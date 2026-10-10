#pragma once

#include "gpu_dispatch.h"

/* NOTE: cuda_runtime.h is banned here. Always use the driver APIs.
 * Keep SDK headers out of this project and add any required duck-types
 * to the repo-owned ABI headers instead.
 */

typedef int cudaError_t;
typedef struct CUstream_st *cudaStream_t;

#if defined(__HIP_PLATFORM_AMD__) && !defined(_WIN32) && !defined(_WIN64)
#include <sys/mman.h>
/* Work around ROCm VMM unmap behavior by reprotecting the range after unmap.
 * On systems where this is fixed, remapping PROT_NONE is harmless.
 */
#define unmap_workaround(va, size) \
    mmap((void *)(va), (size), PROT_NONE, \
         MAP_PRIVATE | MAP_FIXED | MAP_NORESERVE | MAP_ANONYMOUS, -1, 0)
#else
#define unmap_workaround(va, size)
#endif

#include <string.h>
#include <stdio.h>
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <assert.h>

/* control.c */
bool cuda_budget_deficit(const char **prevailing_deficit_method);

/* 导出宏：Windows 需 dllexport；XPU 后端同样要导出 alloc_fn/free_fn，故不随平台分支裁剪。 */
#if defined(_WIN32) || defined(_WIN64)
#define SHARED_EXPORT __declspec(dllexport)
#include <BaseTsd.h>
typedef SSIZE_T ssize_t;
#else
#define SHARED_EXPORT
#endif

#if (defined(_WIN32) || defined(_WIN64)) && !defined(AIMDO_XPU)
/* shmem-detect.c */
bool aimdo_wddm_init(gpu_device_t dev);
void aimdo_wddm_cleanup();
bool poll_budget_deficit(const char **prevailing_deficit_method);
/* cuda-detour.c */
bool aimdo_setup_hooks();
void aimdo_teardown_hooks();
#else
/* 非 Windows 或 XPU：WDDM/NVML 检测不适用，使用内联空实现。 */
static inline bool aimdo_wddm_init(gpu_device_t dev) { return true; }
static inline void aimdo_wddm_cleanup() {}
bool aimdo_setup_hooks(void);
void aimdo_teardown_hooks(void);

static inline bool poll_budget_deficit(const char **prevailing_deficit_method) {
    return cuda_budget_deficit(prevailing_deficit_method);
}
#endif

#if defined(AIMDO_XPU)
/* XPU 后端不 hook CUDA 分配器：改由 Python 侧通过 PyTorch XPUPluggableAllocator
 * 注册本扩展导出的 alloc_fn/free_fn，因此 hook 入口为空实现。 */
#define aimdo_setup_hooks()    (true)
#define aimdo_teardown_hooks() ((void)0)
#endif

/* module-load.c */
void *aimdo_find_loaded_module(const char *const *libraries, size_t library_count);

#include "control.h"

#if defined(AIMDO_XPU)
/* XPU 后端：宏改道到后端无关中性分发 g_gpu（由 src-xpu/dispatch.c 在运行时填充）。 */
#define cuInit                      g_gpu.p_init
#define cuGetErrorString            g_gpu.p_get_error_string
#define cuCtxGetDevice              g_gpu.p_ctx_get_device
#define cuCtxSynchronize            g_gpu.p_ctx_synchronize
#define cuDeviceGet                 g_gpu.p_device_get
#define cuDeviceGetAttribute        g_gpu.p_device_get_attribute
#define cuDeviceTotalMem            g_gpu.p_device_total_mem
#define cuDeviceGetName             g_gpu.p_device_get_name
#define cuMemGetInfo                g_gpu.p_mem_get_info
#define cuMemAllocHost              g_gpu.p_mem_alloc_host
#define cuMemFreeHost               g_gpu.p_mem_free_host
#define cuMemHostRegister           g_gpu.p_mem_host_register
#define cuMemHostUnregister         g_gpu.p_mem_host_unregister
#define cuMemAddressReserve         g_gpu.p_mem_address_reserve
#define cuMemAddressFree            g_gpu.p_mem_address_free
#define cuMemCreate                 g_gpu.p_mem_create
#define cuMemMap                    g_gpu.p_mem_map
#define cuMemSetAccess              g_gpu.p_mem_set_access
#define cuMemUnmap                  g_gpu.p_mem_unmap
#define cuMemRelease                g_gpu.p_mem_release
#define cuMemcpyHtoDAsync           g_gpu.p_memcpy_htod_async
#define cuEventCreate               g_gpu.p_event_create
#define cuEventDestroy              g_gpu.p_event_destroy
#define cuEventRecord               g_gpu.p_event_record
#define cuEventSynchronize          g_gpu.p_event_synchronize
#define cuDeviceGetLuid             g_gpu.p_device_get_luid
#else
#define cuInit                      g_cuda.p_cuInit
#define cuGetErrorString            g_cuda.p_cuGetErrorString
#define cuCtxGetDevice              g_cuda.p_cuCtxGetDevice
#define cuCtxSynchronize            g_cuda.p_cuCtxSynchronize
#define cuDeviceGet                 g_cuda.p_cuDeviceGet
#define cuDeviceGetAttribute        g_cuda.p_cuDeviceGetAttribute
#define cuDeviceTotalMem            g_cuda.p_cuDeviceTotalMem
#define cuDeviceGetName             g_cuda.p_cuDeviceGetName
#define cuMemGetInfo                g_cuda.p_cuMemGetInfo
#define cuMemAllocHost              g_cuda.p_cuMemAllocHost
#define cuMemFreeHost               g_cuda.p_cuMemFreeHost
#define cuMemHostRegister           g_cuda.p_cuMemHostRegister
#define cuMemHostUnregister         g_cuda.p_cuMemHostUnregister
#define cuMemAddressReserve         g_cuda.p_cuMemAddressReserve
#define cuMemAddressFree            g_cuda.p_cuMemAddressFree
#define cuMemCreate                 g_cuda.p_cuMemCreate
#define cuMemMap                    g_cuda.p_cuMemMap
#define cuMemSetAccess              g_cuda.p_cuMemSetAccess
#define cuMemUnmap                  g_cuda.p_cuMemUnmap
#define cuMemRelease                g_cuda.p_cuMemRelease
#define cuMemcpyHtoDAsync           g_cuda.p_cuMemcpyHtoDAsync
#define cuEventCreate               g_cuda.p_cuEventCreate
#define cuEventDestroy              g_cuda.p_cuEventDestroy
#define cuEventRecord               g_cuda.p_cuEventRecord
#define cuEventSynchronize          g_cuda.p_cuEventSynchronize
#define cuDeviceGetLuid             g_cuda.p_cuDeviceGetLuid
#endif

#define MAX(a, b) (((a) > (b)) ? (a) : (b))
#define MIN(a, b) (((a) < (b)) ? (a) : (b))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))

/* NOTE: align_to must be power of 2 */
#define ALIGN_UP(x, align_to) (((x) + (align_to) - 1) & ~((align_to) - 1))

#define CUDA_PAGE_SIZE   (2 << 20)
#define CUDA_ALIGN_UP(s) ALIGN_UP(s, CUDA_PAGE_SIZE)

typedef unsigned long long ull;
#define K 1024
#define M (K * K)
#define G (M * K)

enum DebugLevels {
    __NONE__ = -1,
    /* Default to everything so if python itegration is hosed, we see prints. */
    ALL = 0,
    CRITICAL,
    AIMDO_LOG_ERROR,
    WARNING,
    INFO,
    DEBUG,
    VERBOSE,
    VVERBOSE,
};

/* debug.c */
extern int log_level;
extern uint64_t log_shot_counter;
const char *get_level_str(int level);
void log_reset_shots();
void aimdo_log(int level, const char *file, int line, const char *format, ...);

#define do_log(do_shot_counter, level, ...) {                                                   \
    static _Thread_local uint64_t _sc_;                                                         \
    if ((!log_level || log_level >= (level)) && _sc_ < log_shot_counter) {                      \
        _sc_ = (do_shot_counter) ? log_shot_counter : 0;                                        \
        aimdo_log((level), __FILE__, __LINE__, __VA_ARGS__);                                    \
    }                                                                                           \
}

#define log(level, ...) do_log(false, level, __VA_ARGS__)
#define log_shot(level, ...) do_log(true, level, __VA_ARGS__)

/* The default VRAM headroom. Different deficit methods with BYO headroom */
#define VRAM_HEADROOM (256 * 1024 * 1024)
extern int64_t simple_vram_headroom;

static inline ssize_t budget_deficit(size_t size) {
    ssize_t deficit_simple, deficit_delta;
    ssize_t deficit;
    const char *prevailing_deficit_method = "unknown";

    poll_budget_deficit(&prevailing_deficit_method);
    deficit_simple = (ssize_t)(total_vram_usage + size) + (ssize_t)simple_vram_headroom -
                     (ssize_t)vram_capacity;
    deficit_delta = deficit_sync + (ssize_t)total_vram_usage -
                    (ssize_t)total_vram_last_check + (ssize_t)size;
    deficit = MAX(deficit_simple, deficit_delta) + (ssize_t)extra_vram_headroom;
    if (deficit > 0) {
        log(DEBUG, "%s: Prevailing Method: %s Deficit: %zu Extra Headroom: %zu Alloc Size %zu\n", __func__,
            deficit_simple > deficit_delta ? "simple" : prevailing_deficit_method,
            (size_t)deficit / M, (size_t)extra_vram_headroom / M, size / M);
    }
    return deficit;
}

static inline int check_cu_impl(gpu_result_t res, const char *label, int oom_level, int error_level) {
    if (res != GPU_SUCCESS) {
        const char* desc;
        if (cuGetErrorString(res, &desc) != GPU_SUCCESS) {
            desc = "<FATAL - CANNOT PARSE GPU ERROR CODE>";

        }
        log(res == GPU_ERROR_OUT_OF_MEMORY ? oom_level : error_level,
            "GPU API FAILED (%d): %s: %s\n", (int)res, label, desc);
    }
    return (res == GPU_SUCCESS);
}
#define CHECK_CU(x) check_cu_impl((x), #x, VVERBOSE, DEBUG)
#define CHECK_CU_OOM_ERROR(x) check_cu_impl((x), #x, AIMDO_LOG_ERROR, DEBUG)
#define CHECK_CU_ERROR(x) check_cu_impl((x), #x, VVERBOSE, AIMDO_LOG_ERROR)

static inline gpu_result_t three_stooges(gpu_deviceptr_t vaddr, size_t size, int device,
                                     gpu_mem_handle_t *handle) {
    gpu_mem_handle_t h = 0;
    gpu_result_t err;

    gpu_mem_prop_t prop = {
        .type = GPU_MEM_ALLOCATION_TYPE_PINNED,
        .location.type = GPU_MEM_LOCATION_TYPE_DEVICE,
        .location.id = device,
    };

    gpu_mem_access_desc_t accessDesc = {
        .location.type = GPU_MEM_LOCATION_TYPE_DEVICE,
        .location.id = device,
        .flags = GPU_MEM_ACCESS_FLAGS_PROT_READWRITE,
    };

    if (!CHECK_CU_ERROR(err = cuMemCreate(&h, size, &prop, 0))) {
        goto fail;
    }
    if (!CHECK_CU_ERROR(err = cuMemMap(vaddr, size, 0, h, 0))) {
        goto fail_mmap;
    }
    if (!CHECK_CU_ERROR(err = cuMemSetAccess(vaddr, size, &accessDesc, 1))) {
        goto fail_access;
    }
    total_vram_usage += size;

    *handle = h;
    return GPU_SUCCESS;

fail_access:
    CHECK_CU_ERROR(cuMemUnmap(vaddr, size));
    unmap_workaround(vaddr, size);
fail_mmap:
    CHECK_CU_ERROR(cuMemRelease(h));
fail:
    return err;
}

/* vrambuf.c */
#if defined(__HIP_PLATFORM_AMD__) && defined(_WIN32)
bool va_pool_init(void);
void va_pool_cleanup(void);
#else
static inline bool va_pool_init(void) { return true; }
static inline void va_pool_cleanup(void) {}
#endif

/* model_vbar.c */
size_t vbars_free(ssize_t size);
SHARED_EXPORT
uint64_t vbars_analyze(void *devctx, bool only_dirty);

/* pyt-cu-alloc.c（CUDA 专属，XPU 构建下不定义，但这些声明保持中性类型以便一致） */
int aimdo_cuda_malloc(gpu_deviceptr_t *dptr, size_t size,
                      gpu_result_t (*true_cuMemAlloc_v2)(gpu_deviceptr_t*, size_t));
int aimdo_cuda_free(gpu_deviceptr_t dptr,
                    gpu_result_t (*true_cuMemFree_v2)(gpu_deviceptr_t));

int aimdo_cuda_malloc_async(gpu_deviceptr_t *devPtr, size_t size, gpu_stream_t hStream,
                            gpu_result_t (*true_cuMemAllocAsync)(gpu_deviceptr_t*, size_t, gpu_stream_t));
int aimdo_cuda_free_async(gpu_deviceptr_t devPtr, gpu_stream_t hStream,
                          gpu_result_t (*true_cuMemFreeAsync)(gpu_deviceptr_t, gpu_stream_t));

bool malloc_graph_alloc(gpu_deviceptr_t *ptr, size_t size, gpu_stream_t stream);
bool malloc_graph_free(gpu_deviceptr_t ptr, gpu_stream_t stream, int *result);
bool malloc_graph_sync_paused(void);

bool allocations_init(void);
void allocations_cleanup(void);
void allocations_lock(void);
void allocations_unlock(void);
void allocations_analyze(bool only_dirty);
SHARED_EXPORT
void aimdo_analyze(void *devctx);
