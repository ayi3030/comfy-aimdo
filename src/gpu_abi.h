#pragma once

#include <stddef.h>
#include <stdint.h>

#if defined(__HIP_PLATFORM_AMD__)
/* HIP runtime exports use the platform C ABI. */
#define CUDAAPI
#elif defined(_WIN32) || defined(_WIN64)
#define CUDAAPI __stdcall
#else
#define CUDAAPI
#endif

typedef unsigned long long cuuint64_t;

#if defined(_WIN64) || defined(__LP64__) || defined(_LP64)
typedef unsigned long long CUdeviceptr;
#else
typedef unsigned int CUdeviceptr;
#endif

typedef int CUdevice;
typedef int CUresult;
typedef struct CUctx_st *CUcontext;
typedef struct CUstream_st *CUstream;
typedef struct CUevent_st *CUevent;
typedef unsigned long long CUmemGenericAllocationHandle;

typedef struct CUuuid_st {
    char bytes[16];
} CUuuid;

/* hipDeviceAttribute_t is not numerically compatible with CUdevice_attribute.
 * hipDeviceAttributeIntegrated is 16, so querying a HIP device with the CUDA
 * value would read hipDeviceAttributeKernelExecTimeout instead.
 */
typedef enum CUdevice_attribute_enum {
#if defined(__HIP_PLATFORM_AMD__)
    CU_DEVICE_ATTRIBUTE_INTEGRATED = 16,
#else
    CU_DEVICE_ATTRIBUTE_INTEGRATED = 18,
#endif
} CUdevice_attribute;

typedef enum cudaError_enum {
    CUDA_SUCCESS = 0,
    CUDA_ERROR_OUT_OF_MEMORY = 2,
} cudaError_enum;

typedef enum CUevent_flags_enum {
    CU_EVENT_DISABLE_TIMING = 0x2,
} CUevent_flags;

typedef enum CUmemAllocationHandleType_enum {
    CU_MEM_HANDLE_TYPE_NONE = 0x0,
} CUmemAllocationHandleType;

typedef enum CUmemAccess_flags_enum {
    CU_MEM_ACCESS_FLAGS_PROT_NONE = 0x0,
    CU_MEM_ACCESS_FLAGS_PROT_READ = 0x1,
    CU_MEM_ACCESS_FLAGS_PROT_READWRITE = 0x3,
} CUmemAccess_flags;

typedef enum CUmemLocationType_enum {
    CU_MEM_LOCATION_TYPE_INVALID = 0x0,
    CU_MEM_LOCATION_TYPE_DEVICE = 0x1,
    CU_MEM_LOCATION_TYPE_HOST = 0x2,
    CU_MEM_LOCATION_TYPE_HOST_NUMA = 0x3,
    CU_MEM_LOCATION_TYPE_HOST_NUMA_CURRENT = 0x4,
} CUmemLocationType;

typedef enum CUmemAllocationType_enum {
    CU_MEM_ALLOCATION_TYPE_INVALID = 0x0,
    CU_MEM_ALLOCATION_TYPE_PINNED = 0x1,
} CUmemAllocationType;

typedef struct CUmemLocation_st {
    CUmemLocationType type;
    int id;
} CUmemLocation;

typedef struct CUmemAllocationProp_st {
    CUmemAllocationType type;
    CUmemAllocationHandleType requestedHandleTypes;
    CUmemLocation location;
    void *win32HandleMetaData;
    struct {
        unsigned char compressionType;
        unsigned char gpuDirectRDMACapable;
        unsigned short usage;
        unsigned char reserved[4];
    } allocFlags;
} CUmemAllocationProp;

typedef struct CUmemAccessDesc_st {
    CUmemLocation location;
    CUmemAccess_flags flags;
} CUmemAccessDesc;

typedef enum CUdriverProcAddress_flags_enum {
    CU_GET_PROC_ADDRESS_DEFAULT = 0x0,
    CU_GET_PROC_ADDRESS_LEGACY_STREAM = 0x1,
    CU_GET_PROC_ADDRESS_PER_THREAD_DEFAULT_STREAM = 0x2,
} CUdriverProcAddress_flags;

