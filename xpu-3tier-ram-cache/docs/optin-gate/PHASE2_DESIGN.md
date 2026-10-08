# comfy-aimdo × Intel Arc B580 (XPU) 重构方案 — Phase 2 设计文档

> 团长（解耦林）主理。依据：Phase 0 源码体检 + 蓝驭芯（intel-xpu-adapter）联网查证结论。
> 目标插件：`comfy-aimdo`（Comfy-Org，CUDA/HIP 驱动级 VMM 显存卸载 C 扩展）。
> 目标环境：ComfyUI v0.3.x + Intel Arc B580（Battlemage / Xe2）+ oneAPI Level Zero / IPEX。

---

## 0. 可行性判定（来自蓝驭芯查证，已附来源）

| 项 | 结论 | 关键事实 |
|----|------|----------|
| XPU 虚拟内存 API 等价性 | **可行** | Level Zero 提供与 CUDA VMM 一一对应的 API：`zeVirtualMemReserve`/`zeVirtualMemFree`、`zePhysicalMemCreate`/`zePhysicalMemDestroy`、`zeVirtualMemMap`/`zeVirtualMemUnmap`、`zeVirtualMemSetAccessAttribute`/`zeVirtualMemGetAccessAttribute`、`zeVirtualMemQueryPageSize`。规范 ≥1.1 即存在，现代 Arc 驱动（含 LZ 1.21.x）已分发，B580(Xe2) 支持。 |
| PyTorch XPU 可插拔分配器 | **可行** | `torch.xpu.memory.XPUPluggableAllocator`，`alloc_fn(size_t, int device, sycl::queue*)` / `free_fn(...)`，经 `torch.xpu.memory.change_current_allocator` 注册，是 `CUDAPluggableAllocator` 的 XPU 等价物。 |
| 版本矩阵 | 真机实测 | torch **2.14.0+xpu** / oneAPI **2026.1.0** / Python **3.13.14**（`VERIFY_real_b580.md` §1；原建议值 torch 2.7.0 / IPEX 2.7.10+xpu / Py3.11–3.12 为规划）。xformers 不支持 XPU，须换 `F.scaled_dot_product_attention`。 |
| dtype | 推荐 | Xe2 的 XMX 原生支持 bf16（默认）与 fp16。 |

**两项关键实机验证（原「本沙箱无 Intel 硬件，无法代验」已被真机取代；现均已完成）：**
- (a) B580 驱动 `zeVirtualMemReserve` 能否成功（VMM 功能可用性）；
- (b) `XPUPluggableAllocator` 的 `alloc_fn` 返回 VMM 设备 VA 后，PyTorch 能否正确识别与回收。

**判定：comfy-aimdo 在 Arc B580 上是「可真移植后端」，不是只能降级。**

---

## 1. 总体策略：两条轨道

### Track 1 — Python 层 XPU 感知 + 优雅降级（最小侵入，立即可合并，无需 Intel 硬件）
让 comfy-aimdo 在检测到 Intel XPU 时**干净地禁用自身**（`lib=None`、不崩、不误导成 "assuming Nvidia"），ComfyUI 在 Arc B580 上走默认 `torch.xpu` 分配器正常出图。原 CUDA/HIP 行为 100% 不变。
- 价值：今天就能让插件在 XPU 环境安装/运行不崩。
- 风险：极低，仅 Python 层改动，可静态 + 导入守卫审查。

### Track 2 — 真 XPU 后端（ze* VMM + XPUPluggableAllocator，需抽象共享 C 层）
新增 `src-xpu/dispatch.c` + 共享层「后端无关中性接口」，让 VBAR 卸载机制在 XPU 上复刻。
- 价值：在 Arc B580 上获得与 CUDA 同级的动态权重卸载能力。
- 风险：高。（**原「无法编译/实机验证」前提已被真机取代**：XPU 后端已编译为 `aimdo_xpu.dll` 并部署；(a)(b) 已在真机 PASS，见 `VERIFY_real_b580.md`。）

> 原则：能加分支就不重写，能抽象就不改签名，老用户无感。Track 1 必做；Track 2 视交付深度决定。

---

## 2. 设备抽象层设计（Track 2 核心）

### 2.1 现状耦合点（已枚举）
- `g_cuda.*` 仅被 `src/plat.h` 与 `src/cuda-hooks-shared.h` 两处头文件直接引用（宏定义）。
- 共享 `src/*.c` 通过 `cuMemAddressReserve`/`cuMemCreate`/`cuMemMap`/`cuMemSetAccess`/`cuMemUnmap`/`cuMemRelease`/`cuDeviceGet`/`cuMemGetInfo` 等宏调用，传的是 CUDA 类型（`CUdevice`/`CUdeviceptr`/`CUmemGenericAllocationHandle`/`CUmemAllocationProp`/`CUmemAccessDesc`）。
- `src/plat.h` 的 `three_stooges()` 是核心 VMM 三步（`cuMemCreate→cuMemMap→cuMemSetAccess`）；`CHECK_CU` 依赖 `CUDA_SUCCESS`。
- `src/gpu_abi.h` 定义 CUDA/HIP 鸭子类型，含 HIP 专属覆盖（如 `CU_DEVICE_ATTRIBUTE_INTEGRATED` 16 vs 18）。

