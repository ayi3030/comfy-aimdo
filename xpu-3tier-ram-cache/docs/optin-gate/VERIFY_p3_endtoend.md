# P3 真机端到端独立复验报告 — comfy-aimdo XPU @ Intel Arc B580

> ## ⚠️ 后续事实（2026-10-09 追加；**不改动下方原文**）
> 本文 §0 / §5 / §9.5 的「未通过验收 / 门禁 = NO / 仍未解决」为**当时**结论；两个问题**均已修复**，据此判断「当前仍未通过」会与事实相反：
> - 门禁（§5 / §9.3）：已由 **`dev28` / `c2cf98fb9`**（非阻断 `_xpu_opt_in()`）修复；免 flag 自动启用应转 PASS（**待 P4 部署产物级复验**）。
> - **【2026-10-09 补记】P4 部署产物级复验已完成且全绿（证据 `logs/acc_20261009-015303_*`）**：免 flag 自动启用与真模型工作流均通过；上文「（待 P4 …复验）」依此**升级为「已验收通过」**。
> - 启动阻塞（§6）：已由 task #14 修复（本文 §9 已记录）。
> - 下方**数值 / sha / 行号 / 日志一律保留**。归属：intel-xpu-adapter。

- 验证者：runtime-verifier（独立复验，非部署方）
- 时间：2026-10-09 01:21 ~ 01:24（本地）
- 主机：Windows 11，Intel(R) Arc(TM) B580 Graphics，torch 2.14.0+xpu，oneAPI 2026.1.0（sycl9.dll）
- 目标：证明补丁后的 ComfyUI 在 Intel Arc 上**无** `--enable-dynamic-vram` 时自动启用 DynamicVRAM，且 XPU 后端真实初始化（非静默回退）。
- 结论速览：**未通过验收**。存在两个独立问题：
  1. **门禁补丁本身已加载并生效，但其"免 flag 自动启用"的目标未达成** —— 被 comfy_aimdo 自身的 opt-in 门（`xpu.py::_xpu_opt_in`）挡住。**【复跑后仍未解决，见第 9 节】**
  2. **ComfyUI 在两种模式下都无法完成启动** —— `nodes.py` 存在无关的 `NameError`，服务器横幅从未出现。**【已被 comfyui-python-engineer-2 修复，复跑确认已解决，见第 9 节】**

> **复跑更新（2026-10-09 01:31，见第 9 节）**：启动阻塞项已修复，两种模式均到达 `To see the GUI go to`。但**第 1 项（免 flag 自动启用）仍然 FAIL** —— comfy_aimdo 的 `_xpu_opt_in()` 未修，no-flag 依旧输出 `No working comfy-aimdo install detected`。整体验收结论不变：**未通过**。

---

## 1. 部署状态（Read-only 比对）

| 项 | 值 |
|---|---|
| ComfyUI | v0.39.0（portable），`HEAD detached at v0.39.0`，commit `b0b7435` |
| comfy_aimdo 版本 | `0.5.6.dev25` |
| comfy_aimdo commit | `g9b1efd905` |
| aimdo_xpu.dll | 238592 B |
| aimdo_xpu.dll sha256 | `b4241291e962450ed71cd0da9a2a00ff5b1de4e26d86f6b434bc52af5011ce2c` |
| `_version.py` mtime | 2026-10-09T01:12:11 |
| `aimdo_xpu.dll` mtime | 2026-10-09T01:12:11 |

**运行晚于部署（进程加载的是已部署字节）**：三次运行的子进程起始时间为 `01:21:58` / `01:22:56` / `01:23:46`，均晚于部署 mtime `01:12:11`。ComfyUI 在启动时导入这些文件，故运行进程确实加载了上述字节。

`main.py` 相对 v0.39.0 的改动经 `git diff main.py` 确认**仅**为 Intel 门禁补丁（`dynamic_vram_supported()` 增加 `is_intel_xpu()` 分支），与交付的 `comfyui-intel-gate.patch` 一致。

---

## 2. Harness 审查与修复（`verify_runtime.py`）

审查发现 3 个真实缺陷，已修复（编辑 `verify_runtime.py` 获授权）：

