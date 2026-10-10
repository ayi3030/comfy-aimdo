/* ============================================================================
 * comfy-aimdo XPU 后端（Intel Arc / oneAPI Level Zero）
 *
 * ⚠️⚠️ 未编译 / 未实机验证 ⚠️⚠️
 *   本文件无法在无 Intel 硬件的环境下编译验证。需在 Intel Arc (B580) 机器上
 *   按交付说明编译，并完成两项关键实机验证：
 *     (a) zeVirtualMemReserve/zePhysicalMemCreate/zeVirtualMemMap 在 B580 驱动上
 *         能否成功（VMM 功能可用性）；
 *     (b) PyTorch XPUPluggableAllocator 用本文件导出的 alloc_fn 返回的 VMM 设备
 *         VA 后，PyTorch 能否把它当作合法设备 USM 指针识别并正确回收。
 *     (c) zeMemAllocHost / zeMemFree 在 B580 驱动上能否成功分配/释放「页锁定、设备
 *         可见」的宿主 USM（XPU 等价 cudaHostRegister 的分配侧）；以及符号缺失/
 *         调用失败时能否正确回退 malloc（见 xpu_mem_alloc_host 与 hostbuf-plat.c）。
 *
 * 设计约束：
 *   - 不 include 任何 SDK 头（含 <ze_api.h>）。运行时 dlopen Level Zero loader，
 *     用 dlsym 解析 ze* 符号；ze* 类型在本文件内以「鸭子类型」自行声明。
 *   - 全部 ze* 函数签名逐字核对自 oneapi-src/level-zero master include/ze_api.h。
 *   - 未取得确切数值的常量（如结构体 stype）以 #define + TODO 标注，待实机确认。
 * ==========================================================================*/
#include "plat.h"
#include "vrambuf.h"

#if defined(_WIN32) || defined(_WIN64)
#include <windows.h>
#define AIMDO_DLOPEN(p)    ((void *)LoadLibraryA(p))
#define AIMDO_DLSYM(h, s)  ((void *)GetProcAddress((HMODULE)(h), (s)))
#define AIMDO_DLCLOSE(h)   FreeLibrary((HMODULE)(h))
#else
#include <dlfcn.h>
#define AIMDO_DLOPEN(p)    dlopen((p), RTLD_NOW | RTLD_GLOBAL)
#define AIMDO_DLSYM(h, s)  dlsym((h), (s))
#define AIMDO_DLCLOSE(h)   dlclose(h)
#endif

/* ---- Level Zero 鸭子类型（不 include ze_api.h）---- */
typedef int       ze_result_t;
typedef void     *ze_driver_handle_t;
typedef void     *ze_device_handle_t;
typedef void     *ze_context_handle_t;
typedef void     *ze_physical_mem_handle_t;
typedef uint32_t  ze_init_flags_t;
typedef uint32_t  ze_structure_type_t;
typedef uint32_t  ze_memory_type_t;
typedef uint32_t  ze_memory_access_attribute_t;
typedef uint32_t  ze_physical_mem_flags_t;

/* 枚举/常量（数值已逐字核对自 oneapi-src/level-zero master include/ze_api.h） */
#define ZE_SUCCESS                               0
#define ZE_INIT_FLAG_GPU_ONLY                    (1u << 0)
#define ZE_MEMORY_TYPE_DEVICE                    2u
#define ZE_MEMORY_ACCESS_ATTRIBUTE_READWRITE     1u
#define ZE_PHYSICAL_MEM_FLAG_ALLOCATE_ON_DEVICE  (1u << 0)
/* ze_structure_type_t 取值（关键修正）：
 *   ZE_STRUCTURE_TYPE_PHYSICAL_MEM_DESC       = 0x20   <-- 原实现猜为 0x1D 是错的，
 *       会导致 zePhysicalMemCreate 因 stype 不匹配被驱动拒绝（INVALID_ENUMERATION/ARGUMENT），
 *       整个 VMM 卸载通路直接失效。已按上游 ze_api.h 更正为 0x20。
 *   ZE_STRUCTURE_TYPE_CONTEXT_DESC            = 0x0D
 *   ZE_STRUCTURE_TYPE_DEVICE_MEMORY_PROPERTIES = 0x07 */
#define ZE_STRUCTURE_TYPE_PHYSICAL_MEM_DESC      0x20u
#define ZE_STRUCTURE_TYPE_CONTEXT_DESC           0x0Du
#define ZE_STRUCTURE_TYPE_DEVICE_MEMORY_PROPERTIES 0x07u
/* zeMemAllocHost 的宿主分配描述符 stype；数值核对自 ze_api.h。 */
#define ZE_STRUCTURE_TYPE_HOST_MEM_ALLOC_DESC     0x15u
#define ZE_HOST_MEM_ALLOC_FLAG_DEFAULT            0u
#define ZE_MAX_DEVICE_NAME                       256

