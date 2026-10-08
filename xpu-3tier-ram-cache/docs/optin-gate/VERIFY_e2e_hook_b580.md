# 真机 B580 端到端复验（补充）— UR-USM 钩子组件级验证 + 门禁根因

> ## ⚠️ 后续事实（2026-10-09 追加；**不改动下方原文**）
> 本文为**当时**真机复验。§0 表与 §4/§5 有两个阻塞项，**现均已解除**；据其得出「当前仍 FAIL」会与事实相反：
> - §5「阻塞项二：comfy_aimdo `_xpu_opt_in()` 第二段门禁」→ **已由 `dev28` / `c2cf98fb9`（非阻断守卫）修复**；免 `--enable-dynamic-vram` 自动启用**生效**（待 P4 复验）。
> - §4「阻塞项一：`nodes.py` `NameError` 启动崩溃」→ **已由 task #14 修复**，flag / no-flag 两模式均可达服务器横幅。
> - §6「含大模型加载的工作流 / 三层卸载压力往返**未验证**」**仍成立（F8，保持原文）**。
> - **【2026-10-09 补记】大模型三层卸载压力往返已由 P4 验收通过（全绿，证据 `logs/acc_20261009-015303_*`）**；本文上两处「（待 P4 复验）」与「仍成立（F8）」均仅指本注解成文时，现一并**升级为「已验收通过」**。
> - 下方**数值 / sha / 行号 / 日志一律保留**。归属：intel-xpu-adapter。

- 验证者：intel-xpu-adapter（蓝驭芯，XPU 适配专家）
- 时间：2026-10-09 01:22 ~ 01:26（本地）
- 主机：Windows 11；`Intel(R) Arc(TM) B580 Graphics`；Python 3.13.14（portable embed）；
  torch `2.14.0+xpu`；oneAPI 2026.1.0（sycl9.dll）
