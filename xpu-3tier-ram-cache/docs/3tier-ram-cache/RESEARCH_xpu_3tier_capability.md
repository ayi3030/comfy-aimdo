# Phase1-A：XPU 三层卸载能力边界研究（H2D 桩影响面 / L0 拷贝实现 / VBAR fault 可达性）

- 任务编号：**#27 Phase1-A**
- 执行人：`intel-xpu-adapter-3`（Intel GPU 适配专家）
- 性质：**只读研究**，未修改任何文件
- 真机：Intel Arc B580（Battlemage / Xe2），Windows
- 部署产物：`comfy_aimdo 0.5.6.dev28`（`__commit_id__ = gc2cf98fb9`）
  证据：`E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/python_embeded/Lib/site-packages/comfy_aimdo/_version.py`

> ⚠️ **本报告最重要的结论（请先读）**：团队下发的「地面真值 #1（部署产物里 H2D 是空桩）」**归属错误**。该桩存在于**上游 `Comfy-Org/comfy-aimdo` 的 `src-xpu/dispatch.c`**，而**部署的 `aimdo_xpu.dll` 根本不是从这棵树编译的**。部署 DLL 来自另一个 fork，其中的 H2D **已用 SYCL 实现且可工作**。因此「H2D 是桩但权重仍进显存」这个表面矛盾**并不存在**——它是把两棵不同的源码树混为一谈造成的。详见 §1、§2。所有结论均给出 `文件:行` 证据，并标注置信度。

---

## 0. 结论速览

| 问题 | 结论 | 置信度 |
|---|---|---|
| (5) 表面矛盾（H2D 是桩为何权重仍进显存） | 矛盾不成立：部署 DLL 的 H2D **不是桩**，是 SYCL `queue->memcpy` 同步实现；`dispatch.c` 的桩只在上游树里，未被编译 | **已核实（PE 导出 + 字符串 + 构建工作流三重证据）** |
| (a) 因桩失效的调用路径 | 桩**仅**影响 2 个 C 调用点（`hostbuf.c:278`、`hostbuf-file-reader.c:86`）。在部署产物中这两点**正常工作**；若真编译了桩，则 safetensors 权重加载会直接抛错 | 已核实（静态）/ 抛错路径需实机复现 |
| (b) L0 正确实现 H2D | 用 `zeCommandListAppendMemoryCopy` 拷进 **VMM 设备 VA 规范允许**（只要求「设备可访问」）；签名已逐字核对 `ze_api.h` | 签名**已核实**；VMM-VA-作-dst 有 B580 驱动小拷贝异常前科，**需实机验证** |
| (c) XPU 是否已有可用拷贝/映射路径 | **有且已在用**：SYCL `queue->memcpy`（当前 H2D）/ SYCL `parallel_for` 内核写 VMM / VMM 显式 map-unmap / USM `malloc_device`。`zeMemAllocHost`-类共享路径被**刻意放弃**（见 §5.3） | 已核实（源码） |
| (d) VBAR 权重落显存路径 | 走 `cast_to_gathered → read_tensor_file_slice_into → read_file_to_device → hostbuf_file_reader_read → cuMemcpyHtoDAsync → SYCL`。**没有绕开 H2D** | **已核实** |
| (e) L0 有无 CUDA VMM page-fault 等价物 | **无用户级 page-fault 引擎**。仅有 `ZE_DEVICE_PROPERTY_FLAG_ONDEMANDPAGING`（驱动托管，用于 USM/shared）+ `zeContextMakeMemoryResident/EvictMemory` + `zeCommandListAppendMemAdvise` | 已核实（头文件） |
| 三层（VRAM→RAM→磁盘） | VRAM=**已可用**；磁盘=**已可用**；RAM=**部分可用/需补齐**（XPU 上 pinning 被 ComfyUI 关掉、`cuMemHostRegister` 是空操作） | VRAM/磁盘已核实；RAM 层**需 Phase1-B + 实机确认** |

---

## 1. 两棵源码树 / 部署产物溯源（解开全部矛盾的地基）

工作区里存在**两棵互不相同、且都被称作“comfy-aimdo 源码”的树**：

| # | 路径 | git remote / 分支 | `src-xpu/` 内容 | H2D 实现 |
|---|---|---|---|---|
| 树 A（“上游”） | `C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/comfy-aimdo-src/` | `Comfy-Org/comfy-aimdo` @ `master`(08b37ee) | **`dispatch.c`（唯一文件，含桩）** | ❌ **空桩**（`dispatch.c:379-396`） |
| 树 B（团队 fork） | `C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/fork/` | `ayi3030/comfy-aimdo` @ `xpu-autoenable`(c2cf98f) | `dispatch.cpp`（73KB）+ `stubs.c` + `ur-usm-*` + `ze-*` | ✅ **SYCL `queue->memcpy`**（`dispatch.cpp:799-928`） |

树 A 的 `git remote -v` 证实其 origin 就是 `https://github.com/Comfy-Org/comfy-aimdo`；树 B 的 origin 是团队工作 fork。

### 1.1 部署的 `aimdo_xpu.dll` 是从树 B 编出来的（三重证据）

**证据 1 — PE 导出表**（实测解析 `site-packages/comfy_aimdo/aimdo_xpu.dll`，共 87 个导出）：

```
aimdo_xpu_copy_host_to_vbar
aimdo_xpu_is_mapped_pinned_vbar
aimdo_xpu_needs_small_vbar_copy_workaround
xpu_alloc_fn / xpu_free_fn / xpu_raw_alloc_fn / xpu_raw_free_fn
xpu_ur_hook_is_interposed / xpu_ur_hook_enable / xpu_ur_hook_disable ...
xpu_set_queues / xpu_get_vmm_stats / xpu_get_total_vram_usage
xpu_allocator_empty_cache / xpu_allocator_get_memory_stats ...
```

这些符号**只存在于树 B 的 `dispatch.cpp`**（`aimdo_xpu_copy_host_to_vbar` 定义在 `aimdo-xpu/fork/src-xpu/dispatch.cpp:1670`）。树 A 的 `dispatch.c` 一个都没有。

**证据 2 — 桩字符串缺失**：全工作区 grep 桩日志 `"host->device copy not implemented"`，命中**仅**两处：`comfy-aimdo-src/src-xpu/dispatch.c` 与 `aimdo-xpu/aimdo-xpu.patch`（即树 A 及其补丁）。部署 DLL 中无此串。