### 2.2 抽象方案（中性接口）
新增/改写如下，保持 CUDA/HIP 既有路径签名不变：

1. **`src/gpu_abi.h`（泛化）**：新增后端无关中性类型
   ```c
   typedef int   gpu_result_t;          // 替代 CUresult
   typedef void *gpu_device_t;          // 替代 CUdevice
   typedef void *gpu_deviceptr_t;       // 替代 CUdeviceptr
   typedef void *gpu_mem_handle_t;      // 替代 CUmemGenericAllocationHandle
   typedef struct { int type; int id; } gpu_mem_location_t;
   typedef struct { gpu_mem_location_t location; int flags; } gpu_mem_access_desc_t;
   typedef struct { int type; int requestedHandleTypes; gpu_mem_location_t location; } gpu_mem_prop_t;
   #define GPU_SUCCESS 0
   ```
   CUDA/HIP 构建下这些类型可 `typedef` 回原 CUDA 类型，保证零改动兼容。

2. **`src/gpu_dispatch.h`（新增 `GpuDispatch` 中性分发结构）**：把原有 `AimdoCudaDispatch` 的字段改为中性签名（返回值 `gpu_result_t`，指针用 `void*`），保留 `AimdoCudaDispatch` 仅供 CUDA/HIP 后端内部填充。

3. **`src/plat.h`（宏改道）**：将 `cuInit`/`cuMemCreate`/... 宏从 `g_cuda.p_xxx` 改指向中性 `g_gpu.p_xxx`；`CHECK_CU` 改为 `CHECK_GPU`（判 `GPU_SUCCESS`）；`three_stooges()` 改为调用 `g_gpu` 中性三步，prop/access 结构用中性类型。CUDA/HIP 后端提供薄封装把 CUDA 类型转中性类型。

4. **`src-xpu/dispatch.c`（新增，XPU 后端）**：
   - `aimdo_xpu_runtime_init()`：加载 Level Zero loader（`libze_loader.so`/`ze_api.dll`），经 `zeModuleGetFunction`/直接符号解析 `zeVirtualMemReserve` 等（参考 `aimdo_cuda_runtime_init` 的模块定位 + 符号表模式，但 LZ 用 `zeInit` + loader，不是 `cuGetProcAddress`）。
   - 实现 `g_gpu` 各指针：`mem_address_reserve`（→`zeVirtualMemReserve`）、`mem_create`（→`zePhysicalMemCreate`）、`mem_map`（→`zeVirtualMemMap`）、`mem_set_access`（→`zeVirtualMemSetAccessAttribute`）、`mem_unmap`/`mem_release`/`mem_get_info`（→`zeMemGetAllocProperties`/统计）/ 设备枚举等。
   - 提供 `alloc_fn`/`free_fn` 导出，内部走 VMM，产设备 VA 交 PyTorch XPU caching allocator（镜像 `pyt-cu-plug-alloc.c`）。

5. **构建系统**：`scripts/build-linux-aimdo.sh` 与 `build-wheels.yml` 增加第三个目标
   - `aimdo_xpu.so` = `src/*.c + src-xpu/dispatch.c + src-posix/*.c`（`-DAIMDO_XPU`）
   - `aimdo_xpu.dll` = `src/*.c + src-xpu/dispatch.c + src-win/*.c`
   - Windows 上 XPU 走 `src-win` 的 `module-load.c`/`xfer-file-plat.c` 等通用实现（不涉及 CUDA detour）。

---

## 3. 文件级改动清单（表：文件 → 位置 → 现状 → 改动 → 风险）

### Track 1（Python，必做，安全）
| 文件 | 位置 | 现状 | 改动 | 风险 |
|------|------|------|------|------|
| `comfy_aimdo/control.py` | `detect_vendor()` L29-59 | 只认 cuda/rocm；识别不出时**默认假设 cuda**(L76) | 增加 xpu 检测：`torch.xpu.is_available()` 且无 cuda/rocm → "xpu"；未识别时**不再默认 cuda**，而是记日志并返回 `None`（禁用） | 低 |
| `comfy_aimdo/control.py` | `init()` L62-100 | 无 xpu 分支；impl 字典无 xpu | `impl` 增加 `"xpu": "aimdo_xpu"`；xpu 下若扩展未编译则 `lib=None` 并记 "XPU backend not built, aimdo disabled" | 低 |
| `comfy_aimdo/torch.py` | L36/L42 | `CUDAPluggableAllocator(torch.cuda.memory...)` 基类 CUDA 专属；导入即崩于无 CUDA 环境 | 守卫：`torch.cuda` 可用才定义 CUDA 类；新增 `XPUPluggableAllocator(torch.xpu.memory.XPUPluggableAllocator)`（守卫 `torch.xpu`）；`get_torch_allocator()` 按当前后端返回对应分配器或 None | 低 |
| `comfy_aimdo/torch.py` | L8-22 | `__cuda_array_interface__` 构造张量 | 增加 XPU 兼容路径（`__cuda_array_interface__` 仅 CUDA；XPU 走 `torch.frombuffer`/`as_tensor` 等效） | 低 |
| `comfy_aimdo/model_vbar.py` | `__init__` L50-58 | `lib` 为 None 时 `lib.vbar_allocate` 崩 | 顶部加 `if lib is None: raise RuntimeError("aimdo not initialized / XPU backend disabled")` 或在模块加载即守卫 | 低 |
| `comfy_aimdo/vram_buffer.py` | `__init__` L27-37 | 同上 | 同上加 None 守卫 | 低 |
| `comfy_aimdo/host_buffer.py` `malloc_graph.py` `storage.py` | 各处 `lib.*` | 仅加载后触发 | 现有守卫已够；在 `init()` 返回 False 时这些不会被调用，无需改 | 无 |