1. **过早 kill（会截断关键证据）**：原实现把 `DynamicVRAM enabled/disabled` 一行当作终止条件。该行在设备枚举与服务器横幅**之前**输出，一旦命中即 kill，会漏掉设备行、XPU 队列发布行及后续崩溃堆栈。→ 改为等待 `To see the GUI go to` / `Starting server` 全量启动，仅在致命模式或进程退出时提前终止，并追加 flush 宽限期。
2. **`port_in_use()` 崩溃**：`netstat -ano` 输出为 OEM/非 UTF-8 字节，`subprocess.run(text=True)` 解码失败 → 读取线程异常 → `stdout=None` → `TypeError`。→ 加 `errors="replace"`。
3. **`taskkill` 同类解码风险**：加 `errors="replace"` 保持健壮。

其余逻辑审查通过：`taskkill /F /T /PID` 杀整棵进程树；cwd 为 portable 根；PATH 前置 `python_embeded\Library\bin`（保证 sycl9.dll 解析）；`PYTHONIOENCODING=utf-8`。清理后确认无残留 python、8188 空闲。

---

## 3. 三次运行

命令（精确）：

```
# Run 1 — no-flag
"E:\HE\WorkBuddyData\binaries\python\versions\3.13.12\python.exe" \
  "C:\Users\HE\WorkBuddy\2026-10-08-23-50-54\aimdo-xpu\verify_runtime.py" --mode no-flag --timeout 300
# Run 2 — flag
... --mode flag --timeout 300
# Run 3 — 诊断：no-flag + 仅环境变量（无 --enable-dynamic-vram）
AIMDO_XPU_ENABLED=1 "E:\...\python.exe" "...\verify_runtime.py" --mode no-flag --timeout 300
```

日志：
- Run 1：`logs\p3_noflag_20261009-012158.log`（pid 17468）
- Run 2：`logs\p3_flag_20261009-012256.log`（pid 12228）
- Run 3：`logs\p3_noflag_20261009-012346.log`（pid 4764）

三种运行均以 `taskkill /F /T` 结束；**均未出现 `To see the GUI go to` / `Starting server`**（见第 6 节阻塞项）。

---

## 4. Marker 表（PASS/FAIL + 原始日志证据）

| # | Marker | no-flag | flag | 原始证据（逐字） |
|---|---|---|---|---|
| A | DynamicVRAM enabled | **FAIL** | **PASS** | flag L43：`[INFO] DynamicVRAM support detected and enabled`；no-flag 无此行 |
| B | 无 "No working comfy-aimdo" | **FAIL** | **PASS** | no-flag L41：`[WARNING] No working comfy-aimdo install detected. DynamicVRAM support disabled. Falling back to legacy ModelPatcher. VRAM estimates may be unreliable especially on Windows` |
| C | GPU 命名 Arc/B580 | PASS | PASS | 两者均：`[INFO] Device: xpu:0 Intel(R) Arc(TM) B580 Graphics` |
| D | SYCL queue published | **FAIL** | **PASS** | flag L10：`[INFO] comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend`；no-flag 无此行 |
| E | 无 ABI mismatch | PASS | PASS | 无 `ABI mismatch` |
| F | 无致命错误模式 | **FAIL** | **FAIL** | 两者均含 `Traceback (most recent call last)` → `NameError: name 'refusal' is not defined`（见第 6 节） |
| G | torch 预导入告警（信息性/良性） | PASS | PASS | `[WARNING] Potential Error in code: Torch already imported, torch should not be imported before this point.`（OmniXPU prestartup 导入 torch 所致） |
| H | 运行晚于部署 dll | PASS | PASS | run 01:21:58/01:22:56 ≥ dll 01:12:11 |
| I | commit == g9b1efd905 | PASS | PASS | `_version.py` 实测 `g9b1efd905` |

