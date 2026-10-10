#include "plat.h"
#include "hostbuf-plat.h"

#include <windows.h>

size_t hostbuf_page_size(void) {
    static size_t page_size;

    if (!page_size) {
        SYSTEM_INFO info;

        GetSystemInfo(&info);
        page_size = info.dwPageSize;
    }

    return page_size;
}

size_t hostbuf_reserve_granularity(void) {
    static size_t granularity;

    if (!granularity) {
        SYSTEM_INFO info;

        GetSystemInfo(&info);
        granularity = info.dwAllocationGranularity;
    }

    return granularity;
}

#if defined(AIMDO_XPU)
/* ============================================================================
 * XPU（Intel Arc / oneAPI Level Zero）后端：宿主 RAM 层缓冲的页锁定分配
 *
 * 目标：等价 NVIDIA cudaHostRegister 的「页锁定宿主内存」能力——宿主机被驱动锁页、
 *       设备（GPU）可直接 DMA，避免可分页宿主内存的暂存拷贝，从而让 XPU 的 RAM 层
 *       （hostbuf）在 H2D 拷贝与「显存溢出→宿主缓存」语义上对齐英伟达。
 *
 * 实现要点：
 *   - L0 宿主 USM（zeMemAllocHost）是「分配即锁页、设备可见、已提交」的单次分配，
 *     没有 CUDA 那种「VirtualAlloc 保留 → 逐块提交 → cuMemHostRegister 逐块锁页」
 *     的两段式能力。因此这里一次性分配全部 reserved_size，等价于 NVIDIA 三段式的合并。
 *   - capability 在驱动内同质：进程内首次 reserve 时探测一次（分配并释放 1 字节），
 *     由 xpu_mem_alloc_host 进程级决策（L0 页锁定 USM vs malloc）经
 *     xpu_host_alloc_is_pinned() 定夺整进程模式，避免后续 zeMemFree / free 混用。
 *       * g_hostbuf_pinned == 1  ⇔  xpu_host_alloc_is_pinned()==1  →  全程 zeMemAllocHost/zeMemFree
 *       * g_hostbuf_pinned == 0             →  全程 VirtualAlloc（与改前逐字节等价）
 *   - 若某次「超大整块」zeMemAllocHost 失败（如系统无法锁页 reserved_size），reserve
 *     返回 NULL，让 hostbuf_grow 走 OOM 失败路径（RAM 缓存优雅退回磁盘直通），
 *     不回退 VirtualAlloc，避免释放时 zeMemFree/free 混用。
 *
 * 注意：此分支仅当 AIMDO_XPU 构建（aimdo_xpu.dll）激活；CUDA/ROCm 走下方 #else，
 *       字节级保持不变。
 * ==========================================================================*/

static int g_hostbuf_pinned = -1;  /* -1=未知, 0=VirtualAlloc, 1=zeMemAllocHost */

static int hostbuf_xpu_probe_pinned(void) {
    /* 一次性探测：分配并释放 1 字节宿主 USM，触发 xpu_mem_alloc_host 内部的进程级
     * 决策（L0 页锁定 USM vs malloc），再据 xpu_host_alloc_is_pinned() 定夺整进程模式。
     * 引用 g_gpu 的函数指针做空指针保护——若后端尚未初始化（p_mem_alloc_host 为 NULL），
     * 视为不可用，回退 VirtualAlloc。 */
    void *p = NULL;

    if (g_gpu.p_mem_alloc_host && g_gpu.p_mem_free_host) {
        if (g_gpu.p_mem_alloc_host(&p, 1) == GPU_SUCCESS && p) {
            g_gpu.p_mem_free_host(p);
        }
    }
    /* 与 xpu_mem_alloc_host 的进程级决策严格一致，杜绝 zeMemFree/free 混用。 */
    g_hostbuf_pinned = xpu_host_alloc_is_pinned() ? 1 : 0;
    return g_hostbuf_pinned;
}

void *hostbuf_reserve_address_space(size_t size) {
    if (g_hostbuf_pinned < 0) {
        hostbuf_xpu_probe_pinned();
    }
    if (g_hostbuf_pinned == 1) {
        void *p = NULL;
        /* zeMemAllocHost 一次性分配全部 reserved_size：页锁定、设备可见、已提交。 */
        if (cuMemAllocHost(&p, size) == GPU_SUCCESS && p) {
            return p;
        }
        /* 超大整块分配失败：返回 NULL 走 OOM 失败路径，不回退 VirtualAlloc。 */
        return NULL;
    }
    /* 回退：保持旧 VirtualAlloc 可分页行为（与改前逐字节等价）。 */
    return VirtualAlloc(NULL, size, MEM_RESERVE, PAGE_NOACCESS);
}

bool hostbuf_commit_address_space(void *ptr, size_t size) {
    if (g_hostbuf_pinned == 1) {
        /* zeMemAllocHost 已一次性提交并锁页，无需再逐块提交。 */
        (void)ptr;
        (void)size;
        return true;
    }
    return VirtualAlloc(ptr, size, MEM_COMMIT, PAGE_READWRITE) == ptr;
}

bool hostbuf_decommit_address_space(void *ptr, size_t size) {
    if (g_hostbuf_pinned == 1) {
        /* zeMemAllocHost 不可部分释放；整块释放见 hostbuf_release_address_space。 */
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
    if (g_hostbuf_pinned == 1) {
        cuMemFreeHost(ptr);
        return;
    }
    VirtualFree(ptr, 0, MEM_RELEASE);
}

#else /* CUDA / ROCm（及非 XPU 平台）：原样保留，字节级不变 ==================== */

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