- 被测产物：**部署在真机 site-packages 的 comfy_aimdo**，`0.5.6.dev25` / commit `g9b1efd905`
  - `...\python_embeded\Lib\site-packages\comfy_aimdo\`（PEP 420 命名空间包，无 `__init__.py`）
  - `aimdo_xpu.dll` sha256 `b4241291e962450ed71cd0da9a2a00ff5b1de4e26d86f6b434bc52af5011ce2c`（238592 B）
- 与 `VERIFY_p3_endtoend.md`（runtime-verifier）的关系：**独立并行**。本报告在**组件级**（UR-USM 钩子
  本身）给出更强的正面证据；两者对阻塞项的结论**互相独立地一致**。

---

## 0. 结论速览

| 层面 | 结论 | 置信度 |
|---|---|---|
| UR-USM native_hook 钩子是否真的在仲裁（非仅"安装成功"） | **PASS**（组件级 14/14 检查全过，见 §2） | 已实测 |
| 真机 ComfyUI 进程内 XPU 后端是否真实初始化（非静默回退） | **PASS**（`--enable-dynamic-vram`，见 §3） | 已实测 |
| 真机 ComfyUI 是否无 flag 自动启用 DynamicVRAM | **FAIL**（被 `_xpu_opt_in()` 挡住） | 已实测，与 runtime-verifier 一致 |
| ComfyUI 能否启动到服务横幅 | **FAIL**（`nodes.py` 的 `NameError`，与 AIMDO 无关） | 已实测，与 runtime-verifier 一致 |
| 含大模型加载的工作流 / 三层卸载压力往返 | **未验证（被上述阻塞项阻断）** | 阻塞 |

**一句话**：AIMDO 的 XPU 路径本身在 B580 上**工作正常且证据充分**；当前端到端验收被
**两个与本适配无关的阻塞项**挡住 —— (1) `ComfyUI/nodes.py` 一处未完成编辑导致的 `NameError`；
(2) comfy_aimdo 自身 `_xpu_opt_in()` 的第二段门禁。

---

## 1. 验证方法

三步，全部在真机、被测产物即部署字节：

1. **组件级钩子冒烟**：`outputs/e2e_deployed_smoke.py`，直接 `import comfy_aimdo.control` 并复刻
   `ComfyUI/main.py` 的调用顺序（先 `control.init()`，由其 `_preload_torch_runtime()` 触发
   `import torch`），然后 `init_devices([0])` → 分配 → 观测钩子计数 → `deinit()`。
   输出：`outputs/e2e_deployed_smoke.json`。
2. **真机 ComfyUI 启动（带 flag）**：`run_intel_gpu.bat` 等价命令
   （`--enable-dynamic-vram --disable-auto-launch --port 8199`），抓取启动日志。
   输出：`outputs/aimdo_comfyui_launch.log`。
3. **AIMDO 隔离对照**：同命令但 `--disable-dynamic-vram`，证明 `nodes.py` 崩溃与 AIMDO 无关。
   输出：`outputs/comfyui_noAIMDO.log`。

---

## 2. 组件级 UR-USM 钩子验证：14/14 PASS

`e2e_deployed_smoke.py` 全部检查通过（`RESULT: PASS`）：

| # | 检查 | 证据（逐字） |
|---|---|---|
| 0a | 加载的是部署产物 | `...\site-packages\comfy_aimdo`；`version 0.5.6.dev25 commit=g9b1efd905` |
| 0b | 未依赖 `AIMDO_XPU_ENABLED` | `AIMDO_XPU_ENABLED=None`（走显式 `implementation="xpu"` 通道） |
| 1 | `control.init(implementation='xpu')` 成功 | `init()=True` |
| 2 | Windows 默认选择 native_hook | `mode=native_hook` |
| 3 | 目标 GPU 为 Intel Arc | `Intel(R) Arc(TM) B580 Graphics  total=11.60 GiB` |
| 4 | `init_devices([0])` 成功（队列已发布、设备上下文建立） | `init_devices=True` |
| 5 | 钩子暴露统计（已挂接并启用） | `21 个计数器` |
| 6 | torch 分配抵达 AIMDO 钩子 | `alloc_calls 0 -> 3` |
| 7 | 被跟踪记账而非透传 | `tracked_alloc_calls=3 tracked_alloc_bytes=1536 MiB` |
| 8 | 每次请求都解析出设备 | `unknown_device_calls=0` |
| 9 | expandable_segments 未绕过钩子 | `physical_mem_create_calls=0` |
| 10 | 释放被观测并回冲 | `tracked_free_calls=3 tracked_free_bytes=1536 MiB` |
| 11 | 清空缓存后记账平衡 | `alloc=1610612736 free=1610612736` |
| 12 | 钩子耗时统计可用 | `{"hook_calls":3,"hook_ns":383883600,...}` |
| 13 | 干净卸载 | `deinit 返回`；`restored the torch.xpu entry points` |

**这一层的独有价值**：runtime-verifier 的测试停在"后端初始化成功 / 队列已发布"。
本测试进一步证明钩子**真的在分类与记账 PyTorch 的分配**（`alloc_calls` 增长、`tracked_alloc_bytes`
与 3×512 MiB 精确吻合、`alloc==free` 对账平衡、`physical_mem_create_calls==0` 说明
expandable_segments 未旁路钩子）。即：native 侧的 `urUSMDeviceAlloc/urUSMFree` detour
（`src-xpu/ur-usm-detour.c`）在真机上按设计工作。

原生侧关键日志（组件与 ComfyUI 两次运行一致）：

```
aimdo_xpu_ur_hook_install: arbitrating Unified Runtime USM allocations; PyTorch caching allocator retained
aimdo_setup_hooks: native Torch XPU allocator retained; arbitrating Unified Runtime USM allocations
comfy-aimdo WDDM adapter match: Intel(R) Arc(TM) B580 Graphics runtime_luid=00000000:00009846 dxgi_luid=00000000:00009846
comfy-aimdo inited for GPU: Intel(R) Arc(TM) B580 Graphics (VRAM: 11875 MB)
comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend
comfy-aimdo XPU backend ready (mode=native_hook)
```

---

## 3. 真机 ComfyUI 进程（带 `--enable-dynamic-vram`）：XPU 路径全绿

`outputs/aimdo_comfyui_launch.log` 逐字证据：

| 要求（任务 #11 原文） | 命中行 |
|---|---|
| DynamicVRAM detected and enabled | **L43** `[INFO] DynamicVRAM support detected and enabled` |
| SYCL queue published | L10 `comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend` |
| WDDM 匹配 | L41 `WDDM adapter match: ... runtime_luid=00000000:00009846 dxgi_luid=00000000:00009846`（两侧相等） |
| 目标 GPU 上下文建立 | L42 `comfy-aimdo inited for GPU: Intel(R) Arc(TM) B580 Graphics (VRAM: 11875 MB)` |
| 设备枚举 | L38 `Device: xpu:0 Intel(R) Arc(TM) B580 Graphics` |
| 后端模式 | L12 `comfy-aimdo XPU backend ready (mode=native_hook)` |
| 版本一致 | L46/L52 `comfy-aimdo version: 0.5.6.dev25` |
| OmniXPU 显存比例 | L24 `XPU allocator memory fraction applied during prestartup: requested=0.99 actual=0.989999999958` |
| 干净退出 | L92 `comfy-aimdo XPU: restored the torch.xpu entry points` |

---

## 4. 阻塞项一：ComfyUI `nodes.py` 启动崩溃（**与 AIMDO 无关，已隔离证明**）

`--enable-dynamic-vram` 与 `--disable-dynamic-vram` **两种模式都以同一堆栈崩溃**，服务器横幅
`To see the GUI go to` 从未出现：

```
File "...\ComfyUI\main.py", line 616, in <module>
    event_loop, prompt_server, start_all_func = start_comfyui()