**XPU 后端是否真实初始化（非静默回退）**：
- **flag 模式 = 真实初始化，非回退**。证据链（flag 日志）：
  - L10 `comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend`
  - L11 `aimdo_xpu_ur_hook_install: arbitrating Unified Runtime USM allocations; PyTorch caching allocator retained`
  - L12 `comfy-aimdo XPU backend ready (mode=native_hook)`
  - L41 `comfy-aimdo WDDM adapter match: Intel(R) Arc(TM) B580 Graphics runtime_luid=00000000:00009846 dxgi_luid=00000000:00009846`
  - L42 `comfy-aimdo inited for GPU: Intel(R) Arc(TM) B580 Graphics (VRAM: 11875 MB)`
  - （Windows 上 `native_hook` 模式保留 PyTorch 缓存分配器、以 UR-USM 钩子仲裁，是设计路径，不是回退。）
- **no-flag 模式 = 明确回退**（且已记录，非静默）：no-flag L10 `comfy-aimdo XPU backend not requested; using native PyTorch XPU allocator`。未回退到 CUDA/CPU，仍是 XPU 原生分配器，但 AIMDO 的 DynamicVRAM 未接管。

---

## 5. Gate-proof 裁决（关键问题）

**提问**：无 `--enable-dynamic-vram` 时，是否输出 `DynamicVRAM support detected and enabled`？

**答复：否。** 该行在 no-flag 模式下**不出现**（Run 1）。出现的是 `No working comfy-aimdo install detected. DynamicVRAM support disabled.`（no-flag L41）。

**为什么 —— 两段式门禁，补丁只过第一段**：

1. **第一段（ComfyUI 门禁）—— 补丁已加载且生效。** 证明：no-flag 模式输出了 `No working comfy-aimdo install detected`，该行位于 `main.py:311`，只有进入 `main.py:280` 的 `if args.enable_dynamic_vram or (enables_dynamic_vram() and dynamic_vram_supported()):` 分支、且 `torch_version_numeric >= (2,8)`（本机 2.14）才可能输出。在无 NVIDIA/AMD 的纯 XPU 机上，`dynamic_vram_supported()` 之所以返回 True，唯一可能来源就是补丁新增的 `is_intel_xpu()` 分支（`main.py:275`）。**故 main.py 门禁补丁确被加载并改变了控制流。** 若补丁缺失，`dynamic_vram_supported()` 为 False，该块整体跳过，`main.py:309/311` 两行都不会出现。

2. **第二段（comfy_aimdo 自身 opt-in 门）—— 挡住了实际启用。** `control.init()`（`main.py:74-84`）→ `xpu.py::setup_backend()` 在 `xpu.py:430` 判定：

   ```python
   if not explicitly_requested and not _xpu_opt_in():
       logging.info("comfy-aimdo XPU backend not requested; using native PyTorch XPU allocator")
       return False
   ```

   而 `_xpu_opt_in()`（`xpu.py:483`）仅在 `AIMDO_XPU_ENABLED == "1"` 或 `args.enable_dynamic_vram` 为真时返回 True。no-flag 下两者皆假 → `setup_backend` 提前返回 False → **SYCL 队列从未发布**（no-flag L38 `no XPU device published yet; deferring dispatch table wiring until xpu_set_queues()`）→ 随后 `main.py:285` 的 `init_devices()` 因原生后端未接线而失败 → 落到 `main.py:311`，输出 `No working comfy-aimdo install detected`。

**根因隔离（Run 3，决定性）**：保持 no-flag（**不加** `--enable-dynamic-vram`），仅注入 `AIMDO_XPU_ENABLED=1`：

- L10 `comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend`
- L12 `comfy-aimdo XPU backend ready (mode=native_hook)`
- L38 `Device: xpu:0 Intel(R) Arc(TM) B580 Graphics`
- L43 `DynamicVRAM support detected and enabled`

即：**唯一阻塞点就是 comfy_aimdo 的 `_xpu_opt_in()`**。main.py 门禁补丁无法越过它。

**裁决**：
- 「main.py 门禁补丁被加载并生效」= **YES**（证据充分）。
- 「无需 `--enable-dynamic-vram` 即在 Intel 上自动启用 DynamicVRAM」= **NO**（被 `_xpu_opt_in()` 挡住）。
- 因此，就交付目标而言，**门禁补丁不充分 / 未达成端到端自动启用**。

---

## 6. 阻塞项（独立于 aimdo，但阻断验收）

两种模式在启动阶段都崩溃，服务器从未启动。原始堆栈（flag 日志 L72-91，no-flag 相同）：