**证据 3 — 构建工作流**：`aimdo-xpu/fork/.github/workflows/build-xpu-windows.yml`
- L50 `COMMUNITY_FORK_REPO: xiangyuT/comfy-aimdo-xpu`，L52 pin SHA `cc3729fc59eeab77bd4c8b28b80e49c9faa855d4`；
- L98-106 `Overlay patched src-xpu/`：`cp -r aimdo/src-xpu/. native/src-xpu/` → **编译的是本 fork 的 `src-xpu/`**；
- L149-151 `Build aimdo_xpu.dll`（`native/scripts/build-windows-xpu.cmd`）。
- 结论：DLL = `xiangyuT` 的 `src/ + src-win/` **+ 团队 fork 的 `src-xpu/`（dispatch.cpp）**。

### 1.2 「地面真值」逐条校正

| 下发的地面真值 | 校正 |
|---|---|
| ① `src-xpu/dispatch.c:379-396` H2D 是空桩 | ✅ 事实成立，但**在树 A**；**部署产物不含该文件** |
| ② 注册点 `dispatch.c:573` → `g_gpu.p_memcpy_htod_async` | ✅ 树 A 事实；**树 B 是 `dispatch.cpp:1869` → `g_cuda.p_cuMemcpyHtoDAsync = xpu_memcpy_host_to_device`** |
| ③ 宏 `plat.h:99` `cuMemcpyHtoDAsync → g_gpu.p_memcpy_htod_async` | ✅ 树 A 的 `plat.h:99`；**树 B 的 `src/plat.h:92` 是 `→ g_cuda.p_cuMemcpyHtoDAsync`（无条件，无 `AIMDO_XPU` 分支）** |
| ④ 两个消费点 `hostbuf.c:278` / `hostbuf-file-reader.c:86` | ✅ **两棵树都成立、且行号一致**（树 A 用 `gpu_result_t`，树 B 用 `CUresult`，逻辑同源） |
| ⑤ 表面矛盾 | ❌ **矛盾不成立**，见 §2 |

> **一句话**：树 A 是一份「上游参考快照」，它的 `src-xpu/dispatch.c` 是上游留的占位后端；真机跑的是树 B 编译的 DLL，H2D 早已补齐。下发材料把树 A 的桩当成了运行时代码。

---

## 2. (5) 表面矛盾的解答

命题：「H2D 是桩 → 权重进不了显存」与「20.97GB+14.95GB staged 在 12GB B580 上跑完」冲突。

**解答：命题的前件为假**——部署 DLL 的 H2D **不是桩**：

```cpp
// aimdo-xpu/fork/src-xpu/dispatch.cpp:799 （部署产物真正编译的实现）
CUresult xpu_memcpy_host_to_device(CUdeviceptr destination, const void *source,
                                   size_t size, CUstream stream) {
    sycl::queue *queue = resolve_queue(stream);
    ...
    queue->memcpy(reinterpret_cast<void *>(destination), source, size)
        .wait_and_throw();          // 同步完成，不做异步
    ...
}
// 注册：
g_cuda.p_cuMemcpyHtoDAsync = xpu_memcpy_host_to_device;   // dispatch.cpp:1869
```

所以权重的确经 H2D 进了显存，链路上没有任何桩。

**并且存在第二条独立解释（即使桩存在也未必死）**：`ComfyUI/comfy/model_management.py:1548-1570` 的 `cast_to_gathered` 逻辑是「先试 aimdo 文件切片快路径，返回 False 则**回落 torch 自己的 `copy_`**」：

```python
if comfy.memory_management.read_tensor_file_slice_into(tensor, dest_view, ...):
    continue
...
dest_view.copy_(tensor, non_blocking=non_blocking)   # 回落：torch 原生 XPU 拷贝
```

即「设备侧权重落位」有 **aimdo H2D** 与 **torch 原生拷贝** 两条路。二者都不依赖 `dispatch.c` 的桩。

> **附带发现（对本矛盾进一步佐证）**：safetensors 权重在加载时会在存储上打 `_comfy_tensor_file_slice` 标记（`ComfyUI/comfy/utils.py:150-152`），因此会**优先**走 aimdo 快路径 → 真正调用到 H2D。这也从反面证明：若 H2D 真是桩，实测会直接抛错而不是“静默成功”。实测跑通 ⇒ H2D 有效。

**结论**：矛盾由「源码树张冠李戴」产生，非系统性缺陷。置信度：**已核实**。

---

## 3. (a) 因该桩而失效的全部调用路径清单

### 3.1 C 层受影响点（全仓库仅此 2 处调用 `cuMemcpyHtoDAsync`）

grep 结果（树 B，树 A 同构，仅类型名不同）：

| # | 文件:行 | 函数 | 触发条件 | 桩存在时的行为 |
|---|---|---|---|---|
| 1 | `src/hostbuf.c:278` | `hostbuf_read_file_slice()` | 入参 `device_ptr != 0` 且 `size > 0`、`device >= 0`；按 `HOSTBUF_STREAM_WINDOW=64MiB` 分窗 | `CHECK_CU(cuMemcpyHtoDAsync(...))` 判假 → 函数 `return false` |
| 2 | `src/hostbuf-file-reader.c:86` | `hostbuf_file_reader_read()` | 入参 `device_ptr != 0` 且 `device >= 0`；按 `HOSTBUF_FILE_READER_WINDOW=64MiB` 分窗 | 同上 → `return false` |

（`hostbuf_grow`/`hostbuf_register`/`hostbuf_truncate` 会调 `cuMemHostRegister/Unregister`——这些在 XPU 上是**空操作**但**不经过 H2D 桩**，故不在本清单内，见 §5.2。）

### 3.2 Python 层触发链（自顶向下）

```
ComfyUI/comfy/ops.py:128  cast_modules_with_vbar()
  └─ ops.py:182  xfer_dest = comfy_aimdo.torch.aimdo_to_tensor(s._v, device)   # VBAR VA → torch 张量视图
  └─ ops.py:226  comfy.model_management.cast_to_gathered(xfer_source, xfer_dest, ...)
       └─ model_management.py:1563  comfy.memory_management.read_tensor_file_slice_into(...)
            ├─ (destination2 为设备张量) memory_management.py:59  read_file_to_device(...)
            │     └─ comfy_aimdo/host_buffer.py:67  → lib.hostbuf_file_reader_read   ← C 点 #2
            └─ (destination 为 CPU+HOSTBUF) memory_management.py:70  hostbuf.read_file_slice(...)
                  └─ comfy_aimdo/host_buffer.py:104  → lib.hostbuf_read_file_slice  ← C 点 #1
```

### 3.3 分支判据（哪条真正被走）