typedef enum CUdriverProcAddressQueryResult_enum {
    CU_GET_PROC_ADDRESS_SUCCESS = 0,
    CU_GET_PROC_ADDRESS_SYMBOL_NOT_FOUND = 1,
    CU_GET_PROC_ADDRESS_VERSION_NOT_SUFFICIENT = 2,
} CUdriverProcAddressQueryResult;

/* ============================================================================
 * 后端无关中性类型（XPU 适配新增）
 *
 * 目的：让共享的 src/*.c 在不改动逻辑的前提下同时服务 CUDA / HIP / XPU。
 *  - AIMDO_XPU 构建：真中性类型，完全不依赖任何 CUDA 类型（Level Zero 后端填充 g_gpu）。
 *  - CUDA/HIP 构建：typedef 回原 CUDA 类型（HIP 的驱动 API 与 CUDA ABI 兼容），
 *                  因此既有调用点语义与字节级行为完全不变。
 *
 * 注意：gpu_mem_prop_t 的字段布局对齐 CUDA 的 CUmemAllocationProp 的三个关键字段
 *       （type / location.type / location.id），以便 three_stooges() 用同一份字面量
 *       初始化语法在两种后端下都能编译。
 * ==========================================================================*/
#if defined(AIMDO_XPU)
typedef int    gpu_result_t;
typedef void  *gpu_device_t;
/* XPU 分支的 gpu_deviceptr_t 必须是整型而非 void*：共享的 src/*.c（malloc-graph /
 * vrambuf / model-vbar 等）对设备地址做指针偏移算术（如 ptr + value * MG_PAGE），
 * 在 MSVC 上 void* 算术会报 C2036（'void *': unknown size）；CUDA 分支的
 * gpu_deviceptr_t = CUdeviceptr = unsigned long long 本就是整型，语义一致。
 * 改用 uintptr_t 既支持算术、又能无损承载 64 位地址，CUDA/ROCm 分支不受影响。 */
typedef uintptr_t gpu_deviceptr_t;
typedef void  *gpu_mem_handle_t;
typedef void  *gpu_stream_t;   /* Level Zero 无 CUDA stream 概念，占位 */
typedef void  *gpu_event_t;    /* Level Zero 事件用占位实现 */

typedef struct gpu_mem_location_st {
    int type;
    int id;
} gpu_mem_location_t;

typedef struct gpu_mem_access_desc_st {
    gpu_mem_location_t location;
    int flags;
} gpu_mem_access_desc_t;

typedef struct gpu_mem_prop_st {
    int type;
    int requestedHandleTypes;
    gpu_mem_location_t location;
} gpu_mem_prop_t;

#define GPU_SUCCESS                          0
#define GPU_ERROR_OUT_OF_MEMORY              2
#define GPU_MEM_ALLOCATION_TYPE_PINNED       1
#define GPU_MEM_LOCATION_TYPE_DEVICE         1
#define GPU_MEM_ACCESS_FLAGS_PROT_READWRITE  3
#define GPU_EVENT_DISABLE_TIMING             0x2
#define GPU_DEVICE_ATTRIBUTE_INTEGRATED      18
#else
typedef CUresult                        gpu_result_t;
typedef CUdevice                        gpu_device_t;
typedef CUdeviceptr                     gpu_deviceptr_t;
typedef CUmemGenericAllocationHandle    gpu_mem_handle_t;
typedef CUstream                        gpu_stream_t;
typedef CUevent                         gpu_event_t;
typedef CUmemAllocationProp             gpu_mem_prop_t;
typedef CUmemAccessDesc                 gpu_mem_access_desc_t;

#define GPU_SUCCESS                          CUDA_SUCCESS
#define GPU_ERROR_OUT_OF_MEMORY              CUDA_ERROR_OUT_OF_MEMORY
#define GPU_MEM_ALLOCATION_TYPE_PINNED       CU_MEM_ALLOCATION_TYPE_PINNED
#define GPU_MEM_LOCATION_TYPE_DEVICE         CU_MEM_LOCATION_TYPE_DEVICE
#define GPU_MEM_ACCESS_FLAGS_PROT_READWRITE  CU_MEM_ACCESS_FLAGS_PROT_READWRITE
#define GPU_EVENT_DISABLE_TIMING             CU_EVENT_DISABLE_TIMING
#define GPU_DEVICE_ATTRIBUTE_INTEGRATED      CU_DEVICE_ATTRIBUTE_INTEGRATED
#endif