```
Traceback (most recent call last):
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

`git diff nodes.py` 显示该文件相对 v0.39.0 被改过（+48 行），其中新增：

```python
+            if refusal is not None:
+                logging.warning(refusal)
+                continue
```

`nodes.py` 全文 grep 中 `refusal` **仅出现在 2430/2431 两行，从未被赋值** —— 这是死代码级 `NameError`，无条件在自定义节点循环首轮触发，导致 `init_extra_nodes()` 抛错、`start_comfyui()` 中断、`main.py:616` 退出。**ComfyUI 完全无法启动**（三份日志均无 `To see the GUI go to`）。

同一 diff 还把 `nodes_camera.py` / `nodes_camera_angle.py` 加入内置列表，触发 `load_custom_node()` 的 `sys_module_name` UnboundLocalError（`nodes.py:2311`，仅 WARNING，非致命）。

> 注：该 `nodes.py` 改动与 Intel/XPU/aimdo 无关（内容为视频 VAE NaN 守卫 + camera 节点）。它是否由本团队引入需 team-lead 追溯；但无论来源，它使当前部署**不可启动**，必须先修复（删除这两行或补齐 `refusal` 定义），否则任何端到端验收都不可能通过。

---

## 7. 清理

- 三次运行后：`tasklist` 无残留 python.exe；`netstat` 8188 空闲。
- 仅 kill 了本 harness 启动的进程树（`taskkill /F /T /PID`）。
- 未重装任何包，未改动 `comfy_aimdo` / site-packages / ComfyUI 源码（仅编辑了 `verify_runtime.py`）。

---

## 8. 给 team-lead 的建议（按优先级）

1. **修 `nodes.py` 的 `NameError`**（`nodes.py:2430-2432`）—— 否则 ComfyUI 根本起不来，一切验收无意义。
2. **补第二段门禁**：让 Intel XPU 也能免 flag 自动启用。可选最小改法之一：
   - 在 `comfy_aimdo/xpu.py::_xpu_opt_in()` 内增加对 Intel XPU 生效路径的判定（与 main.py 门禁同风格）；或
   - 在 `main.py` 门禁判定通过时显式调用/设置等价的 opt-in（例如把 Intel 路径视为显式请求），使 `setup_backend` 不再被 `_xpu_opt_in()` 拦截。
3. 修好后再跑 `--mode pairwise` 复验：期望 no-flag 与 flag 都出现 `published N SYCL queue(s)` + `DynamicVRAM support detected and enabled`，且出现 `To see the GUI go to`。

---

## 附：原始日志逐字引用索引

- no-flag 关键行：L10 / L33 / L34 / L36 / L38 / L39 / L40 / L41 / L87 / L89
- flag 关键行：L10 / L11 / L12 / L35 / L36 / L38 / L41 / L42 / L43 / L72-91
- diag（no-flag+AIMDO_XPU_ENABLED=1）关键行：L10 / L12 / L38 / L43 / L89 / L91

---

## 9. 复跑（2026-10-09 01:31，comfyui-python-engineer-2 修复启动阻塞后）

修复项（已在运行树核验，独立确认非口头转述）：
- `nodes.py`：`grep refusal` 无结果（孤儿块已删除）；`grep nodes_camera` 仅剩真实内置 `nodes_camera_trajectory.py`（两个不存在的 `nodes_camera*.py` 条目已删）。
- `comfy/ldm/minimax/model.py:211`：现为 `return comfy.ops.linear_input_act(self.fc2, self.fc1(x), "swiglu")`（旧的 `self.fc2(x, input_act=...)` 已移除）。
- `main.py` 门禁、`comfy_extras/nodes_audio.py` 未动。
- `git diff --stat`：`main.py 4 (+)` / `nodes.py 42 (+)` / `model.py 30 (+)` / `nodes_audio.py 40 (+)`。

命令：`verify_runtime.py --mode pairwise --timeout 300`
日志：`logs\p3_noflag_20261009-013115.log`（pid 16960）、`logs\p3_flag_20261009-013142.log`（pid 6840）

### 9.1 启动阻塞 —— **已解决**

两种模式均到达服务器横幅，无 `Traceback` / `NameError`（check F 由 FAIL 转 PASS）：
- no-flag L90-92：`[INFO] Starting server` / `[INFO] To see the GUI go to: http://127.0.0.1:8188`
- flag L92-94：`[INFO] Starting server` / `[INFO] To see the GUI go to: http://127.0.0.1:8188`