- `memory_management.py:40-42`：若 `destination.device.type != "cpu"` 且 `destination2 is None`，则把 `destination2 = destination`、`destination = None` → 走 **C 点 #2**（`read_file_to_device`）。XPU 上 VBAR 目标张量是设备张量，故 **#2 是 XPU 的主路径**。
- `memory_management.py:65-74`：仅当目标存储带 `_comfy_hostbuf`（由 `comfy/pinned_memory.py:104` 设置）才走 **C 点 #1**。
- 早退条件 `memory_management.py:36-52`：张量存储无 `_comfy_tensor_file_slice` 或不满足连续性/尺寸/偏移约束 → 直接 `return False`，**根本不碰 H2D**，由 `cast_to_gathered` 回落 torch `copy_`。

### 3.4 部署产物中的实际行为

| 场景 | 结果 |
|---|---|
| 部署 dev28 DLL（树 B） | C 点 #1/#2 的 `cuMemcpyHtoDAsync` → `dispatch.cpp:1869` 的 SYCL 实现 → **正常工作**（同步阻塞完成） |
| 若误用树 A 的桩编译 | C 点 #1/#2 返回 false → `read_file_to_device`/`read_file_slice` 抛 `RuntimeError` → 权重加载失败（正是桩日志所说 `hostbuf file->VRAM loading disabled`） |

**结论**：桩的真实影响面**仅这 2 个 C 点**；在部署产物中**无失效路径**。置信度：静态**已核实**；「桩会抛错」这一断言建议在实机用一次故意替换复现（见 §11 待查证）。

---

## 4. (b) 正确实现 host→device 异步拷贝的 Level Zero 做法

### 4.1 前置说明：当前实现为什么“不异步”

树 B 的 `xpu_memcpy_host_to_device`（`dispatch.cpp:799-928`）用 **SYCL `queue->memcpy().wait_and_throw()`**，是**同步**的。函数名里的 `Async` 只是兼容 CUDA 命名。它还有个 8MiB 分片重试（`dispatch.cpp:829-850`）应对「大页拷贝导致运行时内部 staging 超 WDDM 预算 → 假 OOM」。事件 API（`xpu_event_record`）因此变成「已完成哨兵」（`dispatch.cpp:947-958` 注释明说）。

**要不要改成真正的 L0 异步**，取决于是否需要与文件读取重叠。下面给出规范做法。

### 4.2 Level Zero 异步 H2D 的完整调用序列

标准流程（与 CUDA 的 `cuMemcpyHtoDAsync` 语义对应）：

```
① zeContextCreate                       // 已有：树 A dispatch.c:528 / 树 B 由 ze-init 建立
② zeDeviceGetCommandQueueGroupProperties // 取一个支持 copy 的 queue group ordinal
③ zeCommandQueueCreate(context, device, &{stype=0x0E, ordinal=copy_group}, &queue)
④ zeCommandListCreate(context, device, &{stype=0x0F, commandQueueGroupOrdinal=copy_group}, &list)
⑤ zeEventPoolCreate + zeEventCreate      // 可选的 signal/wait 事件
⑥ zeCommandListAppendMemoryCopy(list, dst, src, size, hSignalEvent, 0, nullptr)
⑦ zeCommandListClose(list)
⑧ zeCommandQueueExecuteCommandLists(queue, 1, &list, hFence_or_null)
⑨ zeEventHostSynchronize(hEvent, UINT64_MAX)   // 或 zeCommandQueueSynchronize(queue, UINT64_MAX)
   完成后回收 list/queue/event
```

> 「异步」在 L0 里是相对 **host 线程**：`⑧` 立即返回，用 `⑨` 或 fence/event 等待。若要「H2D 与下一次磁盘读重叠」，可在提交后不等、先发起下一次 `xfer_file_read`，再在复用 host staging 前 `⑨`（对应 `hostbuf.c:230-288` 现有的 2-slot 流水注释）。

### 4.3 逐字核对的真实签名（来源：`oneapi-src/level-zero` `include/ze_api.h`）

出处 URL：`https://github.com/oneapi-src/level-zero/blob/master/include/ze_api.h`
（本地核对文件：`/tmp/ze_api.h`，21568 行，`ZE_API_VERSION_CURRENT = ZE_MAKE_VERSION(1, 19)`；与 CI pin 的 loader tag `v1.34.0` 抓取到的 `ze_api.h` 字节一致）

