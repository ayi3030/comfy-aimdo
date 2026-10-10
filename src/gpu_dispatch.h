#pragma once

#include "gpu_abi.h"

#include <stdbool.h>

/* 后端运行时初始化/清理：每个后端（src-cuda / src-hip / src-xpu 的 dispatch.c）
 * 各自定义同名符号，共享层只调用这一个入口，因此无需改动 control.c。 */
bool aimdo_cuda_runtime_init(void);
void aimdo_cuda_runtime_cleanup(void);

#if !defined(AIMDO_XPU)
/* ---------------------------------------------------------------------------
 * CUDA / HIP 后端：沿用原有基于 CUDA 驱动 API 形状的分发结构。
 * -------------------------------------------------------------------------*/
typedef CUresult (CUDAAPI *PFN_cuInit)(unsigned int flags);
typedef CUresult (CUDAAPI *PFN_cuGetProcAddress)(const char *symbol, void **pfn, int cudaVersion,
                                                 cuuint64_t flags,
                                                 CUdriverProcAddressQueryResult *symbolStatus);
typedef CUresult (CUDAAPI *PFN_cuGetErrorString)(CUresult error, const char **pStr);
typedef CUresult (CUDAAPI *PFN_cuCtxGetDevice)(CUdevice *device);
typedef CUresult (CUDAAPI *PFN_cuCtxSynchronize)(void);
typedef CUresult (CUDAAPI *PFN_cuDeviceGet)(CUdevice *device, int ordinal);
typedef CUresult (CUDAAPI *PFN_cuDeviceGetAttribute)(int *pi, CUdevice_attribute attrib,
                                                     CUdevice dev);
typedef CUresult (CUDAAPI *PFN_cuDeviceTotalMem)(size_t *bytes, CUdevice dev);
typedef CUresult (CUDAAPI *PFN_cuDeviceGetName)(char *name, int len, CUdevice dev);
typedef CUresult (CUDAAPI *PFN_cuDeviceGetUuid)(CUuuid *uuid, CUdevice dev);
typedef CUresult (CUDAAPI *PFN_cuMemGetInfo)(size_t *free_bytes, size_t *total_bytes);
typedef CUresult (CUDAAPI *PFN_cuMemAlloc_v2)(CUdeviceptr *dptr, size_t bytesize);
typedef CUresult (CUDAAPI *PFN_cuMemFree_v2)(CUdeviceptr dptr);
typedef CUresult (CUDAAPI *PFN_cuMemAllocAsync)(CUdeviceptr *dptr, size_t bytesize,
                                                CUstream hStream);
typedef CUresult (CUDAAPI *PFN_cuMemFreeAsync)(CUdeviceptr dptr, CUstream hStream);
typedef CUresult (CUDAAPI *PFN_cuMemAllocHost)(void **pp, size_t bytesize);
typedef CUresult (CUDAAPI *PFN_cuMemFreeHost)(void *p);
typedef CUresult (CUDAAPI *PFN_cuMemHostRegister)(void *p, size_t bytesize,
                                                 unsigned int flags);
typedef CUresult (CUDAAPI *PFN_cuMemHostUnregister)(void *p);
typedef CUresult (CUDAAPI *PFN_cuMemAddressReserve)(CUdeviceptr *ptr, size_t size,
                                                    size_t alignment, CUdeviceptr addr,
                                                    unsigned long long flags);
typedef CUresult (CUDAAPI *PFN_cuMemAddressFree)(CUdeviceptr ptr, size_t size);
typedef CUresult (CUDAAPI *PFN_cuMemCreate)(CUmemGenericAllocationHandle *handle, size_t size,
                                            const CUmemAllocationProp *prop,
                                            unsigned long long flags);
typedef CUresult (CUDAAPI *PFN_cuMemMap)(CUdeviceptr ptr, size_t size, size_t offset,
                                         CUmemGenericAllocationHandle handle,
                                         unsigned long long flags);
typedef CUresult (CUDAAPI *PFN_cuMemSetAccess)(CUdeviceptr ptr, size_t size,
                                               const CUmemAccessDesc *desc, size_t count);
typedef CUresult (CUDAAPI *PFN_cuMemUnmap)(CUdeviceptr ptr, size_t size);
typedef CUresult (CUDAAPI *PFN_cuMemRelease)(CUmemGenericAllocationHandle handle);
typedef CUresult (CUDAAPI *PFN_cuMemcpyHtoDAsync)(CUdeviceptr dst, const void *src,
                                                  size_t bytes, CUstream hStream);
