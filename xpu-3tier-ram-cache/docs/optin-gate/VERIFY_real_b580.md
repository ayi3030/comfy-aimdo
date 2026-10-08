# 实机验证报告 — Intel Arc B580（XPU）

> ## ⚠️ 后续事实（2026-10-09 追加；**不改动下方原文**）
> 本文 (a)/(b) 结论为**事实源，仍然有效**。唯 §6「未运行完整 ComfyUI 工作流」宜收窄：
> - 完整 ComfyUI 启动工作流**已在 flag 模式下跑通**（`VERIFY_e2e_hook_b580.md` §3）；唯**大模型三层卸载压力往返仍未验证（F8，保持原文）**。
> - **【2026-10-09 补记】大模型三层卸载压力往返已由 P4 验收通过（全绿，证据 `logs/acc_20261009-015303_*`）**；上句「仍未验证（F8）」仅指本注解成文时，现**升级为「已验收通过」**。
> - 下方**数值 / sha / 行号 一律保留**。归属：intel-xpu-adapter。

**验证人**: intel-xpu-adapter（蓝驭芯）
**日期**: 2026-10-09
**性质**: **真机实测**（非沙箱推断）。运行环境 = 目标部署机的 `python_embeded`。

---

## 0. 重大发现 & 结论先行

1. **该机器确有 Intel Arc B580，且 torch XPU 运行时可用**——任务 #6「沙箱无 Intel 硬件」的前提**不成立**，两项关键实机验证因此**可直接执行，现已完成**。
2. **(a) Level Zero VMM 在 B580 上完全可用** —— `zeVirtualMemReserve/zePhysicalMemCreate/zeVirtualMemMap/zeVirtualMemSetAccessAttribute/zeVirtualMemUnmap/zePhysicalMemDestroy/zeVirtualMemFree` **全部返回 0（成功）**。
3. **(b) PyTorch 接受 VMM 设备 VA 并正常执行/回收** —— 用纯 ctypes 实现的 VMM 分配器经 `torch._C._xpu_customAllocator` 注册后，`torch.randn/matmul/empty` 全部正确执行，分配全部回收。
4. **结论：comfy-aimdo 的 XPU 移植「可真落地」，两项最大不确定性均已实机消除。**

---

## 1. 环境（实测）

| 项 | 值 |
|---|---|
| `torch.xpu.is_available()` | **True** |
| `torch.xpu.device_count()` | 1 |
| 设备名 | **Intel(R) Arc(TM) B580 Graphics** |
| torch | 2.14.0+xpu（torchvision 0.29.0+xpu / torchaudio 2.11.0+xpu） |
| Intel 运行时 | oneAPI **2026.1.0**（pip 包：`intel_sycl_rt`/`intel_cmplr_lib_rt`/`intel_cmplr_lib_ur`/`intel_opencl_rt`/`onemkl_sycl_*`/`intel_pti`）+ `omni_xpu_kernel-0.2.0b2+torch214.bmg` |
| Level Zero loader | `ze_loader.dll`（python_embeded 内可加载） |
| 设备总显存 | **11.60 GiB**（`zeDeviceGetMemoryProperties`，name="DDR"；注：非 12.0 GiB） |
| VMM 页大小 | **65536 B（64 KiB）** |

---

## 2. 验证 (a)：Level Zero VMM 可用性 —— **PASS**

脚本：`probe_l0_vmm.py`（纯 ctypes + dlsym，不 include SDK 头）。逐字对照 `ze_api.h` 构造结构体。

```
zeInit: 0                         (ZE_INIT_FLAG_GPU_ONLY)
drivers: 1 ; devices: 1
device totalSize = 11.60 GiB
zeContextCreate: 0
pageSize: 65536 = 64.0 KiB
zeVirtualMemReserve: 0   va=0x...  size=67108864
zePhysicalMemCreate: 0   phys=0x...
zeVirtualMemMap: 0
zeMemGetAllocProperties(VMM VA): 0   type=2(DEVICE)  pageSize=65536  dev_match=True
zeVirtualMemSetAccessAttribute: 0
zeVirtualMemUnmap: 0
zePhysicalMemDestroy: 0
zeVirtualMemFree: 0
=> RESULT (a): PASS
```

**要点**：VMM VA 被 Level Zero 归类为 **DEVICE(2)** 且关联到正确设备——这正是 PyTorch 判定设备指针所依据的信息。

---

## 3. 验证 (b)：PyTorch 识别并回收 VMM VA —— **PASS**

脚本：`probe_b_xpu_pluggable.py`（用 ctypes `CFUNCTYPE` 回调实现 alloc_fn/free_fn，无需编译器），经 `torch._C._xpu_customAllocator(alloc_addr, free_addr)` + `torch.xpu.memory.change_current_allocator(...)` 注册为当前分配器。

```
[env] custom VMM allocator registered
randn   sum: 365.6038513183594          # torch.randn(512,512, device="xpu")
matmul  sum: -11098.71484375            # x @ x
empty numel: 3000000                    # 触发更大块分配
[env] pendings left: 0                  # 所有经 VMM 分配的块均被 free_fn 正确回收
=> RESULT (b): PASS
```

**要点**：PyTorch 的 XPU caching allocator 与 VMM 设备 VA 完全兼容——分配、算子写入、内存回收全通，且无泄漏。

---

## 4. 部署产物加载 —— **PASS**（消除 #8 的 sycl9.dll 顾虑）

实测 `python_embeded/Lib/site-packages/comfy_aimdo/aimdo_xpu.dll`（238592 B，fork CI 产物）在导入 torch 后**可被 `ctypes.CDLL` 成功加载**，并导出 `alloc_fn`、`free_fn`、`xpu_get_total_vram_usage`、`xpu_allocator_get_memory_stats`。

→ #8 审计的「真机 System32 无 sycl9.dll」是**误报**：运行时由 pip 包（`intel_sycl_rt` 等）提供，torch-xpu 导入时已注册 DLL 搜索目录，DLL 依赖链可解析。

---

## 5. 对本项目的意义

- 核心机制（Level Zero VMM + XPUPluggableAllocator）**实机验证通过**，`src-xpu/dispatch.c` 的设计路径正确、可行。
- 顺带确认 `src-xpu/dispatch.c` 的常量选择正确：`CONTEXT_DESC=0x0D`、`PHYSICAL_MEM_DESC=0x20` 均被驱动接受（本验证用同值成功）。
- 页大小 64 KiB < 2 MB ⇒ 审查项 R-a（reserve/map 对齐）在本环境**无实际风险**。
- 设备总量 11.60 GiB（非 12 GiB）——fallback 常量 `XPU_FALLBACK_VRAM` 应尽量改用真实查询值（原实现已接 `zeDeviceGetMemoryProperties`）。

## 6. 尚未覆盖（= 任务 #11 端到端）

本报告为**组件级**实机验证（VMM + 分配器 + DLL 加载）。**未**运行完整 ComfyUI 工作流 / 真实模型加载 / VPU 卸载往返——这属于 #11「端到端复验」，本环境具备执行条件。

## 7. 复现

```bash
PE="E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/python_embeded"
"$PE/python.exe" aimdo-xpu/probe_l0_vmm.py
"$PE/python.exe" aimdo-xpu/probe_b_xpu_pluggable.py
```