```c
// ze_api.h:3588
ze_result_t ZE_APICALL
zeCommandQueueCreate(
    ze_context_handle_t hContext,           ///< [in] handle of the context object
    ze_device_handle_t hDevice,             ///< [in] handle of the device object
    const ze_command_queue_desc_t* desc,    ///< [in] pointer to command queue descriptor
    ze_command_queue_handle_t* phCommandQueue);///< [out] pointer to handle of command queue object created

// ze_api.h:3684
ze_result_t ZE_APICALL
zeCommandQueueExecuteCommandLists(
    ze_command_queue_handle_t hCommandQueue,///< [in] handle of the command queue
    uint32_t numCommandLists,               ///< [in] number of command lists to execute
    ze_command_list_handle_t* phCommandLists,///< [in][range(0,numCommandLists)] list of handles
    ze_fence_handle_t hFence);              ///< [in][optional] handle of the fence to signal on completion

// ze_api.h:3718
ze_result_t ZE_APICALL
zeCommandQueueSynchronize(
    ze_command_queue_handle_t hCommandQueue,///< [in] handle of the command queue
    uint64_t timeout);                      ///< [in] ns; 0 = poll, UINT64_MAX = block until done/LOST

// ze_api.h:3983
ze_result_t ZE_APICALL
zeCommandListCreate(
    ze_context_handle_t hContext,           ///< [in] handle of the context object
    ze_device_handle_t hDevice,             ///< [in] handle of the device object
    const ze_command_list_desc_t* desc,     ///< [in] pointer to command list descriptor
    ze_command_list_handle_t* phCommandList);///< [out] pointer to handle of command list object created

// ze_api.h:4100
ze_result_t ZE_APICALL
zeCommandListClose(
    ze_command_list_handle_t hCommandList); ///< [in] handle of command list object to close

// ze_api.h:4727   ★ 核心拷贝 API
ze_result_t ZE_APICALL
zeCommandListAppendMemoryCopy(
    ze_command_list_handle_t hCommandList,  ///< [in] handle of command list
    void* dstptr,                           ///< [in] pointer to destination memory to copy to
    const void* srcptr,                     ///< [in] pointer to source memory to copy from
    size_t size,                            ///< [in] size in bytes to copy
    ze_event_handle_t hSignalEvent,         ///< [in][optional] handle of the event to signal on completion
    uint32_t numWaitEvents,                 ///< [in][optional] number; must be 0 if nullptr==phWaitEvents
    ze_event_handle_t* phWaitEvents);       ///< [in][optional][range(0,numWaitEvents)] events to wait on

// ze_api.h:5758
ze_result_t ZE_APICALL
zeEventCreate(
    ze_event_pool_handle_t hEventPool,      ///< [in] handle of the event pool
    const ze_event_desc_t* desc,            ///< [in] pointer to event descriptor
    ze_event_handle_t* phEvent);            ///< [out] pointer to handle of event object created

// ze_api.h:5507
ze_result_t ZE_APICALL
zeEventPoolCreate(
    ze_context_handle_t hContext,           ///< [in] handle of the context object
    const ze_event_pool_desc_t* desc,       ///< [in] pointer to event pool descriptor
    uint32_t numDevices,                    ///< [in][optional] number of device handles
    ze_device_handle_t* phDevices,          ///< [in][optional][range(0,numDevices)] device handles
    ze_event_pool_handle_t* phEventPool);   ///< [out] pointer handle of event pool object created

// ze_api.h:6428
ze_result_t ZE_APICALL
zeEventHostSynchronize(
    ze_event_handle_t hEvent,               ///< [in] handle of the event
    uint64_t timeout);                      ///< [in] ns; 0 = query, UINT64_MAX = block until done/LOST

// ze_api.h:7619   ★ 共享宿主/固定内存（当前实现刻意未用，见 §5.3）
ze_result_t ZE_APICALL
zeMemAllocHost(
    ze_context_handle_t hContext,
    const ze_host_mem_alloc_desc_t* host_desc,
    size_t size, size_t alignment, void** pptr);

// ze_api.h:7566
ze_result_t ZE_APICALL
zeMemAllocDevice(
    ze_context_handle_t hContext,
    const ze_device_mem_alloc_desc_t* device_desc,
    size_t size, size_t alignment,
    ze_device_handle_t hDevice, void** pptr);

// ze_api.h:7513
ze_result_t ZE_APICALL
zeMemAllocShared(
    ze_context_handle_t hContext,
    const ze_device_mem_alloc_desc_t* device_desc,
    const ze_host_mem_alloc_desc_t* host_desc,
    size_t size, size_t alignment,
    ze_device_handle_t hDevice, void** pptr);

// ze_api.h:10215
ze_result_t ZE_APICALL
zeVirtualMemReserve(ze_context_handle_t hContext, const void* pStart,
                    size_t size, void** pptr);
// ze_api.h:10410
ze_result_t ZE_APICALL
zePhysicalMemCreate(ze_context_handle_t hContext, ze_device_handle_t hDevice,
                    ze_physical_mem_desc_t* desc, ze_physical_mem_handle_t* phPhysicalMemory);
// ze_api.h:10494
ze_result_t ZE_APICALL
zeVirtualMemMap(ze_context_handle_t hContext, const void* ptr, size_t size,
                ze_physical_mem_handle_t hPhysicalMemory, size_t offset,
                ze_memory_access_attribute_t access);
```

**相关结构体（逐字）**：

```c
// ze_api.h（命令队列/命令表/事件/物理内存 描述符）
typedef struct _ze_command_queue_desc_t {
    ze_structure_type_t stype;   const void* pNext;
    uint32_t ordinal;            uint32_t index;          // index 必须为 0
    ze_command_queue_flags_t flags;                        // uint32_t
    ze_command_queue_mode_t mode; ze_command_queue_priority_t priority;
} ze_command_queue_desc_t;

typedef struct _ze_command_list_desc_t {
    ze_structure_type_t stype;   const void* pNext;
    uint32_t commandQueueGroupOrdinal;  ze_command_list_flags_t flags;
} ze_command_list_desc_t;

typedef struct _ze_event_desc_t {
    ze_structure_type_t stype; const void* pNext;
    uint32_t index; ze_event_scope_flags_t signal; ze_event_scope_flags_t wait;
} ze_event_desc_t;

typedef struct _ze_event_pool_desc_t {
    ze_structure_type_t stype; const void* pNext;
    ze_event_pool_flags_t flags; uint32_t count;
} ze_event_pool_desc_t;

typedef struct _ze_host_mem_alloc_desc_t {
    ze_structure_type_t stype; const void* pNext; ze_host_mem_alloc_flags_t flags;
} ze_host_mem_alloc_desc_t;

typedef struct _ze_device_mem_alloc_desc_t {
    ze_structure_type_t stype; const void* pNext; ze_device_mem_alloc_flags_t flags; uint32_t ordinal;
} ze_device_mem_alloc_desc_t;

typedef struct _ze_physical_mem_desc_t {
    ze_structure_type_t stype; const void* pNext; ze_physical_mem_flags_t flags; size_t size;
} ze_physical_mem_desc_t;

// 不透明句柄
typedef struct _ze_command_queue_handle_t* ze_command_queue_handle_t;
typedef struct _ze_command_list_handle_t*  ze_command_list_handle_t;
typedef struct _ze_event_handle_t*         ze_event_handle_t;
typedef struct _ze_event_pool_handle_t*    ze_event_pool_handle_t;
typedef struct _ze_physical_mem_handle_t*  ze_physical_mem_handle_t;
typedef struct _ze_fence_handle_t*         ze_fence_handle_t;
```

**关键枚举值（逐字，`ze_structure_type_t`）**：

```
ZE_STRUCTURE_TYPE_DEVICE_MEMORY_PROPERTIES = 0x7   // 与树 A dispatch.c:59 / :157 一致
ZE_STRUCTURE_TYPE_CONTEXT_DESC             = 0xd   // 与树 A dispatch.c:58 一致
ZE_STRUCTURE_TYPE_COMMAND_QUEUE_DESC       = 0xe   // ★ 新增 H2D 通路需要
ZE_STRUCTURE_TYPE_COMMAND_LIST_DESC        = 0xf   // ★
ZE_STRUCTURE_TYPE_EVENT_POOL_DESC          = 0x10  // ★
ZE_STRUCTURE_TYPE_EVENT_DESC               = 0x11  // ★
ZE_STRUCTURE_TYPE_DEVICE_MEM_ALLOC_DESC    = 0x15
ZE_STRUCTURE_TYPE_HOST_MEM_ALLOC_DESC      = 0x16
ZE_STRUCTURE_TYPE_PHYSICAL_MEM_DESC        = 0x20  // 与树 A dispatch.c:57 一致
```