### Track 2（C 后端，需抽象 + 未验证）
| 文件 | 位置 | 现状 | 改动 | 风险 |
|------|------|------|------|------|
| `src/gpu_abi.h` | 全文件 | CUDA/HIP 鸭子类型 | 增加中性类型 + HIP/CUDA 下 `typedef` 回原类型 | 中（影响所有编译单元） |
| `src/gpu_dispatch.h` | `AimdoCudaDispatch` L54-89 | CUDA 类型分发 | 新增 `GpuDispatch` 中性结构；保留原结构 | 中 |
| `src/plat.h` | 宏 L72-97、`CHECK_CU` L180、`three_stooges` L184-222 | 绑定 `g_cuda` + CUDA 类型 | 宏改道 `g_gpu` 中性；`CHECK_GPU`；`three_stooges` 中性化 | 高（核心路径） |
| `src/control.c` `model-vbar.c` `vrambuf.c` `hostbuf*.c` `malloc-graph.c` | 调用 `cuXxx` | 传 CUDA 类型 | 改用中性类型（机械替换，配合头层 typedef 兼容） | 中 |
| `src-xpu/dispatch.c` | 新增 | 无 | XPU 后端：`aimdo_xpu_runtime_init` + `g_gpu` 填充 + `alloc_fn`/`free_fn` 导出 | 高（未编译/未验证） |
| `scripts/build-linux-aimdo.sh` | L94-109 | 两目标 | 增 `aimdo_xpu.so` 目标（`-DAIMDO_XPU`） | 低 |
| `.github/workflows/build-wheels.yml` | 矩阵 | 两目标 | 增 `aimdo_xpu` 构建（Linux/Windows），wheel 含 `aimdo_xpu` | 低 |

---

## 4. 回退链（XPU → CUDA → CPU）
- **运行时**：`control.init(implementation=None)` 自动探测后端顺序建议 `xpu → cuda → rocm`（XPU 优先，因本次目标机即为 Arc）；任一后端扩展加载失败则 `lib=None` 并降级到下一后端；全部失败则 `lib=None`（插件禁用，ComfyUI 用默认分配器）。
- **显存压力**：XPU 后端复用 aimdo 的 `budget_deficit` 逻辑，用 `zeMemGetAllocProperties`/设备查询替代 `cuMemGetInfo`。
- **CPU 回退**：无 GPU 时 `lib=None`，所有分配器返回 None，完全无侵入。

---

## 5. 验证计划（实机，标注「未实机验证」）
1. **构建**：在 Arc B580 机器按 `build-linux-aimdo.sh` 增 `aimdo_xpu.so`，确认 LZ loader 解析成功。
2. **单元 (a)**：小程序调用 `zeVirtualMemReserve`+`zePhysicalMemCreate`+`zeVirtualMemMap`，验证 VMM 可用。
3. **集成**：`control.init("xpu")` → `init_devices([0])` → 建 `ModelVBAR` → `fault()`/`unpin()` 往返，确认 VA 稳定、卸载生效。
4. **PyTorch (b)**：`XPUPluggableAllocator` 注册后跑一次推理，确认 `alloc_fn` 返回的 VMM VA 被 PyTorch 识别/回收，无泄漏/段错误。
5. **质量/性能基线**：同一工作流在 XPU 原生分配器 vs aimdo-XPU 下各跑一次，记录耗时/显存峰值（PSNR/SSIM 对比输出一致性）。
6. **防静默回退**：日志确认设备是 XPU 而非 CUDA/CPU；如回退须显式告警。

---

## 6. 风险与未决项
- R1：Level Zero VMM 在 B580 实际可用性 —— ✅ **已关闭**（(a) PASS）。
- R2：XPUPluggableAllocator 的 VMM VA 能否被 PyTorch 识别 —— ✅ **已关闭**（(b) PASS）。
- R3：~~Track 2 C 代码在沙箱无法编译~~ **（XPU 后端已在真机编译部署，见 `VERIFY_real_b580.md` §4）**，质量另经代码审查（`REVIEW_phase4_xpu.md`）。
- R4：共享 `src/*.c` 中性化改动面较广，需保证 CUDA/HIP 既有路径字节级行为不变（靠 `typedef` 兼容 + 回归测试）。