typedef CUresult (CUDAAPI *PFN_cuEventCreate)(CUevent *phEvent, unsigned int flags);
typedef CUresult (CUDAAPI *PFN_cuEventDestroy)(CUevent hEvent);
typedef CUresult (CUDAAPI *PFN_cuEventRecord)(CUevent hEvent, CUstream hStream);
typedef CUresult (CUDAAPI *PFN_cuEventSynchronize)(CUevent hEvent);
typedef CUresult (CUDAAPI *PFN_cuDeviceGetLuid)(char *luid, unsigned int *deviceNodeMask,
                                                CUdevice dev);

typedef struct AimdoCudaDispatch {
    PFN_cuInit p_cuInit;
    PFN_cuGetProcAddress p_cuGetProcAddress;
    PFN_cuGetErrorString p_cuGetErrorString;
    PFN_cuCtxGetDevice p_cuCtxGetDevice;
    PFN_cuCtxSynchronize p_cuCtxSynchronize;
    PFN_cuDeviceGet p_cuDeviceGet;
    PFN_cuDeviceGetAttribute p_cuDeviceGetAttribute;
    PFN_cuDeviceTotalMem p_cuDeviceTotalMem;
    PFN_cuDeviceGetName p_cuDeviceGetName;
    PFN_cuDeviceGetUuid p_cuDeviceGetUuid;
    PFN_cuMemGetInfo p_cuMemGetInfo;
    PFN_cuMemAlloc_v2 p_cuMemAlloc_v2;
    PFN_cuMemFree_v2 p_cuMemFree_v2;
    PFN_cuMemAllocAsync p_cuMemAllocAsync;
    PFN_cuMemAllocAsync p_cuMemAllocAsync_ptsz;
    PFN_cuMemFreeAsync p_cuMemFreeAsync;
    PFN_cuMemFreeAsync p_cuMemFreeAsync_ptsz;
    PFN_cuMemAllocHost p_cuMemAllocHost;
    PFN_cuMemFreeHost p_cuMemFreeHost;
    PFN_cuMemHostRegister p_cuMemHostRegister;
    PFN_cuMemHostUnregister p_cuMemHostUnregister;
    PFN_cuMemAddressReserve p_cuMemAddressReserve;
    PFN_cuMemAddressFree p_cuMemAddressFree;
    PFN_cuMemCreate p_cuMemCreate;
    PFN_cuMemMap p_cuMemMap;
    PFN_cuMemSetAccess p_cuMemSetAccess;
    PFN_cuMemUnmap p_cuMemUnmap;
    PFN_cuMemRelease p_cuMemRelease;
    PFN_cuMemcpyHtoDAsync p_cuMemcpyHtoDAsync;
    PFN_cuEventCreate p_cuEventCreate;
    PFN_cuEventDestroy p_cuEventDestroy;
    PFN_cuEventRecord p_cuEventRecord;
    PFN_cuEventSynchronize p_cuEventSynchronize;
    PFN_cuDeviceGetLuid p_cuDeviceGetLuid;
} AimdoCudaDispatch;

extern AimdoCudaDispatch g_cuda;

typedef CUresult (CUDAAPI *PFN_deviceGetProperties)(void *prop, CUdevice dev);

extern PFN_deviceGetProperties g_device_get_properties;

#if (defined(_WIN32) || defined(_WIN64)) && defined(AIMDO_CUDA)
bool aimdo_nvml_device_init(CUdevice device, void **handle);
bool aimdo_nvml_memory_info(void *handle, size_t *free_bytes, size_t *total_bytes);
#endif

#else /* AIMDO_XPU -----------------------------------------------------------
 * XPU（Intel Arc / oneAPI Level Zero）后端：后端无关中性分发结构。
 * 由 src-xpu/dispatch.c 在 aimdo_cuda_runtime_init() 中填充。
 * -------------------------------------------------------------------------*/
typedef gpu_result_t (*PFN_gpu_init)(unsigned int flags);
typedef gpu_result_t (*PFN_gpu_get_error_string)(gpu_result_t error, const char **pStr);
typedef gpu_result_t (*PFN_gpu_ctx_get_device)(gpu_device_t *device);
typedef gpu_result_t (*PFN_gpu_ctx_synchronize)(void);
typedef gpu_result_t (*PFN_gpu_device_get)(gpu_device_t *device, int ordinal);
typedef gpu_result_t (*PFN_gpu_device_get_attribute)(int *pi, int attrib, gpu_device_t dev);
typedef gpu_result_t (*PFN_gpu_device_total_mem)(size_t *bytes, gpu_device_t dev);
typedef gpu_result_t (*PFN_gpu_device_get_name)(char *name, int len, gpu_device_t dev);
typedef gpu_result_t (*PFN_gpu_mem_get_info)(size_t *free_bytes, size_t *total_bytes);
typedef gpu_result_t (*PFN_gpu_mem_alloc_host)(void **pp, size_t bytesize);
typedef gpu_result_t (*PFN_gpu_mem_free_host)(void *p);
typedef gpu_result_t (*PFN_gpu_mem_host_register)(void *p, size_t bytesize, unsigned int flags);
typedef gpu_result_t (*PFN_gpu_mem_host_unregister)(void *p);
typedef gpu_result_t (*PFN_gpu_mem_address_reserve)(gpu_deviceptr_t *ptr, size_t size,
                                                    size_t alignment, gpu_deviceptr_t addr,
                                                    unsigned long long flags);