File "...\ComfyUI\main.py", line 551, in start_comfyui
    asyncio_loop.run_until_complete(nodes.init_extra_nodes(...))
File "...\ComfyUI\nodes.py", line 2641, in init_extra_nodes
    await init_external_custom_nodes()
File "...\ComfyUI\nodes.py", line 2430, in init_external_custom_nodes
    if refusal is not None:
       ^^^^^^^
NameError: name 'refusal' is not defined
```

**隔离证据**：`--disable-dynamic-vram`（AIMDO 关闭）运行同样崩在此行（`outputs/comfyui_noAIMDO.log`）。
故该崩溃与 AIMDO/XPU **完全无关**。

**根因**：`git -C ComfyUI diff -- nodes.py` 显示相对 v0.39.0（HEAD `b0b7435`）存在**未提交的本地编辑**，其中两处是半成品：

```python
@@ -2385,6 +2427,10 @@ async def init_external_custom_nodes():
+            if refusal is not None:      # ← refusal 从未被赋值
+                logging.warning(refusal)
+                continue

@@ -2544,6 +2590,8 @@ async def init_builtin_extra_nodes():
+        "nodes_camera.py",               # ← 文件不存在
+        "nodes_camera_angle.py",         # ← 文件不存在
```

- `refusal` 在 `nodes.py` 全文仅出现在 2430/2431 两行，**从未赋值** → 无条件 `NameError` → 致命。
- `comfy_extras/nodes_camera.py` / `nodes_camera_angle.py` **在磁盘上不存在** → `load_custom_node()`
  的 `sys_module_name` 未绑定（`nodes.py:2311` `UnboundLocalError`，仅 WARNING）。
- 同一 diff 还含一处**无关但可能有意**的改动：`VAEDecode/VAEDecodeTiled` 增加
  `_reject_non_finite_video()` 非有限值守卫。该改动本身不导致崩溃，但说明 `nodes.py` 被多来源编辑。

**最小修复（任选其一）**：
1. 删除 `nodes.py:2430-2432` 的 `if refusal is not None:` 三行（`refusal` 是死代码）；并
2. 删除内置列表里 `nodes_camera.py` / `nodes_camera_angle.py` 两行（或补齐对应文件）。

> 归属：`nodes.py` 是 ComfyUI core，非 XPU/aimdo 代码。本报告只做隔离与定位，未改动该文件
> （其为他人/其他工作流在编辑中，存在 `main.py.bak.20261009-010833` 等痕迹）。

---

## 5. 阻塞项二：comfy_aimdo `_xpu_opt_in()` 第二段门禁

真机 ComfyUI 在 `main.py:74` 调 `control.init(simple_vram_headroom=..., nvml_pressure=...)`，
**不带** `implementation` → `implementation_was_explicit=False` → `xpu.setup_backend(..., explicitly_requested=False)`。

`xpu.py:430`：
```python
if not explicitly_requested and not _xpu_opt_in():
    logging.info("comfy-aimdo XPU backend not requested; using native PyTorch XPU allocator")
    return False
