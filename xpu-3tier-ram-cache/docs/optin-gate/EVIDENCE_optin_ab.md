# EVIDENCE — Intel 免 flag 自动启用 DynamicVRAM（A/B 证据）

> ## ⚠️ 后续事实（2026-10-09 追加，task #21/#19 落地后；**不改动下方原文**）
> 下方为**当时观测**，观测本身无错；但据顶部「the fix is being re-landed as task #21」或 §4「fork HEAD 仍 `9b1efd905`」得出「当前仍如此」会与事实相反：
> - 修复**已落地并部署**：非阻断 `_xpu_opt_in()` 随 **`dev28` / `c2cf98fb9`** 上线（真机 site-packages 现为 `0.5.6.dev28`）；免 `--enable-dynamic-vram` 自动启用由此**生效**（待 P4 复验）。
> - **【2026-10-09 补记】免 flag 自动启用已由 P4 验收通过（全绿，证据 `logs/acc_20261009-015303_*`）**；上文「（待 P4 复验）」依此**升级为「已验收通过」**。
> - fork `main` 已由 `9b1efd905` 推进（`728d6bb` → `c2cf98fb9`）。
> - 下方**数值 / sha / 行号 / 日志一律保留**。归属：intel-xpu-adapter。

> **NOT APPLIED to any repo — but this is the CORRECT approach. The shipped dev26 gate (fork 728d6bb78) is the broken one; the fix is being re-landed as task #21.**
>
> ⚠️ **证据等级：模块级（module-level）证明，非「部署产物验收」。** 下列 A/B 用「影子包（整包复制安装版 `comfy_aimdo`，仅替换 `xpu.py`）+ 运行期 `sys.path` 注入」在真机 B580 上跑出，**未改动 site-packages、未改动任何 clone 工作树、未推送**。因此它证明的是**该补丁逻辑**在真机 ComfyUI 进程里的行为，**不构成**对 dev26 构建产物的验收。

- 关联任务：#17（实现+验证）/ #19（落地 728d6bb，门禁已坏）/ **#21（修复）**
- 补丁（未应用，且经复核确认为**正确**做法）：`aimdo-xpu/PATCH_xpu_opt_in_alignment.patch`（+68/−7）。出货版 `728d6bb78` 的门禁是**坏的**（根因与双日志见 `EVIDENCE_optin_gate_race.md`）。
- 真机：Windows 11 + Intel Arc B580；`python_embeded`（Python 3.13.14）；torch 2.14.0+xpu

---

## 0. 结论

不带 `--enable-dynamic-vram` 时：**FIXED（改后 xpu.py）** 后端自动激活；**CONTROL（原样 xpu.py）** 被第二段门禁拦住并回退。二者唯一差异 = `xpu.py` 一处 → 归因明确。

---

## 1. 方法（可复现）

1. 复制安装版整包为影子包：`cp -r <site-packages>/comfy_aimdo  <_e2e_pkg>/comfy_aimdo`
2. 仅替换：`cp <fork>/comfy_aimdo/xpu.py  <_e2e_pkg>/comfy_aimdo/xpu.py`
   - FIXED 用改后版本；CONTROL 用安装版原样（`diff` 证明与 site-packages 逐字节相同）。
3. 运行期注入（绕开嵌入式 `python313._pth` 的隔离）：`_e2e_shadow_launch.py` 先 `sys.path.insert(0, SHADOW)`，再 `runpy` 执行 `ComfyUI/main.py`。
4. 有界监督 `_e2e_optin_supervisor.py`：拉起→等待横幅/超时→`taskkill /F /T` 收树→落日志。
5. 命令等价于：`python_embeded\python.exe -s _e2e_shadow_launch.py --disable-auto-launch --port 8199`（**无** `--enable-dynamic-vram`）。
6. 事后：删除影子包；核验 site-packages 的 `comfy_aimdo/xpu.py` 与 git HEAD blob（CRLF 归一化）逐字节相同（未修改）。

---

## 2. A/B 日志证据

### 2.1 FIXED（改后 xpu.py） — `aimdo-xpu/_e2e_optin_fixed.log`
```
10:[INFO] comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend
12:[INFO] comfy-aimdo XPU backend ready (mode=native_hook)
43:[INFO] DynamicVRAM support detected and enabled
94:[INFO] To see the GUI go to: http://127.0.0.1:8199
```

### 2.2 CONTROL（原样 xpu.py） — `aimdo-xpu/_e2e_optin_control.log`
```
10:[INFO] comfy-aimdo XPU backend not requested; using native PyTorch XPU allocator
41:[WARNING] No working comfy-aimdo install detected. DynamicVRAM support disabled.
           Falling back to legacy ModelPatcher. VRAM estimates may be unreliable especially on Windows
92:[INFO] To see the GUI go to: http://127.0.0.1:8199
```

### 2.3 对照表

| 检查 | FIXED | CONTROL |
|---|---|---|
| `published 1 SYCL queue(s)` | ✅ | miss |
| `XPU backend ready (mode=native_hook)` | ✅ | miss |
| `DynamicVRAM support detected and enabled` | ✅ | miss |
| `XPU backend not requested` | miss | ✅ |
| `No working comfy-aimdo install detected` | miss | ✅ |
| 服务器横幅 | ✅ | ✅ |
| `NameError` / `Traceback` | 无 | 无 |

---

## 3. 单元矩阵（12/12，`_xpu_opt_in` 逻辑）

以 AST 抽取 `_xpu_opt_in`，注入伪 `comfy.cli_args` / `comfy.model_management`：

| # | `AIMDO_XPU_ENABLED` | `enables_dynamic_vram()` | `is_intel_xpu()` | 结果 | 期望 |
|---|---|---|---|---|---|
| 1 | 未设 | 无 `comfy.cli_args` | — | False | ✅ |
| 2 | `1` | — | — | True | ✅ |
| 3 | `0` | — | — | False | ✅ |
| 4 | 未设 | True | True | **True** | ✅ |
| 5 | 未设 | True | False | False | ✅ |
| 6 | 未设 | True | 未加载/异常 | True（不阻断） | ✅ |
| 7 | 未设 | False（如 `--disable-dynamic-vram`） | True | False | ✅ |
| 8 | `0` | True | True | False（强制关优先） | ✅ |
| 9 | 未设 | 旧版无判据，flag=True | True | True | ✅ |
| 10 | 未设 | 旧版无判据，flag=False | True | False | ✅ |
| 11 | 未设 | 判据抛异常，flag=True | True | True（回退+告警） | ✅ |
| 12 | 未设 | 判据抛异常，flag=False | True | False | ✅ |

**真实模块集成**：真机 `python_embeded` 导入真实 `comfy.cli_args` + `comfy.model_management` →
`enables_dynamic_vram()=True`、`is_intel_xpu()=True`（`cpu_state=GPU, xpu_available=True`）、`_xpu_opt_in()=True`。

---

## 4. 触及面声明（合规）

- **site-packages 未改**：已核验安装版 `comfy_aimdo/xpu.py` 与 git HEAD blob 逐字节相同。
- **未改任何 clone 工作树以外的内容**；fork 克隆 `comfy-aimdo-upstream/comfy_aimdo/xpu.py` 的改动为**本地未提交**（HEAD 仍 `9b1efd905`，无本地 commit）。
- **未推送**：无任何 push。
- 影子包已清理。
- 本证据**不构成部署产物验收**（见页首 caveat）。