（顺带证实：树 A `dispatch.c:46-59` 里对 stype=0x20 / 0x0D 的“关键修正”与上游头文件一致；`ZE_PHYSICAL_MEM_FLAG_ALLOCATE_ON_DEVICE = 0x1`、`ZE_MEMORY_TYPE_DEVICE=2`、`ZE_MEMORY_ACCESS_ATTRIBUTE_READWRITE=1` 亦一致。）

### 4.4 ★ `zeVirtualMemMap` 得到的 VMM 设备 VA 能否作为 `zeCommandListAppendMemoryCopy` 的目标？

**规范层面：可以。** `zeCommandListAppendMemoryCopy` 的 @details（`ze_api.h:4682-4699`）只要求：

> "The application must ensure the memory pointed to by **dstptr and srcptr is accessible by the device** on which the command list was created."

VMM 设备 VA（`zePhysicalMemCreate(ALLOCATE_ON_DEVICE) + zeVirtualMemMap` 得到）是**设备物理内存映射而来的地址**，天然「设备可访问」；规范**未**把 `dstptr` 限定为 USM 分配。因此规范**允许**直接将文件内容拷进 VMM 设备 VA。
**置信度：高（规范文本明确）。**

**实机层面：有已知坑，必须验证。** 树 B 的历史证据表明在 Windows/B580 类驱动上，把拷贝**直接打到 VBAR VMM 映射**会出现异常：
- `dispatch.cpp:1657-1665` 注释：**「≤2MiB 的 host→VMM 拷贝会被拒 `ZE_RESULT_ERROR_OUT_OF_DEVICE_MEMORY`，而 2MiB+1 字节却成功」**；
- 因此引入内核写回兜底 `aimdo_xpu_copy_host_to_vbar`（`dispatch.cpp:1670-1746`）：`malloc_host → memset → padded H2D 到 staging → parallel_for 内核逐字节写 VMM`；
- `dispatch.cpp:1760-1766` 还记录「一旦首次小拷贝被拒，连正常 padded H2D 也报 device busy」，故用 `aimdo_xpu_needs_small_vbar_copy_workaround(device)` 预判（按 `node_mask != 1` 判定被污染的适配器）。

**结论**：规范允许 VMM VA 作 dst；但 **B580 驱动对「直接拷贝进 VMM 映射」存在尺寸相关的拒绝行为**，工程上应继续沿用「内核对 VMM 映射写回」的既有兜底，或在 Windows 上先 `zeCommandListAppendMemoryCopy` 到 staging 再用内核写 VMM。**置信度：规范已核实 / 驱动行为需实机验证。**

**顺带**：树 B 的 H2D 用的是 **SYCL `queue->memcpy`**，而 SYCL 对「非 USM 的裸 VMM VA」没有保证——这正是它在失败分支里用 `sycl::get_pointer_type` 打出 `dest_kind=not_usm_vmm`（`dispatch.cpp:880-887`）来排查的原因。若要更稳的 H2D，**L0 原生命令表**（明确设备可访问语义 + 可配 staging）比 SYCL memcpy 更可控。

---

## 5. (c) XPU 后端里已有的其它可用拷贝 / 映射路径（逐一判定）

证据集中在 `aimdo-xpu/fork/src-xpu/dispatch.cpp` 与 `src-xpu/stubs.c`。

| 路径 | 实现位置 | 机制 | 可用性判定 | 备注 |
|---|---|---|---|---|
| **C1. SYCL 同步 H2D** | `dispatch.cpp:799-928` | `queue->memcpy(...).wait_and_throw()`，>8MiB 失败时 8MiB 分片重试 | ✅ **当前主力，已用于生产** | 同步；名字里的 Async 名不副实；用 SYCL 非 L0 |
| **C2. 内核写 VMM** | `dispatch.cpp:1670-1746` `aimdo_xpu_copy_host_to_vbar` | `malloc_host` → H2D 到 `malloc_device` staging → `parallel_for` 逐字节写目标 | ✅ 可用（Windows 专用兜底） | 仅处理 ≤2MiB；`#if _WIN32`；注释说内核可写 VMM 映射 |
| **C3. VMM 显式 map/unmap** | `dispatch.cpp:616-797`（`xpu_virtual_reserve/ physical_create/ virtual_map/ virtual_unmap/ physical_release`） | `zeVirtualMemReserve`→`zePhysicalMemCreate`→`zeVirtualMemMap` | ✅ 可用（VBAR 机制基础，真机验证通过） | 与树 A `dispatch.c` 同源思路，树 B 改用 SYCL 拿到 `queue→context/device` |
| **C4. USM 设备分配** | `dispatch.cpp:528/558`（`xpu_malloc`/`xpu_malloc_async`）、`dispatch.cpp:1025/1146`（torch/raw 段） | `sycl::malloc_device` | ✅ 可用 | 给 torch 分配器用；也可作 H2D staging |
| **C5. USM/Host 分配（pinned 语义）** | `dispatch.cpp:589-604` `xpu_host_alloc/free` | **`std::malloc`（pageable）**，非 `sycl::malloc_host` | ⚠️ **刻意放弃 pinned** | 注释（`dispatch.cpp:593-596`）：「改用 `sycl::malloc_host` 后失败更糟——64MiB 拷贝从 `OUT_OF_DEVICE_MEMORY` 变 `DEVICE_LOST` 且提前 56s」。故 H2D 从 pageable 源拷贝由运行时自建 staging |
| **C6. `cuMemHostRegister`（固定宿主）** | `dispatch.cpp:606-614` `xpu_host_register/unregister` | **空操作返回 SUCCESS** | ⚠️ **未真正 fixed/pinned** | 注释（`dispatch.cpp:607-608`）：「XPU phase 1 关闭宿主 pinning，copy 一律走 SYCL queue」。⇒ `hostbuf.c:73` 的 `cuMemHostRegister` 在 XPU 上不产生固定内存 |
| **C7. `hostbuf_decommit`（RAM 回收）** | `src/hostbuf-decommit.c`（跨平台） + `src-win/hostbuf-plat.c` | 后台线程 `hostbuf_decommit_address_space/release_address_space` 归还宿主页 | ✅ 可用 | 与 H2D 无关；属 RAM 层能力（见 §8） |
| **C8. UR-USM 分配拦截（native_hook）** | Windows：`src-xpu/ur-usm-detour.c`（Detours 钩住 `ur_loader!urUSMDeviceAlloc`）；Linux：`src-xpu/ur-usm-hook.cpp`（LD_PRELOAD） | 在分配点做 AIMDO 预算仲裁 | ✅ 可用（Windows 默认模式） | 只管**分配**不管拷贝；`ur-usm-hook.cpp:15-16` 对 Windows `#error`，故 Windows 走 detour 版 |
| **C9. `zeCommandQueue` 同步拷贝** | ❌ 未实现 | — | ⛔ **不存在** | 全树 grep `zeCommandList*`/`zeCommandQueue*` 在 `src-xpu/` 内**零命中**（唯一命中是 `dispatch.cpp` 无）。这是 §4 建议新增的能力 |