typedef gpu_result_t (*PFN_gpu_mem_address_free)(gpu_deviceptr_t ptr, size_t size);
typedef gpu_result_t (*PFN_gpu_mem_create)(gpu_mem_handle_t *handle, size_t size,
                                           const gpu_mem_prop_t *prop,
                                           unsigned long long flags);
typedef gpu_result_t (*PFN_gpu_mem_map)(gpu_deviceptr_t ptr, size_t size, size_t offset,
                                        gpu_mem_handle_t handle, unsigned long long flags);
typedef gpu_result_t (*PFN_gpu_mem_set_access)(gpu_deviceptr_t ptr, size_t size,
                                               const gpu_mem_access_desc_t *desc, size_t count);
typedef gpu_result_t (*PFN_gpu_mem_unmap)(gpu_deviceptr_t ptr, size_t size);
typedef gpu_result_t (*PFN_gpu_mem_release)(gpu_mem_handle_t handle);
typedef gpu_result_t (*PFN_gpu_memcpy_htod_async)(gpu_deviceptr_t dst, const void *src,
                                                  size_t bytes, gpu_stream_t stream);
typedef gpu_result_t (*PFN_gpu_event_create)(gpu_event_t *phEvent, unsigned int flags);
typedef gpu_result_t (*PFN_gpu_event_destroy)(gpu_event_t hEvent);
typedef gpu_result_t (*PFN_gpu_event_record)(gpu_event_t hEvent, gpu_stream_t hStream);
typedef gpu_result_t (*PFN_gpu_event_synchronize)(gpu_event_t hEvent);
typedef gpu_result_t (*PFN_gpu_device_get_luid)(char *luid, unsigned int *deviceNodeMask,
                                                gpu_device_t dev);

typedef struct GpuDispatch {
    PFN_gpu_init p_init;
    PFN_gpu_get_error_string p_get_error_string;
    PFN_gpu_ctx_get_device p_ctx_get_device;
    PFN_gpu_ctx_synchronize p_ctx_synchronize;
    PFN_gpu_device_get p_device_get;
    PFN_gpu_device_get_attribute p_device_get_attribute;
    PFN_gpu_device_total_mem p_device_total_mem;
    PFN_gpu_device_get_name p_device_get_name;
    PFN_gpu_mem_get_info p_mem_get_info;
    PFN_gpu_mem_alloc_host p_mem_alloc_host;
    PFN_gpu_mem_free_host p_mem_free_host;
    PFN_gpu_mem_host_register p_mem_host_register;
    PFN_gpu_mem_host_unregister p_mem_host_unregister;
    PFN_gpu_mem_address_reserve p_mem_address_reserve;
    PFN_gpu_mem_address_free p_mem_address_free;
    PFN_gpu_mem_create p_mem_create;
    PFN_gpu_mem_map p_mem_map;
    PFN_gpu_mem_set_access p_mem_set_access;
    PFN_gpu_mem_unmap p_mem_unmap;
    PFN_gpu_mem_release p_mem_release;
    PFN_gpu_memcpy_htod_async p_memcpy_htod_async;
    PFN_gpu_event_create p_event_create;
    PFN_gpu_event_destroy p_event_destroy;
    PFN_gpu_event_record p_event_record;
    PFN_gpu_event_synchronize p_event_synchronize;
    PFN_gpu_device_get_luid p_device_get_luid;
} GpuDispatch;

/* 宿主分配方法查询（由 src-xpu/dispatch.c 定义）：L0 页锁定 USM（1）还是 malloc（0）。
 * hostbuf-plat.c 用以决定其 XPU RAM 层缓冲的释放路径（zeMemFree vs free）。
 * 须在至少一次宿主分配调用后读取才有意义。 */
int xpu_host_alloc_is_pinned(void);

extern GpuDispatch g_gpu;
#endif