### 9.2 Marker 表（复跑）

| # | Marker | no-flag | flag | 原始证据 |
|---|---|---|---|---|
| A | DynamicVRAM enabled | **FAIL** | PASS | flag L43 `[INFO] DynamicVRAM support detected and enabled`；no-flag 无 |
| B | 无 "No working comfy-aimdo" | **FAIL** | PASS | no-flag L41 `[WARNING] No working comfy-aimdo install detected. DynamicVRAM support disabled. ...` |
| C | GPU 命名 Arc/B580 | PASS | PASS | 两者 `[INFO] Device: xpu:0 Intel(R) Arc(TM) B580 Graphics` |
| D | SYCL queue published | **FAIL** | PASS | flag L10 `[INFO] comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend`；no-flag 无 |
| E | 无 ABI mismatch | PASS | PASS | 无 `ABI mismatch` |
| F | 无致命错误模式 | PASS | PASS | 无 Traceback / NameError |
| G | torch 预导入告警（信息性） | PASS | PASS | 同前 |
| H | 运行晚于部署 dll | PASS | PASS | run 01:31:15 / 01:31:42 ≥ dll 01:12:11 |
| I | commit == g9b1efd905 | PASS | PASS | 同前 |
| J | 服务器启动 | PASS | PASS | 两者 `To see the GUI go to` |

`flag` 模式 XPU 后端真实初始化（非回退）：flag L10 `published 1 SYCL queue(s)`、L12 `XPU backend ready (mode=native_hook)`、L41 `WDDM adapter match ... runtime_luid=00000000:00009846 dxgi_luid=00000000:00009846`、L42 `inited for GPU: Intel(R) Arc(TM) B580 Graphics (VRAM: 11875 MB)`、L43 `DynamicVRAM support detected and enabled`。

### 9.3 门禁裁决（复跑后）—— 不变

no-flag 仍 `DynamicVRAM enabled = False`，网关仍停在 comfy_aimdo：
- no-flag L10 `[INFO] comfy-aimdo XPU backend not requested; using native PyTorch XPU allocator`
- no-flag L38 `[aimdo] aimdo_cuda_runtime_init: no XPU device published yet; deferring dispatch table wiring until xpu_set_queues()`
- no-flag L41 `[WARNING] No working comfy-aimdo install detected. DynamicVRAM support disabled. ...`

即：`main.py:280` 门禁分支仍被进入（证明补丁生效），但 `comfy_aimdo/xpu.py::_xpu_opt_in()` 仍拦截，`setup_backend` 提前返回 False。**该修复未触及第二段门禁，故"免 flag 自动启用"目标仍未达成。**

### 9.4 清理

复跑后：无残留 python.exe，8188 空闲。仅 kill 本 harness 启动的进程树。

### 9.5 复跑结论

- 启动阻塞（第 6 节）：**已解决** ✅
- 免 flag 自动启用（第 5 节交付目标）：**仍未解决** ❌（阻塞点未动）
- 总体验收：**仍未通过**。

---

## 10. 最终验收（dev28，2026-10-09 01:53–01:57）—— 通过（exit 0）

部署：`comfy_aimdo 0.5.6.dev28` / commit `gc2cf98fb9`；`aimdo_xpu.dll` sha256 `fc58a12ca7a2e5dbff2eccef4d7970d58e7934a18e939d5d7111d229fbd543ce`；`xpu.py` md5 `d90ebc8aceedd3364d2ffba182357daa`（与 team-lead 给定期望**逐字节一致**）。

命令：
```
run_acceptance.py --expect-version 0.5.6.dev28 --expect-commit gc2cf98fb9
```
启动命令（cwd=portable 根，**无** `--enable-dynamic-vram`，`AIMDO_XPU_ENABLED` 已从子进程环境剥离）：
`python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --disable-auto-launch --port 8196`