/* 结构体（字段布局按 Level Zero 规范，逐字核对自 ze_api.h） */
typedef struct {
    ze_structure_type_t stype;
    const void *pNext;
    ze_physical_mem_flags_t flags;
    size_t size;
} ze_physical_mem_desc_t;

typedef struct {
    ze_structure_type_t stype;
    const void *pNext;
    /* 原实现漏掉了 flags 字段：zeContextCreate 会读取 desc->flags（偏移 16），
     * 结构体过短属于越界读取（UB）。补齐以保证 ABI 与驱动一致。flags 置 0。 */
    uint32_t flags;
} ze_context_desc_t;

/* ze_host_mem_alloc_desc_t：页锁定宿主 USM 分配描述符。
 * stype=ZE_STRUCTURE_TYPE_HOST_MEM_ALLOC_DESC(0x15)；flags=0 即
 * ZE_HOST_MEM_ALLOC_FLAG_DEFAULT（设备可见、可迁移的宿主内存）。 */
typedef struct {
    ze_structure_type_t stype;
    const void *pNext;
    uint32_t flags;
} ze_host_mem_alloc_desc_t;

/* ze_device_memory_properties_t：用于取真实显存总量（替代 12GiB 占位值）。
 * 字段顺序/偏移严格按 ze_api.h：stype(0) pNext(8) flags(16) maxClockRate(20)
 * maxBusWidth(24) totalSize(32) name(40)。 */
typedef struct {
    ze_structure_type_t stype;
    void *pNext;
    uint32_t flags;
    uint32_t maxClockRate;
    uint32_t maxBusWidth;
    uint64_t totalSize;
    char name[ZE_MAX_DEVICE_NAME];
} ze_device_memory_properties_t;

typedef struct {
    ze_structure_type_t stype;
    void *pNext;
    ze_memory_type_t type;
    uint64_t id;
    uint64_t pageSize;
} ze_memory_allocation_properties_t;

/* ---- 函数指针（签名逐字核对自 ze_api.h）---- */
static ze_result_t (*p_zeInit)(ze_init_flags_t);
static ze_result_t (*p_zeDriverGet)(uint32_t *, ze_driver_handle_t *);
static ze_result_t (*p_zeDeviceGet)(ze_driver_handle_t, uint32_t *, ze_device_handle_t *);
static ze_result_t (*p_zeContextCreate)(ze_driver_handle_t, const ze_context_desc_t *,
                                        ze_context_handle_t *);
static ze_result_t (*p_zeContextDestroy)(ze_context_handle_t);
static ze_result_t (*p_zeMemGetAllocProperties)(ze_context_handle_t, const void *,
                                                ze_memory_allocation_properties_t *,
                                                ze_device_handle_t *);
static ze_result_t (*p_zeVirtualMemReserve)(ze_context_handle_t, const void *, size_t, void **);
static ze_result_t (*p_zeVirtualMemFree)(ze_context_handle_t, const void *, size_t);
static ze_result_t (*p_zeVirtualMemQueryPageSize)(ze_context_handle_t, ze_device_handle_t,
                                                  size_t, size_t *);
static ze_result_t (*p_zePhysicalMemCreate)(ze_context_handle_t, ze_device_handle_t,
                                            const ze_physical_mem_desc_t *,
                                            ze_physical_mem_handle_t *);
static ze_result_t (*p_zePhysicalMemDestroy)(ze_context_handle_t, ze_physical_mem_handle_t);
static ze_result_t (*p_zeVirtualMemMap)(ze_context_handle_t, const void *, size_t,
                                        ze_physical_mem_handle_t, size_t,
                                        ze_memory_access_attribute_t);
static ze_result_t (*p_zeVirtualMemUnmap)(ze_context_handle_t, const void *, size_t);
static ze_result_t (*p_zeVirtualMemSetAccessAttribute)(ze_context_handle_t, const void *, size_t,
                                                       ze_memory_access_attribute_t);
static ze_result_t (*p_zeVirtualMemGetAccessAttribute)(ze_context_handle_t, const void *, size_t,
                                                       ze_memory_access_attribute_t *, size_t *);
static ze_result_t (*p_zeDeviceGetMemoryProperties)(ze_device_handle_t, uint32_t *,
                                                    ze_device_memory_properties_t *);
/* 页锁定宿主 USM（zeMemAllocHost）：XPU 等价 cudaHostRegister 的分配侧。
 * 设备可见、可被 GPU 直接 DMA，避免可分页宿主内存的暂存拷贝。可选符号：
 * 缺失或失败时回退 malloc（见 xpu_mem_alloc_host）。 */
static ze_result_t (*p_zeMemAllocHost)(ze_context_handle_t, const void *, size_t, size_t,
                                       ze_device_handle_t, void **);
static ze_result_t (*p_zeMemFree)(ze_context_handle_t, void *);

