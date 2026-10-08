# REVIEW（Phase 4）— XPU 后端代码审查（intel-xpu-adapter 复核）

**审查人**: intel-xpu-adapter（蓝驭芯，XPU 技术负责人）
**日期**: 2026-10-09
**范围**: `src-xpu/dispatch.c`（纯 dlsym Level Zero 后端）+ `comfy_aimdo/torch.py` / `control.py`（Python 侧）
**依据**: 逐字核对 `oneapi-src/level-zero` master `include/ze_api.h`（本机已下载，21568 行）+ PyTorch 官方文档。
**性质**: 只读审查，**未改动任何源码**。审查期间发现 `src-xpu/dispatch.c` 正被其他成员并发修改（mtime 01:11:50），为避免覆盖其编辑，本次仅出结论与建议。

---

## 0. 结论先行

`src-xpu/dispatch.c` 的 ze* 鸭子类型、函数签名、结构体布局、枚举常量**全部正确**（逐条核对通过）。当前文件中**已包含**两处关键修正（下 §1），说明返工已在进行。剩余均为**低危/已声明**项（下 §2），不阻断「真移植」，但会影响实机表现与可观测性。

---

## 1. 已核对通过 / 已修复项

| 项 | 核对结果 | 依据 |
|---|---|---|
| 全部 15 个 ze* 函数指针签名 | **正确** | 与 `ze_api.h` 逐字一致（含 `zeVirtualMemSetAccessAttribute` 无 pNext、`zeVirtualMemGetAccessAttribute` 末尾 `size_t* outSize`） |
| `ze_physical_mem_desc_t` 字段序 stype/pNext/flags/size | **正确** | `ze_api.h` L10312 |
| `ze_context_desc_t` 补 `flags` 字段 | **正确**（原漏，属越界读） | `ze_api.h` L3303 |
| `ze_device_memory_properties_t` 布局（totalSize@32, name@40） | **正确** | `ze_api.h` L2473 |
| 常量 `PHYSICAL_MEM_DESC=0x20` / `CONTEXT_DESC=0x0D` / `DEVICE_MEMORY_PROPERTIES=0x07` / `GPU_ONLY=ZE_BIT(0)` | **正确** | `ze_api.h` L327 / L308 / L302 / L1277 |
| **F1** `ctx_desc.stype = ZE_STRUCTURE_TYPE_CONTEXT_DESC` | **已修复**（L516） | 规范要求，否则 `zeContextCreate` 返回 INVALID_ENUMERATION |
| **F3** `zeDeviceGetMemoryProperties` 解析 + 真实显存总量 | **已修复**（L144-169/474/528） | 替换 12GiB 占位；按可选符号解析，失败回退，设计正确 |
| loader 库名 `libze_loader.so.1`/`ze_loader.dll` | **正确** | 与 Windows loader 实测一致 |
| `cuDeviceGetAttribute` 仅在 `control.c:26` 以 `GPU_DEVICE_ATTRIBUTE_INTEGRATED` 调用 → XPU 桩返回 0 | **正确**（离散 Arc） | `src/control.c` |

---

## 2. 剩余问题（按优先级）

| # | 级别 | 位置 | 问题 | 建议 |
|---|---|---|---|---|
| R-a | 低 | `xpu_mem_address_reserve`/`_free` | reserve/free 用**原始 size**，而 create/map/unmap/setaccess 用 `xpu_align(size)`，对齐不一致。实践上因 `vrambuf_create` 已 `CUDA_ALIGN_UP`(2MB)、`vrambuf_grow` 按 16MB 块，且页大小是 ≤16MB 的 2 幂，通常无碍；但**若 `zeVirtualMemQueryPageSize` 返回 >2MB，reserve 会因 size 未页对齐而被拒** | reserve/free 与 map 采用同一 `xpu_align`；或在 init 校验 `g_ze_page_size <= 2MB` 否则告警 |
| R-b | 中（已知 R5） | `xpu_mem_get_info` | `free = total - aimdo记账已用量`；`total` 已可查真值，但失败仍回退 12GiB 占位（B580 名义 12GB，可用 <12GB）。"free" 非驱动真实空闲 | 保留为估算并在日志标注口径；实机确认 `zeDeviceGetMemoryProperties` 成功 |
| R-c | 中（已知 R4） | `xpu_memcpy_htod_async` | 空操作 → hostbuf「文件→显存」加载在 XPU 上不生效 | 用 `zeCommandListAppendMemoryCopy`（需建 queue/command list）补齐；否则该路径须显式禁用 |
| R-d | 低 | `xpu_device_get_name` | 硬编码 "Intel XPU device"，未用 `zeDeviceGetProperties` | 实机接入真名（结构体已可核对）；不影响功能 |
| R-e | 低 | `xpu_ctx_get_device` / `xpu_device_total_mem` | 恒取 device 0；多卡会错 | B580 单卡可用；多卡再改 |
| R-f | 低 | `p_zeMemGetAllocProperties` | 已解析但**未使用**（死代码）。若启用，须先设 `stype=0x17`（MEMORY_ALLOCATION_PROPERTIES） | 清理或补 stype |
| R-g | 中（Python） | `comfy_aimdo/torch.py: get_tensor_from_raw_ptr` | 无条件用 `__cuda_array_interface__`（CUDA 专有）；注释称 XPU 不走此路径，但 `aimdo_to_tensor` 仍会路由到此 | 为 XPU 加守卫/独立路径，避免 XPU 下误用 CUDA 张量接口 |
| R-h | 低（集成） | 符号导出 | `control.py` 依赖 `lib.get_total_vram_usage/init/plat_init/get_devctx/set_log_callback/...` 等**无前缀**符号；须确保 XPU 构建（`src/*.c`+`src-xpu/dispatch.c`）在 Linux 正确导出（可见性） | 构建后 `nm -D aimdo_xpu.so` 核对符号表 |

---

## 3. 曾须实机判定的项（现已由真机关闭，2026-10-09 更新）

- (a) B580 驱动 `zeVirtualMemReserve/zePhysicalMemCreate/zeVirtualMemMap` 是否成功 —— ✅ **PASS 全返回 0**（VMM 功能可用性；`VERIFY_real_b580.md` §2）。
- (b) `XPUPluggableAllocator` 的 `alloc_fn` 返回 VMM 设备 VA 后，PyTorch 能否识别/回收 —— ✅ **PASS**（`VERIFY_real_b580.md` §3，原「最大不确定性」不再成立）。
- 若 (a)/(b) 任一失败的回退路径（`lib=None`，走默认 `torch.xpu` 分配器）**保留备用**，不影响 ComfyUI 正常出图。

---

## 4. 置信度

| 结论 | 置信度 |
|---|---|
| ze* 签名/结构体/常量全部正确、F1/F3 已修复 | **已核实**（对照 ze_api.h + 读当前源码） |
| R-a 对齐不一致（仅当页>2MB 才有害） | **已核实**（代码逻辑 + vrambuf.c 对齐链） |
| R-b/R-c 为功能性限制 | **已核实**（代码即现状） |
| R-g/R-h 潜在集成问题 | **已确认**（构建完成、DLL 已部署加载；R-g 守卫见 `实现说明.md` §2.5） |