### 10.1 门禁（no-flag）—— 全绿

```
[PASS] required: 'comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend'
[PASS] required: 'comfy-aimdo XPU backend ready (mode=native_hook)'
[PASS] required: 'DynamicVRAM support detected and enabled'
[PASS] forbidden: 'XPU backend not requested' present=False
[PASS] forbidden: 'No working comfy-aimdo install detected' present=False
```
→ 这正是第 5 节 P0 的修复证据：**免 flag 自动启用**已达成。

### 10.2 生成

- small（SD1.5 512×512, 20 步）：`status=success`；产物 375,299 B `ComfyUI\output\p3_small\p3_small_00002_.png`；`Prompt executed in 10.55 seconds`。
- H3（fused_refdelta + 8 步，864×480，length 124）：`status=success`；产物 **1,101,392 B** `ComfyUI\output\p3_h3\p3_h3_00001_.mp4`；`Prompt executed in 198.99 seconds`；progress 条为 `0/8 … 8/8`。
- **主配置通过 → exit 0（未触发回退，非 exit 7）。**

### 10.3 卸载证据（"是否真的在卸载"）

原始行（child log）：
```
[INFO] Model MiniMaxH3TEModel_ prepared for dynamic VRAM loading. 14257MB Staged. ...
[INFO] Model MiniMaxH3 prepared for dynamic VRAM loading. 19995MB Staged. ...
[INFO] Model MiniMaxH3VideoVAE prepared for dynamic VRAM loading. 4965MB Staged. ...
[INFO] Model MiniMaxH3AudioVAE prepared for dynamic VRAM loading. 576MB Staged. ...
[INFO] Model storage policy: fast_disk=True paths=['E:\HE\ComfyUI模型库\diffusion_models\MiniMax H3\minimax_h3_fused_refdelta_r1024_turbo8_mystic07_int8_convrot.safetensors']
[INFO] Model storage policy: fast_disk=True paths=['E:\HE\ComfyUI模型库\text_encoders\MiniMax H3\qwen3vl_32b_minimax_h3_int4_convrot.safetensors']
```
逐出/暂存合计 ≈ 14,257+19,995+4,965+576 = **39,793 MB ≈ 38.9 GiB**，而 VRAM 仅 **11,875 MB**。`prepared for dynamic VRAM loading` + `Staged` 逐模型出现，说明 DynamicVRAM 装载/按需换入路径确实运行。
诚实补充：无 OOM/异常行；`aimdo_lines.txt` 中被压力关键词命中的 5 行均为 `[OmniXPU] AIMDO inference budget: devices=0 minimum=…MiB`（OmniXPU 适配器预算行），**未**发现 comfy-aimdo 核心的显式 evict/offload 计数行——不臆测其含义。

### 10.4 异常（非阻断）

- 用户自定义节点 `custom_nodes\aimdo_diag.py` 导入失败：`comfy_entrypoint … did not return a ComfyExtension, skipping`（IMPORT FAILED）。与本门禁/XPU 无关，仅记录。
- 期间发现并修复了一个**工装缺陷**：模型名必须用 `folder_paths` 的原生分隔符（Windows 反斜杠），我最初的短横/斜杠形式被 live 校验拒绝；已修正 `PROMPT_h3*.json`，并让 `selfcheck_prompts.py` 以原生分隔符严格比对，使其可静态复现该类错误。

### 10.5 产物与日志

- `logs\acc_20261009-015303.log`（子进程合并流，184 行）
- `logs\acc_20261009-015303_runner.log`（编排）
- `logs\acc_20261009-015303_{small,h3}_hist.json`
- `logs\acc_20261009-015303_aimdo_lines.txt`（17 行 aimdo 相关）
- `logs\acc_20261009-015303_artifacts.txt`
- 最终工装 md5：PROMPT_small.json `c2aee77d…` / PROMPT_h3.json `33d69337…` / PROMPT_h3_fallback.json `11ae20f3…` / run_acceptance.py `55e9868d…` / selfcheck_prompts.py `d4ebca33…`

### 10.6 清理

`taskkill /F /T` 收树；无残留 portable python；8196 未 LISTENING。


