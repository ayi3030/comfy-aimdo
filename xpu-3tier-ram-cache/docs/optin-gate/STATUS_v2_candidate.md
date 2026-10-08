# STATUS — comfy-aimdo × Intel Arc XPU：中性 ABI 轨道（v2 候选）

**状态：FROZEN — 本周期不推进（用户已选 路 A = fork/SYCL 路线）；作为 v2 候选保留。**

- 冻结日期：2026-10-09
- 基线上游：`Comfy-Org/comfy-aimdo` @ `08b37ee`（工作区 `comfy-aimdo-src/`）
- 统一补丁：`aimdo-xpu/aimdo-xpu.patch`（24 文件，**+1118 / −156**，85485 B）
- 一致性：`git apply --check -R aimdo-xpu.patch` → **PATCH MATCHES WORKING TREE**
- 推送状态：**零提交、零推送**（唯一 remote = `origin=Comfy-Org/comfy-aimdo`，`HEAD == origin/master == 08b37ee`）

> ⚠️ **最硬的一条限制（必须最先读到）：`src-xpu/dispatch.c` 从未被任何编译器编译过。**
> 本机无 `gcc` / `clang` / `cl`。本轨道的一切结论都只是**源码级核对**，不是构建产物、更不是实机运行结果。

---

## 1. 架构差异（与本周期已部署的 路 A 相比）

| | **v2（本轨道，已冻结）** | **路 A（已部署，被接受）** |
|---|---|---|
| 原生实现 | 纯 `dlopen`/`dlsym` 直接调 Level Zero（`ze*`），**不 include 任何 SDK 头**（含 `ze_api.h`） | MSVC + oneAPI DPC++，`src-xpu/dispatch.cpp`（SYCL），链接 `sycl9.dll` |
| 共享 `src/*` | 引入中性 ABI（`gpu_*` 类型、`GpuDispatch g_gpu`），**改动 CUDA/HIP 共享源码** | 不改上游共享源码，以 overlay 方式引入第三方 `src-xpu/` 树 |
| 结构性优势 | **不链接 SYCL ⇒ 结构性免疫 `syclN.dll` ABI 错配**（正是 路 A 的 D1 类缺陷：导入 `sycl8.dll` 而机器只有 `sycl9.dll`） | 依赖 `syclN.dll` 版本与 oneAPI 构建环境绑死 |
| 代价 | 只在本机做过源码级核对，从未编译/运行；改动面波及 CUDA/HIP 共享层 | 依赖第三方 `xiangyuT/comfy-aimdo-xpu` 的 `src-xpu` 树，维护面在别人手里 |

## 2. 补丁内的修正（6 项）

| # | 文件 | 原状 | 现状 | 为什么重要 | 真机确认 |
|---|---|---|---|---|---|
| 1 | `src-xpu/dispatch.c` | `ZE_STRUCTURE_TYPE_PHYSICAL_MEM_DESC` 猜为 `0x1D` | 上游 `ze_api.h` 真值 **`0x20`** | stype 错会被驱动以 INVALID_ENUMERATION 拒绝 ⇒ **整条 VMM 卸载通路直接失效** | stype 值本身经真机实测同值成功 |
| 2 | `src-xpu/dispatch.c` | `ze_context_desc_t` 缺 `uint32_t flags`（16 B） | 补 `flags`（真结构 **24 B**）+ `stype=ZE_STRUCTURE_TYPE_CONTEXT_DESC(0x0D)` | 原实现驱动读 `desc->flags` 属**越界读（UB）** | 结构体布局经上游头文件核对；运行确认待 v2 构建 |
| 3 | `src-xpu/dispatch.c` | 显存总量硬编码 12 GiB 占位 | `zeDeviceGetMemoryProperties` 动态解析（可选符号，失败回退占位） | 记账口径正确性 | 真机实测设备总量 **11.60 GiB** |
| 4 | `src-xpu/dispatch.c` | `xpu_mem_address_reserve/free` 用原始 size，而 create/map/unmap/setaccess 用 `xpu_align(size)` | 六个调用点统一 `xpu_align(size)` | 若页 >2 MiB，map 会超出 reserve 范围。**B580 页 64 KiB 且入参已 2 MiB 对齐 ⇒ 本改动恒等、无行为变化**（保留 `>2 MiB` 告警防御） | 页大小 64 KiB（实测） |
| 5 | `src-xpu/dispatch.c` | `xpu_memcpy_htod_async` 空操作但**返回成功** | 返回非 0 + `AIMDO_LOG_ERROR` | 原实现令 `hostbuf_read_file_slice` 的 `CHECK_CU` **误判成功** ⇒ 「文件读进 hostbuf 却从未进显存」的**静默数据损坏** | 未编译，结论为源码级 |
| 6 | `comfy_aimdo/torch.py` | `get_tensor_from_raw_ptr` 无条件套用 CUDA 专有 `__cuda_array_interface__` | 加设备守卫：仅 `cuda`/未指定放行，其余（如 `xpu`）抛 `NotImplementedError` 并给指引 | 防止 XPU 下误用而得到难以定位的错误结果 | `py_compile` + AST 核对通过；对既有 CUDA/HIP 路径恒等放行 |

## 3. 真机证据（B580 实测，**认知边界务必读**）

实测硬件/运行时：`Intel(R) Arc(TM) B580 Graphics`，torch **2.14.0+xpu**，oneAPI **2026.1.0**，`torch.xpu.is_available()==True`，device_count=1。