**判定汇总**：
- 「把文件内容送进显存」**当前已有可用路径**——C1（+ C2 兜底）。**不需要**为了「能跑」而实现 L0 命令表。
- 「**真异步** H2D 与磁盘读重叠」**没有**——C1 是同步的。这才是 §4 的真正动机（性能项，非阻塞项）。
- 「pinned/USM-host 快速路径」**被刻意关闭**（C5/C6），是潜在优化点但**有反例风险**（改回会 DEVICE_LOST）。

---

## 6. (d) VBAR 的加载路径追溯（关键：是否绕开 H2D 桩）

**结论：没有绕开 H2D，而且根本不经过 `dispatch.c` 的桩。**

### 6.1 VBAR 分配（只“圈地”，不搬数据）

1. `ComfyUI/comfy/model_patcher.py:1811`
   `vbar = comfy_aimdo.model_vbar.ModelVBAR(self.model_size() * 10, self.load_device.index)`
2. `comfy_aimdo/model_vbar.py:423` `ModelVBAR.__init__` → `lib.vbar_allocate(...)` → C `vbar_allocate`（`src/model-vbar.c:217`）
   → `cuMemAddressReserve(&mv->vbar, size, ...)`（`model-vbar.c:241`）**只保留虚拟地址**。
3. `model_patcher.py:1993` `m._v = vbar.alloc(v_weight_size)` → 从 VBAR 切一段 VA（`model_vbar.py:500-508`），得 `(vbar, alloc_ptr, size)`。

### 6.2 VBAR “fault”（把物理显存页映射进 VBAR VA）

4. 用权重时 `comfy/ops.py:168` / `comfy/model_prefetch.py:205` 调 `comfy_aimdo.model_vbar.vbar_fault(s._v)`
   → `model_vbar.py:526` `lib.vbar_fault(...)` → C `vbar_fault`（`src/model-vbar.c:346`）
   → `three_stooges(vaddr, VBAR_PAGE_SIZE, device, &handle)`（`model-vbar.c:397`）
   → `cuMemCreate/cuMemMap/cuMemSetAccess`（`src/plat.h:219-257`）
   → XPU 版 `xpu_physical_create/xpu_virtual_map`（`dispatch.cpp:641/684`）→ `zePhysicalMemCreate/zeVirtualMemMap`。
   **注意：这一步只“分配并映射”物理页，没有任何数据搬入。**

### 6.3 权重的真正落位（这里用到 H2D）

5. `comfy/ops.py:182` `xfer_dest = comfy_aimdo.torch.aimdo_to_tensor(s._v, device)`
   → `comfy_aimdo/torch.py:19` `_construct_storage_from_data_pointer(vbar_va, device, size)` 把 VBAR VA 包成 **torch 张量**。
6. `comfy/ops.py:226` `cast_to_gathered(xfer_source, xfer_dest, ...)` → `model_management.py:1563` `read_tensor_file_slice_into`
7. → `memory_management.py:59` `comfy_aimdo.host_buffer.read_file_to_device(file_obj, offset, size, stream_ptr, destination2.data_ptr(), ...)`
   → `comfy_aimdo/host_buffer.py:68` `lib.hostbuf_file_reader_read(...)`
   → C `hostbuf_file_reader_read`（`src/hostbuf-file-reader.c:86`）
   → **`cuMemcpyHtoDAsync(device_ptr, slot->buffer, chunk, stream)`**
   → 树 B：`g_cuda.p_cuMemcpyHtoDAsync = xpu_memcpy_host_to_device`（`dispatch.cpp:1869`）→ **SYCL `queue->memcpy`**。

### 6.4 关于输入源（为何通常走“文件”而非“HostBuffer”）

- ComfyUI 的 XPU 路径上宿主 pinning 实际未启用：`ComfyUI/comfy/model_management.py:1608` `MAX_PINNED_MEMORY = -1`，且只有 `is_nvidia() or is_amd()` 才会设正值（`:1637`）；XPU 不满足 → `pin_memory()`（`:1665`）恒返回 False。
- 于是 `ops.py:190` `get_pin(...)` 得 None → `xfer_source = [s.weight]`（`ops.py:187`），即 safetensors 文件切片张量（带 `_comfy_tensor_file_slice`，见 `comfy/utils.py:150-152`）。
- ⇒ **XPU 的权重落位主链 = 文件切片 → aimdo H2D → VBAR 显存**。`HostBuffer`（`weights`/`weights-loaded` 子集，`model_patcher.py:1883-1888`）在 XPU 上很可能**未被实际使用**（pinning 关掉），这直接呼应「没有内存这步」的现象（**待 Phase1-B 确认**）。

**对本任务的意义**：VBAR 路径**依赖 H2D**，且**依赖的是树 B 的 SYCL 实现**，与 `dispatch.c` 的桩无关。（附带印证 `三.4`：桩若生效，safetensors 权重会加载失败。）

---

## 7. (e) Level Zero 是否有 CUDA VMM page-fault 的等价物？

**结论：没有用户可控的 page-fault 引擎。** 先厘清概念：

- CUDA 侧，AIMDO 的 VBAR 用的是**显式** `cuMemCreate/cuMemMap/cuMemUnmap`（手动换入换出），**不是** GPU 页错误；所谓 `vbar_fault` 是 **Python 主动调用**的“软件 fault”。
- CUDA 真正的 GPU 页错误来自 **UVM/managed memory**（`cudaMallocManaged` + `cudaMemAdvise` + `cudaMemPrefetchAsync`），由驱动按需迁移。

L0 头文件证据：