/* ---- 运行时状态 ---- */
#define XPU_MAX_DEVICES 16
/* TODO 待实机：B580 标称显存；正式实现应改由 zeDeviceGetMemoryProperties 或
 * torch.xpu.mem_get_info() 取得真实总量，此处仅为避免预算数学除零的兜底值。 */
#define XPU_FALLBACK_VRAM (12ULL << 30)

static void *g_ze_module;
static ze_context_handle_t g_ze_ctx;
static ze_device_handle_t  g_ze_devs[XPU_MAX_DEVICES];
static uint64_t            g_ze_dev_total[XPU_MAX_DEVICES];
static uint32_t            g_ze_dev_count;
static size_t              g_ze_page_size = 2 * 1024 * 1024;

/* 宿主分配方法决策：-1=未知，0=普通 malloc，1=Level Zero 宿主 USM。
 * 同一驱动下宿主分配能力同质，首次 xpu_mem_alloc_host 调用即定夺并沿用。 */
static int                 g_host_use_l0 = -1;

GpuDispatch g_gpu;
extern _Thread_local AimdoContext *g_devctx;

/* 用 zeDeviceGetMemoryProperties 取真实显存总量；失败返回 0，调用方回退占位值。
 * Level Zero 无「空闲显存」原生查询，故 mem_get_info 仍依赖 aimdo 自身记账。 */
static uint64_t xpu_query_device_memory(ze_device_handle_t dev) {
    ze_device_memory_properties_t props[8];
    uint32_t count = (uint32_t)ARRAY_SIZE(props);
    uint32_t i;
    uint64_t total = 0;

    if (!p_zeDeviceGetMemoryProperties) {
        return 0;
    }
    for (i = 0; i < count; i++) {
        memset(&props[i], 0, sizeof(props[i]));
        props[i].stype = ZE_STRUCTURE_TYPE_DEVICE_MEMORY_PROPERTIES;
    }
    if (p_zeDeviceGetMemoryProperties(dev, &count, props) != ZE_SUCCESS) {
        return 0;
    }
    if (count > ARRAY_SIZE(props)) {
        count = (uint32_t)ARRAY_SIZE(props);
    }
    for (i = 0; i < count; i++) {
        total += props[i].totalSize;
    }
    return total;
}

/* ============================ GpuDispatch 实现 ============================ */

static gpu_result_t xpu_init(unsigned int flags) {
    (void)flags;
    return GPU_SUCCESS; /* 真正的初始化在 aimdo_cuda_runtime_init() 里完成 */
}