- **(a) Level Zero VMM 可用 —— PASS**：`zeVirtualMemReserve → zePhysicalMemCreate → zeVirtualMemMap → zeVirtualMemSetAccessAttribute → unmap/destroy/free` 全部返回 0；页 64 KiB；设备总量 11.60 GiB；VA 经 `zeMemGetAllocProperties` 归类为 `DEVICE(2)` 且设备匹配。
- **(b) PyTorch 接受并回收 VMM 设备 VA —— PASS**：纯 ctypes 的 VMM 分配器经 `torch._C._xpu_customAllocator` 注册后，`randn(512²)/matmul/empty(3M)` 全部正确执行，分配全回收（pending=0）。

> **认知边界（关键）**：(a)/(b) 由**独立 ctypes 探针**证明，**不是**经 `aimdo_xpu.dll`。它们确立的是「**机制可行**」——
> B580+驱动+torch 14.0+xpu **有能力**做 Level Zero VMM，torch **确实接受** VMM 设备 VA。
> 它们**不**证明本轨道（或任何已发布 DLL）在运行时真的走通该路径。
> 另外：已部署 `xpu.py` 默认走 `native_hook`（UR-USM hook），而 (b) 验证的是 `change_current_allocator` 这条**不同**的注册路径。
> 这也正面回答了 PHASE2 设计里标为「最大不确定性」的 R1/R2——**经验答案是：可行**。

## 4. 向后兼容证明（CUDA/HIP 零影响）

- `src/plat.h` 中 `cuXxx → g_cuda.p_cuXxx` 的 `#else` 宏块与 HEAD **逐字节一致**（经 awk 抽取该块 diff 为空，两轮独立复核）。
- `gpu_*` 中性类型在 `!AIMDO_XPU` 下是 CUDA 类型的 `typedef` 别名 ⇒ 共享 `src/*.c` 的机械中性化在 CUDA/HIP 下语义/字节级不变。
- 无 Intel 环境行为：`src-xpu/dispatch.c` 全程 `dlopen/dlsym`、XPU 库加载失败 ⇒ `control.init()` 返回 False ⇒ `lib=None` ⇒ 走默认 `torch.xpu`，功能不中断。

## 5. 未覆盖项（开放清单）

- **(d) `hostbuf` 文件→显存路径仍未实现**：`xpu_memcpy_htod_async` 现会**显式失败**（修正 5），但真正实现需 `zeCommandListAppendMemoryCopy`（要建 queue/command list）。**本周期明确不实现。**
- **(e) 多卡**：恒取 device0。
- **(f) `zeDeviceGetProperties` 真实设备名未接**（结构体布局需实机核对，按「不确定就留 TODO」处理）。
- **Windows `change_current_allocator` 约束**（早期探针）：已初始化即报错，且无 `torch._C._xpu_construct_storage_from_data_pointer`。⇒ 即便 v2 复活，也大概率被迫退回 `native_hook`，与修正 6 的守卫方向一致。

## 6. 复活触发条件

出现以下任一情形时，本轨道值得重新启用：
1. 路 A 的 `sycl9.dll` 链接在 torch / 驱动 / oneAPI 版本跳变后**断裂**（ABI 错配类缺陷复发）；
2. 维护第三方 `src-xpu` overlay 树变得不可持续（上游漂移、无人维护）；
3. 需要在**未安装 oneAPI/DPC++ 工具链**的机器上构建 XPU 后端。

## 7. 如何复现与验证（复活时按此执行）

```bash
cd comfy-aimdo-src
git apply aimdo-xpu.patch          # 或 -R 反向校验：git apply --check -R aimdo-xpu.patch
# 然后必须：真正编译（本机无编译器，从未编译过）
#   Linux:  ./scripts/build-linux-aimdo.sh   → aimdo_xpu.so
#   Windows: 见 .github/workflows/build-wheels.yml 的 XPU 目标 → aimdo_xpu.dll
```

**复活后的必做验证（缺一不可）**：
1. 编译通过（此前从未编译过，此为第一道未知）；
2. B580 上 VMM 三连成功（(a)，机制侧已由探针证明可行）；
3. PyTorch 能否识别/回收 `alloc_fn` 返回的 VMM 设备 VA（(b)）——并注意本轨道走 `native_hook` 还是 `change_current_allocator`；
4. 修正后的 stype（`0x20` / `0x0D`）是否被驱动接受；
5. 页大小与 size 对齐一致性（本机 64 KiB 下恒等，其他驱动可能不同）；
6. 端到端：真实 ComfyUI 加载大模型，确认卸载生效且**无静默回退**。

## 8. 相关产物

| 文件 | 内容 |
|---|---|
| `aimdo-xpu.patch` | v2 全部改动的统一补丁（24 文件，+1118/−156） |
| `实现说明.md` | 逐文件改动表 + 修正说明 + 实机状态表 |
| `REVIEW_phase4_xpu.md` | 对 `dispatch.c` 的独立逐字核对（对照上游 `ze_api.h`） |
| `VERIFY_real_b580.md` | 真机 (a)/(b) 探针结果（含认知边界说明） |
| `PHASE2_DESIGN.md` | 原始设计文档（Track 1/2） |
| `交付说明.md` | 部署/回退/风险说明（v2 视角，部分口径已由本文件取代） |