| 机制 | 证据（`ze_api.h`） | 说明 |
|---|---|---|
| 设备属性标志 | `:2004` `ZE_DEVICE_PROPERTY_FLAG_ONDEMANDPAGING = ZE_BIT(3)` —— "Device supports on-demand page-faulting." | **存在**，但是**驱动的托管能力**（用于 runtime 管理的 USM/shared），**不向应用暴露容错回调**；应用无法借此自己捕获“缺页并补数据” |
| 显式驻留控制 | `:9924` `zeContextMakeMemoryResident` / `:9962` `zeContextEvictMemory` | 手动「令 X 常驻 / 允许驱逐」，**需要应用自己搬数据**，等价于 CUDA 的 `cudaMemAdvise/cudaMemPrefetch` 的“建议”语义 |
| 建议接口 | `:5427` `zeCommandListAppendMemAdvise(..., ze_memory_advice_t)`，枚举见 `:5363 ze_memory_advice_t`（`SET_PREFERRED_LOCATION`/`BIAS_CACHED`/…） | 仅**提示**，无硬保证 |
| VMM 访问属性 | `:10581` `zeVirtualMemSetAccessAttribute` | 改 VMM 段权限（RO/RW/none），可用于“取消映射”语义 |
| **缺失** | —— | 无「安装缺页处理回调 / 由内核触发 host 侧补数据」的 API（CUDA 也没有面向 VMM 的用户回调，但 UVM 由驱动全程托管） |

**最接近的替代机制与代价**：

1. **驱动托管 on-demand paging（`F_OFFDEMANDPAGING`）**：代价——(a) 仅对 **USM/shared** 分配生效，**不适合** AIMDO 的 VMM-BAR 显存模型；(b) 无用户回调，AIMDO 无法在换入时“从文件补数据/记账”；(c) 需要 `zeMemAllocShared` 且**放弃显存上限的硬控制**——与「显存预算/驱逐策略由 AIMDO 掌控」的核心冲突。
2. **显式 `Make/EvictMemoryResident` + 软件 fault**（**即现状**）：代价——无透明迁移，**每次重新 fault 都必须重新从宿主/文件拷回**（也就是 §6.3 的 H2D）。这正是「VRAM→RAM→磁盘」想要的层次，但**迁移要自己写**。

**对三层设计的意义**：XPU 上**不要指望**“缺页自动回迁”。三层必须建立在 **AIMDO 自己的软件策略**（`vbar_fault/free_memory/watermark`）之上——这与树 B 现状一致，可行但需自行实现「RAM 缓存层」。

---

## 8. 三层（VRAM → RAM → 磁盘）在 XPU 上的可行性结论

| 层 | 现状 | 判定 | 关键证据 / 缺口 |
|---|---|---|---|
| **L1 VRAM（VBAR VMM）** | 显式 map/unmap，软件 fault | ✅ **已可用** | `model-vbar.c:346` `vbar_fault`→`three_stooges`→`dispatch.cpp:641/684` L0 VMM；dev28 真机跑通 |
| **磁盘（safetensors 文件）** | `xfer_file_read` 8 线程 + `model_mmap` + 文件切片游标 | ✅ **已可用** | `src/xfer-file.c:77`、`comfy/utils.py:133-152`、`model_management.py:1556-1570` |
| **L2 RAM（host 缓存层）** | `HostBuffer`（reserve/commit/prewarm/decommit）**在 XPU 上可能未被使用** | ⚠️ **部分可用 / 需补齐** | 代码在（`src/hostbuf.c`、`src/hostbuf-decommit.c` 跨平台）；但 pinning 关掉（`model_management.py:1608/1637`），`cuMemHostRegister` 空操作（`dispatch.cpp:606`），`xpu_host_alloc` 是 pageable（`dispatch.cpp:597`）⇒ 「weights/weights-loaded」HostBuffer 很可能闲置。**需 Phase1-B + 实机确认** |
| **VRAM↔RAM 拷贝（H2D）** | SYCL 同步拷贝（+ 内核兜底） | ✅ **已可用（但同步）** | `dispatch.cpp:799`；同步导致与磁盘读难以重叠 |

**对齐用户诉求**：「当前只有两层 / 显存不足直接取硬盘、没有内存这步」——从代码看**很可能属实**：
- eviction 只做 `cuMemUnmap + cuMemRelease`（`model-vbar.c:99-105` `mod1`），**只丢显存页，不落任何 host 侧缓存**；
- 重新使用时要么从**仍驻留的 HostBuffer**（可能不存在）要么**从文件重读**（`weights-fast`/文件切片）；
- 再叠加「pinning 关掉 + `cuMemHostRegister` 空操作」⇒ 中间那层 RAM 缓存**实际上没建立**，等价于「释放显存后回文件」。

---

## 9. 分期实现建议

> 说明：以下为**建议**，不含实测性能数据；工作量级用「人日」粗估，风险点已标注。

### 9.1 必须项（P0，解锁“真三层”）

| # | 事项 | 工作量 | 风险 | 说明 |
|---|---|---|---|---|
| P0-1 | **确认 L2 RAM 层是否真的闲置**（Phase1-B 交界） | 0.5–1d | 低 | 打印/探针 `get_pin` 命中率、`HostBuffer` 使用字节；确认 `weights` 子集是否恒空 |
| P0-2 | **若 L2 闲置：在 XPU 上启用一个真正的 RAM 缓存层** | 3–6d | **中高** | 两条路线：(a) 恢复 ComfyUI pinning（放开 `MAX_PINNED_MEMORY` 的 XPU 分支）+ 让 `xpu_host_register` 真正固定（用 `zeMemAllocHost`，但见 P0-2 反例）；(b) 不走 CUDA `cuMemHostRegister`，改为 **native 侧自管 hostbuf + XPU 拷贝**（绕开 `torch.cuda.cudart()`，`pinned_memory.py:105` 在 XPU 上会失败）。**反例警告**：`dispatch.cpp:593-596` 记录改 pinned 反而 DEVICE_LOST，务必小步验证 |
| P0-3 | **驱逐时落 RAM 而非直接回文件** | 2–4d | 中 | 当前 `mod1`（`model-vbar.c:99`）只 unmap；需在 unmap 前把页数据写回一个 XPU 可用 host 缓存，fault 时优先从 RAM 回填 |

### 9.2 可选项（P1，性能向）

| # | 事项 | 工作量 | 风险 | 说明 |
|---|---|---|---|---|
| P1-1 | **L0 原生命令表实现真异步 H2D** | 2–4d | 中 | 按 §4.2/§4.3 组合 `zeCommandQueue/List/Event`；收益是与 `xfer_file_read` 重叠（现为同步）。**须保留 C2 内核兜底**（≤2MiB 拒拷贝 / 污染适配器）。签名务必用 §4.3 原文。|
| P1-2 | **多 queue 并行 H2D** | 1–3d | 中 | 与 `HOSTBUF_FILE_READER_SLOTS`（2-slot）配套 |
| P1-3 | **H2D 直达 VMM 的可行性摸底** | 1–2d | 中 | 验证 §4.4：规范允许，但 B580 有前科；测 ≤2MiB/2MiB+1/大块三档，决定是否去掉内核兜底 |
| P1-4 | **`zeMemAllocHost` 固定暂存** | 1–2d | **高** | 直接触碰已知反例（DEVICE_LOST）；仅在 P1-1 完成后、有回滚的条件下尝试 |