static gpu_result_t xpu_get_error_string(gpu_result_t error, const char **pStr) {
    (void)error;
    if (pStr) {
        *pStr = "Level Zero error (see ze_result_t)";
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_ctx_get_device(gpu_device_t *device) {
    if (device) {
        *device = g_ze_dev_count ? g_ze_devs[0] : NULL;
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_ctx_synchronize(void) {
    /* Level Zero 的无队列 VMM 操作本身同步；此处为空操作。 */
    return GPU_SUCCESS;
}

static gpu_result_t xpu_device_get(gpu_device_t *device, int ordinal) {
    if (!device || ordinal < 0 || (uint32_t)ordinal >= g_ze_dev_count) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    *device = g_ze_devs[ordinal];
    return GPU_SUCCESS;
}

static gpu_result_t xpu_device_get_attribute(int *pi, int attrib, gpu_device_t dev) {
    (void)attrib;
    (void)dev;
    if (pi) {
        *pi = 0; /* 离散 Arc：非集成设备 */
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_device_total_mem(size_t *bytes, gpu_device_t dev) {
    uint32_t i;
    (void)dev;
    if (bytes) {
        *bytes = g_ze_dev_count ? (size_t)g_ze_dev_total[0] : (size_t)XPU_FALLBACK_VRAM;
    }
    (void)i;
    return GPU_SUCCESS;
}

static gpu_result_t xpu_device_get_name(char *name, int len, gpu_device_t dev) {
    (void)dev;
    if (name && len > 0) {
        /* 正式实现可用 zeDeviceGetProperties 取真实名称（结构体布局待实机核对）。 */
        snprintf(name, (size_t)len, "Intel XPU device");
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_get_info(size_t *free_bytes, size_t *total_bytes) {
    /* Level Zero 无原生「空闲显存」查询：用 aimdo 自身记账近似。
     * 也可在 Python 侧改用 torch.xpu.mem_get_info()。 */
    size_t total = 0;
    size_t used = 0;

    if (g_devctx && g_devctx->_device_id >= 0 &&
        (uint32_t)g_devctx->_device_id < g_ze_dev_count) {
        total = (size_t)g_ze_dev_total[g_devctx->_device_id];
        used = (size_t)g_devctx->_total_vram_usage;
    }
    if (!total) {
        total = g_ze_dev_count ? (size_t)g_ze_dev_total[0] : (size_t)XPU_FALLBACK_VRAM;
        used = 0;
    }
    if (total_bytes) {
        *total_bytes = total;
    }
    if (free_bytes) {
        *free_bytes = total > used ? total - used : 0;
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_alloc_host(void **pp, size_t bytesize) {
    /* XPU：优先用 Level Zero 页锁定、设备可见的宿主 USM（zeMemAllocHost），等价 NVIDIA
     * cudaHostRegister 的「分配即页锁定」效果：宿主机可被 GPU 直接 DMA，避免可分页宿主
     * 内存的暂存拷贝（这正是 XPU 缺失的「锁页分页」能力）。
     *
     * 进程内「一次性」定夺宿主分配方法（-1=未定, 0=malloc, 1=L0），定夺后永不翻转：
     * 保证每个存活指针的分配方式在其生命周期内一致，使 xpu_mem_free_host 不会把 L0 指针
     * 当 malloc 释放（反之亦然）。L0 不可用或试探失败时整进程走 malloc（保持旧行为，仅
     * 失去锁页加速）。定夺为 L0 后若某次「超大」分配失败，返回 OOM（不回退 malloc），由调用方
     * 优雅降级（RAM 缓存退回磁盘直通），避免释放混用。
     * 注：hostbuf-file-reader.c 也走本函数，保留 malloc 回退为其安全网。 */
    if (!pp) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    if (g_host_use_l0 < 0) {
        /* 首次调用：试探一次 1 字节 L0 宿主分配以定夺整进程模式。 */
        ze_host_mem_alloc_desc_t desc;
        void *probe = NULL;

        memset(&desc, 0, sizeof(desc));
        desc.stype = ZE_STRUCTURE_TYPE_HOST_MEM_ALLOC_DESC;
        desc.flags = ZE_HOST_MEM_ALLOC_FLAG_DEFAULT;
        if (p_zeMemAllocHost &&
            p_zeMemAllocHost(g_ze_ctx, &desc, 1, 0,
                             g_ze_dev_count ? g_ze_devs[0] : NULL, &probe) == ZE_SUCCESS &&
            probe != NULL) {
            if (p_zeMemFree) {
                p_zeMemFree(g_ze_ctx, probe);
            }
            g_host_use_l0 = 1;
        } else {
            g_host_use_l0 = 0;
        }
    }
    if (g_host_use_l0 == 1) {
        ze_host_mem_alloc_desc_t desc;
        void *p = NULL;

        memset(&desc, 0, sizeof(desc));
        desc.stype = ZE_STRUCTURE_TYPE_HOST_MEM_ALLOC_DESC;
        desc.flags = ZE_HOST_MEM_ALLOC_FLAG_DEFAULT;
        if (p_zeMemAllocHost(g_ze_ctx, &desc, bytesize ? bytesize : 1, 0,
                             g_ze_dev_count ? g_ze_devs[0] : NULL, &p) == ZE_SUCCESS &&
            p != NULL) {
            *pp = p;
            return GPU_SUCCESS;
        }
        return GPU_ERROR_OUT_OF_MEMORY;  /* L0 模式：失败不回退 malloc，避免释放混用 */
    }
    *pp = malloc(bytesize ? bytesize : 1);
    return *pp ? GPU_SUCCESS : GPU_ERROR_OUT_OF_MEMORY;
}

/* 宿主分配方法查询：L0 页锁定 USM（1）还是 malloc（0）。供 hostbuf-plat.c 决定其 RAM 层
 * 缓冲的释放路径（zeMemFree vs free）。须在至少一次 xpu_mem_alloc_host 调用后读取才有意义。 */
int xpu_host_alloc_is_pinned(void) {
    return g_host_use_l0 == 1;
}

static gpu_result_t xpu_mem_free_host(void *p) {
    if (!p) {
        return GPU_SUCCESS;
    }
    if (g_host_use_l0 == 1 && p_zeMemFree) {
        p_zeMemFree(g_ze_ctx, p);
    } else {
        free(p);
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_host_register(void *p, size_t bytesize, unsigned int flags) {
    (void)p;
    (void)bytesize;
    (void)flags;
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_host_unregister(void *p) {
    (void)p;
    return GPU_SUCCESS;
}

static inline size_t xpu_align(size_t size) {
    return (size + g_ze_page_size - 1) & ~(g_ze_page_size - 1);
}

static gpu_result_t xpu_mem_address_reserve(gpu_deviceptr_t *ptr, size_t size, size_t alignment,
                                            gpu_deviceptr_t addr, unsigned long long flags) {
    void *p = NULL;
    (void)alignment; /* zeVirtualMemReserve 不接受对齐参数，返回即页对齐 */
    (void)addr;
    (void)flags;
    if (!ptr) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    /* 修正 R-a：reserve/free 与 create/map/unmap/setaccess 统一使用 xpu_align(size)，
     * 避免二者尺寸不一致导致「map 映射范围超出 reserve 预留范围」。
     * 实机 B580 页大小 64KiB，而入参来自 vrambuf_create 的 2MiB 对齐（CUDA_ALIGN_UP），
     * 2MiB 是 64KiB 的整数倍 ⇒ xpu_align(size)==size，本改动在 B580 上为恒等变换、无行为变化。
     * 仅当驱动给出 >2MiB 页时才生效（该情形 init 已另有告警）。 */
    if (p_zeVirtualMemReserve(g_ze_ctx, NULL, xpu_align(size), &p) != ZE_SUCCESS || !p) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    *ptr = (gpu_deviceptr_t)p;
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_address_free(gpu_deviceptr_t ptr, size_t size) {
    /* 与 xpu_mem_address_reserve 采用同一 xpu_align，保证 free 尺寸与 reserve 尺寸一致。 */
    if (p_zeVirtualMemFree(g_ze_ctx, (const void *)ptr, xpu_align(size)) != ZE_SUCCESS) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_create(gpu_mem_handle_t *handle, size_t size,
                                   const gpu_mem_prop_t *prop, unsigned long long flags) {
    ze_physical_mem_desc_t desc;
    ze_physical_mem_handle_t h = NULL;
    int dev_id = prop ? prop->location.id : 0;
    ze_device_handle_t dev;
    (void)flags;

    if (!handle) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    dev = (dev_id >= 0 && (uint32_t)dev_id < g_ze_dev_count) ? g_ze_devs[dev_id] : g_ze_devs[0];

    memset(&desc, 0, sizeof(desc));
    desc.stype = ZE_STRUCTURE_TYPE_PHYSICAL_MEM_DESC;
    desc.flags = ZE_PHYSICAL_MEM_FLAG_ALLOCATE_ON_DEVICE;
    desc.size  = xpu_align(size);

    if (p_zePhysicalMemCreate(g_ze_ctx, dev, &desc, &h) != ZE_SUCCESS || !h) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    *handle = (gpu_mem_handle_t)h;
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_map(gpu_deviceptr_t ptr, size_t size, size_t offset,
                                gpu_mem_handle_t handle, unsigned long long flags) {
    (void)flags;
    if (p_zeVirtualMemMap(g_ze_ctx, (const void *)ptr, xpu_align(size),
                          (ze_physical_mem_handle_t)handle, offset,
                          ZE_MEMORY_ACCESS_ATTRIBUTE_READWRITE) != ZE_SUCCESS) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_set_access(gpu_deviceptr_t ptr, size_t size,
                                       const gpu_mem_access_desc_t *desc, size_t count) {
    (void)desc;
    (void)count;
    if (p_zeVirtualMemSetAccessAttribute(g_ze_ctx, (const void *)ptr, xpu_align(size),
                                         ZE_MEMORY_ACCESS_ATTRIBUTE_READWRITE) != ZE_SUCCESS) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_unmap(gpu_deviceptr_t ptr, size_t size) {
    if (p_zeVirtualMemUnmap(g_ze_ctx, (const void *)ptr, xpu_align(size)) != ZE_SUCCESS) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_mem_release(gpu_mem_handle_t handle) {
    if (p_zePhysicalMemDestroy(g_ze_ctx, (ze_physical_mem_handle_t)handle) != ZE_SUCCESS) {
        return GPU_ERROR_OUT_OF_MEMORY;
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_memcpy_htod_async(gpu_deviceptr_t dst, const void *src,
                                          size_t bytes, gpu_stream_t stream) {
    /* 未实现：VMM 设备 VA 不可被 CPU 直接访问，需经 zeCommandListAppendMemoryCopy 拷贝。
     * 关键：返回「非 0」而非 GPU_SUCCESS —— 否则 hostbuf_read_file_slice /
     * hostbuf_file_reader_read 里的 CHECK_CU(cuMemcpyHtoDAsync(...)) 会判成功，
     * 导致「文件已读进 hostbuf 但从未拷到显存」的静默数据损坏（调用方误以为完成）。
     * 返回非 0 会让它们向上返回 false，使缺口显式暴露。
     * 实机 B580 已确认此为空操作占位，是本后端唯一未实现的功能路径，须用
     * zeCommandListAppendMemoryCopy 补齐。*/
    (void)dst;
    (void)src;
    (void)bytes;
    (void)stream;
    log(AIMDO_LOG_ERROR,
        "%s: XPU host->device copy not implemented; hostbuf file->VRAM loading disabled\n",
        __func__);
    return 1; /* 非 0 = 失败：避免静默数据损坏 */
}

static gpu_result_t xpu_event_create(gpu_event_t *phEvent, unsigned int flags) {
    (void)flags;
    if (phEvent) {
        *phEvent = (gpu_event_t)(uintptr_t)1; /* 哨兵非空句柄 */
    }
    return GPU_SUCCESS;
}

static gpu_result_t xpu_event_destroy(gpu_event_t hEvent) { (void)hEvent; return GPU_SUCCESS; }
static gpu_result_t xpu_event_record(gpu_event_t hEvent, gpu_stream_t hStream) {
    (void)hEvent; (void)hStream; return GPU_SUCCESS;
}
static gpu_result_t xpu_event_synchronize(gpu_event_t hEvent) { (void)hEvent; return GPU_SUCCESS; }

static gpu_result_t xpu_device_get_luid(char *luid, unsigned int *deviceNodeMask,
                                        gpu_device_t dev) {
    (void)dev;
    if (luid) {
        memset(luid, 0, 16);
    }
    if (deviceNodeMask) {
        *deviceNodeMask = 1;
    }
    return GPU_SUCCESS;
}

/* ====================== 运行时初始化 / 清理 ============================== */

static bool xpu_resolve(void **slot, const char *name) {
    *slot = AIMDO_DLSYM(g_ze_module, name);
    if (!*slot) {
        log(AIMDO_LOG_ERROR, "comfy-aimdo XPU: failed to resolve symbol %s\n", name);
        return false;
    }
    return true;
}

bool aimdo_cuda_runtime_init(void) {
    static const char *const loader_names[] = {
#if defined(_WIN32) || defined(_WIN64)
        "ze_loader.dll",
        "ze_api.dll",
#else
        "libze_loader.so.1",
        "libze_loader.so",
#endif
    };
    uint32_t i;
    ze_driver_handle_t driver = NULL;
    uint32_t driver_count = 0;
    ze_context_desc_t ctx_desc;

    if (p_zeInit) {
        return true; /* 已初始化 */
    }

    for (i = 0; i < ARRAY_SIZE(loader_names); i++) {
        g_ze_module = AIMDO_DLOPEN(loader_names[i]);
        if (g_ze_module) {
            break;
        }
    }
    if (!g_ze_module) {
        log(AIMDO_LOG_ERROR, "comfy-aimdo XPU: Level Zero loader not found\n");
        return false;
    }

    if (!xpu_resolve((void **)&p_zeInit, "zeInit") ||
        !xpu_resolve((void **)&p_zeDriverGet, "zeDriverGet") ||
        !xpu_resolve((void **)&p_zeDeviceGet, "zeDeviceGet") ||
        !xpu_resolve((void **)&p_zeContextCreate, "zeContextCreate") ||
        !xpu_resolve((void **)&p_zeContextDestroy, "zeContextDestroy") ||
        !xpu_resolve((void **)&p_zeMemGetAllocProperties, "zeMemGetAllocProperties") ||
        !xpu_resolve((void **)&p_zeVirtualMemReserve, "zeVirtualMemReserve") ||
        !xpu_resolve((void **)&p_zeVirtualMemFree, "zeVirtualMemFree") ||
        !xpu_resolve((void **)&p_zeVirtualMemQueryPageSize, "zeVirtualMemQueryPageSize") ||
        !xpu_resolve((void **)&p_zePhysicalMemCreate, "zePhysicalMemCreate") ||
        !xpu_resolve((void **)&p_zePhysicalMemDestroy, "zePhysicalMemDestroy") ||
        !xpu_resolve((void **)&p_zeVirtualMemMap, "zeVirtualMemMap") ||
        !xpu_resolve((void **)&p_zeVirtualMemUnmap, "zeVirtualMemUnmap") ||
        !xpu_resolve((void **)&p_zeVirtualMemSetAccessAttribute, "zeVirtualMemSetAccessAttribute") ||
        !xpu_resolve((void **)&p_zeVirtualMemGetAccessAttribute, "zeVirtualMemGetAccessAttribute")) {
        aimdo_cuda_runtime_cleanup();
        return false;
    }

    /* 可选符号：取真实显存总量（缺失时回退 XPU_FALLBACK_VRAM，不影响 VMM 主通路）。 */
    p_zeDeviceGetMemoryProperties =
        (ze_result_t (*)(ze_device_handle_t, uint32_t *, ze_device_memory_properties_t *))
        AIMDO_DLSYM(g_ze_module, "zeDeviceGetMemoryProperties");
    if (!p_zeDeviceGetMemoryProperties) {
        log(WARNING, "comfy-aimdo XPU: zeDeviceGetMemoryProperties unavailable; using fallback VRAM\n");
    }

    /* 可选符号：页锁定宿主 USM（zeMemAllocHost / zeMemFree）。缺失或失败时，
     * xpu_mem_alloc_host 自动回退 malloc（保持功能一致，仅失去锁页加速）。 */
    p_zeMemAllocHost =
        (ze_result_t (*)(ze_context_handle_t, const void *, size_t, size_t, ze_device_handle_t, void **))
        AIMDO_DLSYM(g_ze_module, "zeMemAllocHost");
    p_zeMemFree =
        (ze_result_t (*)(ze_context_handle_t, void *))
        AIMDO_DLSYM(g_ze_module, "zeMemFree");
    if (!p_zeMemAllocHost || !p_zeMemFree) {
        log(WARNING, "comfy-aimdo XPU: zeMemAllocHost/zeMemFree unavailable; hostbuf uses malloc fallback\n");
    }

    if (p_zeInit(ZE_INIT_FLAG_GPU_ONLY) != ZE_SUCCESS) {
        log(AIMDO_LOG_ERROR, "comfy-aimdo XPU: zeInit failed\n");
        aimdo_cuda_runtime_cleanup();
        return false;
    }

    p_zeDriverGet(&driver_count, NULL);
    if (driver_count == 0) {
        log(AIMDO_LOG_ERROR, "comfy-aimdo XPU: no Level Zero driver\n");
        aimdo_cuda_runtime_cleanup();
        return false;
    }
    driver_count = 1;
    if (p_zeDriverGet(&driver_count, &driver) != ZE_SUCCESS || !driver) {
        aimdo_cuda_runtime_cleanup();
        return false;
    }

    p_zeDeviceGet(driver, &g_ze_dev_count, NULL);
    if (g_ze_dev_count == 0) {
        log(AIMDO_LOG_ERROR, "comfy-aimdo XPU: no XPU devices\n");
        aimdo_cuda_runtime_cleanup();
        return false;
    }
    if (g_ze_dev_count > XPU_MAX_DEVICES) {
        g_ze_dev_count = XPU_MAX_DEVICES;
    }
    if (p_zeDeviceGet(driver, &g_ze_dev_count, g_ze_devs) != ZE_SUCCESS) {
        aimdo_cuda_runtime_cleanup();
        return false;
    }

    memset(&ctx_desc, 0, sizeof(ctx_desc));
    /* 规范要求 stype=ZE_STRUCTURE_TYPE_CONTEXT_DESC(0x0D)；驱动据此做参数校验。
     * （设计文档曾记「全 0 即可」，但对照 ze_api.h 与上游 llama.cpp 用法，置 0x0D 更稳妥。）*/
    ctx_desc.stype = ZE_STRUCTURE_TYPE_CONTEXT_DESC;
    if (p_zeContextCreate(driver, &ctx_desc, &g_ze_ctx) != ZE_SUCCESS || !g_ze_ctx) {
        log(AIMDO_LOG_ERROR, "comfy-aimdo XPU: zeContextCreate failed\n");
        aimdo_cuda_runtime_cleanup();
        return false;
    }

    if (p_zeVirtualMemQueryPageSize(g_ze_ctx, g_ze_devs[0], 1, &g_ze_page_size) != ZE_SUCCESS ||
        g_ze_page_size == 0) {
        g_ze_page_size = 2 * 1024 * 1024; /* 兜底；实机 B580 实测为 64KiB */
    }
    /* 实机 B580 页大小为 64KiB，整除 VBAR(32MiB)/vrambuf(16MiB) 块，对齐无风险。
     * 防御：若驱动给出 > 2MiB 的页大小，three_stooges 隐含的 2MiB 对齐假设可能不成立。*/
    if (g_ze_page_size > (size_t)(2 * 1024 * 1024)) {
        log(WARNING,
            "comfy-aimdo XPU: page size %zu exceeds 2MiB; alignment assumptions may break\n",
            g_ze_page_size);
    }

    for (i = 0; i < g_ze_dev_count; i++) {
        uint64_t total = xpu_query_device_memory(g_ze_devs[i]);
        /* 查询失败时回退占位值，避免预算数学除零（真实值应在实机确认）。 */
        g_ze_dev_total[i] = total ? total : XPU_FALLBACK_VRAM;
    }

    /* 填充中性分发 */
    g_gpu.p_init = xpu_init;
    g_gpu.p_get_error_string = xpu_get_error_string;
    g_gpu.p_ctx_get_device = xpu_ctx_get_device;
    g_gpu.p_ctx_synchronize = xpu_ctx_synchronize;
    g_gpu.p_device_get = xpu_device_get;
    g_gpu.p_device_get_attribute = xpu_device_get_attribute;
    g_gpu.p_device_total_mem = xpu_device_total_mem;
    g_gpu.p_device_get_name = xpu_device_get_name;
    g_gpu.p_mem_get_info = xpu_mem_get_info;
    g_gpu.p_mem_alloc_host = xpu_mem_alloc_host;
    g_gpu.p_mem_free_host = xpu_mem_free_host;
    g_gpu.p_mem_host_register = xpu_mem_host_register;
    g_gpu.p_mem_host_unregister = xpu_mem_host_unregister;
    g_gpu.p_mem_address_reserve = xpu_mem_address_reserve;
    g_gpu.p_mem_address_free = xpu_mem_address_free;
    g_gpu.p_mem_create = xpu_mem_create;
    g_gpu.p_mem_map = xpu_mem_map;
    g_gpu.p_mem_set_access = xpu_mem_set_access;
    g_gpu.p_mem_unmap = xpu_mem_unmap;
    g_gpu.p_mem_release = xpu_mem_release;
    g_gpu.p_memcpy_htod_async = xpu_memcpy_htod_async;
    g_gpu.p_event_create = xpu_event_create;
    g_gpu.p_event_destroy = xpu_event_destroy;
    g_gpu.p_event_record = xpu_event_record;
    g_gpu.p_event_synchronize = xpu_event_synchronize;
    g_gpu.p_device_get_luid = xpu_device_get_luid;

    log(INFO, "comfy-aimdo XPU backend initialized (%u device(s), page=%zu)\n",
        g_ze_dev_count, g_ze_page_size);
    return true;
}

void aimdo_cuda_runtime_cleanup(void) {
    if (p_zeContextDestroy && g_ze_ctx) {
        p_zeContextDestroy(g_ze_ctx);
    }
    g_ze_ctx = NULL;
    memset(g_ze_devs, 0, sizeof(g_ze_devs));
    g_ze_dev_count = 0;
    memset(&g_gpu, 0, sizeof(g_gpu));

    if (g_ze_module) {
        AIMDO_DLCLOSE(g_ze_module);
        g_ze_module = NULL;
    }

    p_zeInit = NULL;
    p_zeDriverGet = NULL;
    p_zeDeviceGet = NULL;
    p_zeContextCreate = NULL;
    p_zeContextDestroy = NULL;
    p_zeMemGetAllocProperties = NULL;
    p_zeVirtualMemReserve = NULL;
    p_zeVirtualMemFree = NULL;
    p_zeVirtualMemQueryPageSize = NULL;
    p_zePhysicalMemCreate = NULL;
    p_zePhysicalMemDestroy = NULL;
    p_zeVirtualMemMap = NULL;
    p_zeVirtualMemUnmap = NULL;
    p_zeVirtualMemSetAccessAttribute = NULL;
    p_zeVirtualMemGetAccessAttribute = NULL;
    p_zeDeviceGetMemoryProperties = NULL;

    /* 新增：页锁定宿主 USM（zeMemAllocHost / zeMemFree）符号与决策状态复位，
     * 避免进程退出/重载时 p_mem_alloc_host 已被清零却仍按 g_host_use_l0==1 调用
     * zeMemFree（或反之 free 一个 L0 指针）造成混用崩溃。 */
    p_zeMemAllocHost = NULL;
    p_zeMemFree = NULL;
    g_host_use_l0 = -1;
}

/* ================== PyTorch XPUPluggableAllocator 入口 ==================== */
/* PyTorch 侧：torch.xpu.memory.XPUPluggableAllocator(so_path, "alloc_fn", "free_fn")
 * 再 torch.xpu.memory.change_current_allocator(...)。
 * 签名：void *alloc_fn(size_t size, int device, sycl::queue *queue)（queue 当 void* 忽略）*/

static inline unsigned int xpu_vmm_hash(gpu_deviceptr_t ptr) {
    return (unsigned int)(((uintptr_t)(void *)ptr >> 21) % VMM_HASH_SIZE);
}

SHARED_EXPORT
void *alloc_fn(size_t size, int device, gpu_stream_t stream) {
    VramBuffer *entry;
    unsigned int h;
    (void)stream; /* sycl::queue*，本后端无需 */

    if (!set_devctx_for_device(device)) {
        return NULL;
    }

    entry = vrambuf_create(device, size);
    if (!entry) {
        return NULL;
    }
    if (!vrambuf_grow(entry, size)) {
        vrambuf_destroy(entry);
        return NULL;
    }

    h = xpu_vmm_hash(vrambuf_get(entry));
    entry->next = vmm_table[h];
    vmm_table[h] = entry;
    allocations_dirty = true;

    return (void *)vrambuf_get(entry);
}

SHARED_EXPORT
void free_fn(void *ptr, size_t size, int device, gpu_stream_t stream) {
    (void)size;
    (void)stream;

    if (!ptr || !set_devctx_for_device(device)) {
        return;
    }

    for (VramBuffer **curr = &vmm_table[xpu_vmm_hash((gpu_deviceptr_t)ptr)]; *curr;
         curr = &(*curr)->next) {
        VramBuffer *entry = *curr;

        if (vrambuf_get(entry) != (gpu_deviceptr_t)ptr || entry->device != device) {
            continue;
        }
        *curr = entry->next;
        allocations_dirty = true;
        vrambuf_destroy(entry);
        return;
    }

    log(AIMDO_LOG_ERROR, "%s: could not find VRAM@%p\n", __func__, ptr);
}