```
`xpu.py:483` `_xpu_opt_in()` 仅在 `AIMDO_XPU_ENABLED=="1"` 或 `comfy.cli_args.args.enable_dynamic_vram` 为真时返回 True。

**结果**（与 runtime-verifier §5 一致）：
- 带 `--enable-dynamic-vram`：`_xpu_opt_in()==True` → 后端激活 → 全绿（§3）。
- 不带 flag：`_xpu_opt_in()==False` → 提前 `return False` → `No working comfy-aimdo install detected. DynamicVRAM support disabled.`（明确记录，非静默）。

**归因**：ComfyUI 侧的 Intel 门禁补丁（`comfyui-intel-gate.patch` 给 `main.py::dynamic_vram_supported()`
加 `is_intel_xpu()`）让控制流进入了 `if args.enable_dynamic_vram or (enables_dynamic_vram() and dynamic_vram_supported())`
分支，但**无法越过 comfy_aimdo 自己的 `_xpu_opt_in()`**。两段门禁未对齐。

**最小修复（建议，供 owner 取舍）**：让 Intel XPU 路径被视为"显式请求"，或让 `_xpu_opt_in()` 与
ComfyUI 门禁采用同源判据（例如 `control.init()` 在门禁判定通过时传 `implementation="xpu"`，
即置 `explicitly_requested=True`）。具体改法属 comfy_aimdo 设计决策，此处只给方向。

---

## 6. 未验证项（明确列出）

- **含大模型加载的工作流 / 三层卸载压力往返**：**未验证**。原因：
  1. ComfyUI 因 `nodes.py` 无法启动（§4）；
  2. 即便可启动，`ComfyUI/models/checkpoints` 与 `diffusion_models` 为空目录
     （仅 `vae_approx/*.safetensors` 小文件），**无任何可用 checkpoint**，无法构造大模型加载工作流；
  3. 验收契约（`docs/WINDOWS_XPU_BUILD_TEST_ACCEPTANCE.md`）要求"真正达到压力"的配置
     （长视频全步数）才能主张三层卸载有效，当前环境不具备。
- 因此**不对**三层卸载/压力回收/性能做任何主张。

---

## 7. 清理

- 组件冒烟：进程内 `deinit()` 已调用；无残留。
- 两次 ComfyUI 启动：已 `kill`/`kill -9`；两次均因 `NameError` 自行退出（kill 为兜底）。
- 未改动 site-packages、未重装任何包；仅新增 `outputs/` 下探针与日志。

---

## 附：产物索引

- `outputs/e2e_deployed_smoke.py` / `outputs/e2e_deployed_smoke.json`（组件级 14/14 PASS）
- `outputs/aimdo_comfyui_launch.log`（带 flag 的真机 ComfyUI 启动日志）
- `outputs/comfyui_noAIMDO.log`（`--disable-dynamic-vram` 隔离对照）
- `outputs/e2e_probe_env.py`（OmniXPU provider 入口点探针）