### 9.3 不建议做

- ❌ 去“修” `dispatch.c` 的 H2D 桩 —— 部署产物**根本没有编译它**；改它不会影响真机。
- ❌ 依赖 L0 on-demand paging 做自动回迁 —— 见 §7，模型不匹配。
- ❌ 在 Windows 上用 SYCL `queue->memcpy` 直拷小尺寸进 VBAR VMM 而不走内核兜底 —— 已知被拒（`dispatch.cpp:1657-1665`）。

---

## 10. 证据索引（文件:行）

**部署产物**
- `E:/aiwork/.../site-packages/comfy_aimdo/_version.py` → `0.5.6.dev28`, `gc2cf98fb9`
- PE 导出（实测）：`aimdo_xpu_copy_host_to_vbar` 等 87 项

**树 A（上游，含桩，未被编译）**
- `comfy-aimdo-src/src-xpu/dispatch.c:379-396` 桩；`:573` 注册 `g_gpu.p_memcpy_htod_async`；`:46-59` stype 常量
- `comfy-aimdo-src/src/plat.h:99` `cuMemcpyHtoDAsync → g_gpu.p_memcpy_htod_async`；`:219-257` `three_stooges`
- `comfy-aimdo-src/src/hostbuf.c:278`、`src/hostbuf-file-reader.c:86`

**树 B（团队 fork，部署产物来源）**
- `aimdo-xpu/fork/.github/workflows/build-xpu-windows.yml:50,52,98-106,149-151,210-297`
- `aimdo-xpu/fork/src-xpu/dispatch.cpp:799-928`（H2D）；`:1657-1746`（小 VBAR 兜底）；`:589-614`（host alloc/register 空操作）；`:641-797`（VMM）；`:1818-1877`（注册，含 `:1869` H2D）；`:880-887`（dest_kind 诊断）
- `aimdo-xpu/fork/src/plat.h:92` `cuMemcpyHtoDAsync → g_cuda.p_cuMemcpyHtoDAsync`
- `aimdo-xpu/fork/src/hostbuf.c:73,278`；`src/hostbuf-file-reader.c:43,86`
- `aimdo-xpu/fork/src-xpu/ur-usm-detour.c:1-32`；`ur-usm-hook.cpp:15-16`；`stubs.c:41-90`

**ComfyUI / Python 集成**
- `ComfyUI/comfy/model_patcher.py:1811,1883-1888,1993,2013-2017`
- `ComfyUI/comfy/ops.py:128-226`（尤其 `:168,182,226`）
- `ComfyUI/comfy/model_management.py:1548-1570`（`cast_to_gathered`）；`:1608,1632,1637,1665`（pinning 关）；`:1464`（`mmap.bounce`）
- `ComfyUI/comfy/memory_management.py:18-75`（`:36,40-42,59,65-74`）
- `ComfyUI/comfy/utils.py:150-152`（`_comfy_tensor_file_slice`）
- `ComfyUI/comfy/pinned_memory.py:101-104`（`_comfy_hostbuf`）
- `comfy_aimdo/model_vbar.py:420-708`（`ModelVBAR`）；`comfy_aimdo/host_buffer.py:67-110`；`comfy_aimdo/torch.py:19-41`；`comfy_aimdo/xpu.py:120-138,241-297,408-480`

**Level Zero（联网核对）**
- `https://github.com/oneapi-src/level-zero/blob/master/include/ze_api.h`（本地副本 `/tmp/ze_api.h`，21568 行，API 1.19）
  - funcs: `:3588,3684,3718,3983,4100,4727,5507,5758,6428,7513,7566,7619,9924,9962,10215,10410,10494,5427`
  - structs: `ze_command_queue_desc_t`, `ze_command_list_desc_t`, `ze_event_desc_t`, `ze_event_pool_desc_t`, `ze_host_mem_alloc_desc_t`, `ze_device_mem_alloc_desc_t`, `ze_physical_mem_desc_t`
  - stype: `:302,308,309,310,311,312,316,317,327`
  - flags: `:2004 ONTEMANDPAGING`

---

## 11. 待查证 / 需用户或实机确认项

1. **（需实机）** 树 A 桩是否真的会阻断 safetensors 加载：建议在真机用一次性构建（或 mock `xpu_memcpy_host_to_device` 返回非 0）复现，确认抛错点。目前为静态推断。
2. **（需 Phase1-B / 实机）** XPU 上 `HostBuffer`（weights/weights-loaded 子集）是否恒空；`get_pin` 是否恒 None；即 L2 层是否真的不存在。
3. **（需实机）** B580 驱动是否接受 `zeCommandListAppendMemoryCopy` 直接写入 VBAR VMM（§4.4）；≤2MiB / 2MiB+1 / 大块 三档都要测。
4. **（需查证）** `xiangyuT/comfy-aimdo-xpu@cc3729fc` 的 `src/` 与本地 `aimdo-xpu/fork/src/`（ayi3030）在 `hostbuf.c`/`hostbuf-file-reader.c` 上是否逐字节一致——本报告的行号对齐是**强旁证**，但**未取到该 SHA 的源码**（构建期临时 clone，本地不存在）。
5. **（需确认）** `ze_api.h` 抓取：`v1.34.0` tag 与 `master` 得到**同一文件**，本报告一律按 `master` + API 1.19 引用；若 CI 实际用的 header 版本不同，个别成员可能有增补（核心函数签名稳定）。
6. **（需用户确认）** 目标是否要求「真异步 + 磁盘读重叠」（→ P1-1）；若只需「能跑 + 三层语义」，P0 已足够。

---

## 12. 来源 URL

- 上游源码（树 A）：`https://github.com/Comfy-Org/comfy-aimdo`（master 08b37ee）
- 社区 XPU fork：`https://github.com/xiangyuT/comfy-aimdo-xpu`（pin `cc3729fc59eeab77bd4c8b28b80e49c9faa855d4`）
- 团队 fork（树 B）：`https://github.com/ayi3030/comfy-aimdo`（分支 `xpu-autoenable`，c2cf98f）
- Level Zero API 头：`https://github.com/oneapi-src/level-zero/blob/master/include/ze_api.h`
- Level Zero 文档站：`https://oneapi-src.github.io/level-zero/latest/core/api.html`

---

*报告结束。所有「已核实」项均附 `文件:行` 或头文件行号；所有无法离线确证的项已显式标注「需查证 / 需实机验证」。*
