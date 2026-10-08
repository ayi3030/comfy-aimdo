# EVIDENCE — `_xpu_opt_in` 门禁竞态：三向矩阵 + 部署态真机实测（冻结记录）

> ## ⚠️ 后续事实（2026-10-09 追加；**不改动下方原文**）
> 本文为**成文时冻结记录**，观测无错；以下两处按字面读会与当前事实相反：
> - §1 表「出货 dev26（已部署）」：现真机已部署 **`dev28` / `c2cf98fb9`**；表中「收敛版（非阻断）→ PASS」的逻辑**已由 dev28 构建上线**。
> - §5 注「收敛版……尚未经 CI 构建与 pip 部署」：**已完成**（dev28）。
> - 下方**数值 / sha / 行号 / 日志一律保留**。归属：intel-xpu-adapter。

- 记录者：intel-xpu-adapter（蓝驭芯）
- 日期：2026-10-09
- 目的：冻结「Intel 免 `--enable-dynamic-vram` 自动启用」失效的证据，供 task #21 修复与回归验收使用。
- 性质：**纯记录，无仓库写入、无 site-packages 改动、无 ComfyUI 运行**（文档成文时各日志已固定）。
- 完整根因链与等价证据源论证由 **task #21 / `EVIDENCE_optin_gate_race.md`** 承担；本文只做证据冻结与可复现记录，不重复展开。

---

## 1. 三向矩阵（真机 B580，均**不带** `--enable-dynamic-vram`）

| xpu.py | `_xpu_opt_in` 守卫 | 结果 | 证据 |
|---|---|---|---|
| 部署 dev25（基线） | **无**（仅 `AIMDO_XPU_ENABLED` / `--enable-dynamic-vram`） | ❌ miss | `_e2e_optin_control.log`（runtime-verifier/python-engineer 侧） |
| **出货 dev26 @`728d6bb`**（已部署） | **硬条件**：`mm` 缺失 → `is_intel=None` → 跳过 → **末尾 `return False`** | ❌ miss | `_e2e_optin_deployed_dev26.log`（部署产物级）+ `_e2e_optin_ship.log`（影子级） |
| 收敛版（task #17，非阻断守卫） | **非阻断**：`mm` 缺失/非 callable/异常 → 不参与判定 → **末尾 `return True`** | ✅ **PASS** | `_e2e_optin_conv.log` / `_e2e_optin_fixed.log` |

> **唯一决定性差异 = 函数末尾 `return False` ↔ `return True`。**
> 出货版把 `comfy.model_management.is_intel_xpu()` 做成硬条件，故在真实启动序列下恒失效；
> 收敛版同一守卫做成非阻断（不可用时放行），故生效。

---

## 2. 部署态真机实测（证据等级最高）

**对象：真机 site-packages 上已部署的 `comfy_aimdo`**，**非**影子包注入。

| 项 | 值 |
|---|---|
| 目标 | `E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\python_embeded\Lib\site-packages\comfy_aimdo` |
| 版本 | `0.5.6.dev26` / `g728d6bb78` |
| `xpu.py` mtime | 2026-10-09 01:39:33 |
| 与 `git 728d6bb:comfy_aimdo/xpu.py` | **逐字节相同**（忽略 CR/LF）→ 硬门禁版 |
| `aimdo_xpu.dll` sha256 | `b1fa82fb2035397daacc65360ed2c7854c6de8a137de485fd0602bfce507a18c` |

**运行条件**：真机 ComfyUI，**未**设 `AIMDO_XPU_ENABLED`，**未**带 `--enable-dynamic-vram`，无影子注入。

**结果（`_e2e_optin_deployed_dev26.log`）**：

| token | 类别 | 行号 | 命中 |
|---|---|---|---|
| `DynamicVRAM support detected and enabled` | 正标志 | — | **miss** ❌ |
| `published 1 SYCL queue(s) to the native backend` | 正标志 | — | **miss** ❌ |
| `comfy-aimdo XPU backend ready (mode=native_hook)` | 正标志 | — | **miss** ❌ |
| `comfy-aimdo XPU backend not requested; using native PyTorch XPU allocator` | 负标志 | L10 | HIT |
| `No working comfy-aimdo install detected. DynamicVRAM support disabled. ...` | 负标志 | L41 | HIT |
| `comfy-aimdo version: 0.5.6.dev26` | 版本行 | L44 / L50 | HIT |
| `To see the GUI go to: http://127.0.0.1:8195` | 横幅 | L92 | HIT |

**结论**：**当前部署的 dev26 未实现「Intel 免 flag 自动启用」** —— 静默失效（不报错，但功能未生效）。

> 注：日志中的 `comfy-aimdo version:` 行来自 **已安装分发元数据**（importlib.metadata），
> 反映 **dist-info** 而非"实际跑了哪份 xpu.py"；判定代码身份请以 §3 的 shadow-path/marker 为准。

---

## 3. 日志清单（相对路径 + sha256 + 关键行号）

相对根目录：`aimdo-xpu/`（本目录）。

| 日志 | sha256 | bytes / lines | 关键行 |
|---|---|---|---|
| `_e2e_optin_deployed_dev26.log` | `2cf5c330620a03ae7312871881f5a5b915dc6b2845b66918bab7f52a1c5f3b06` | 10582 / 92 | L10 `XPU backend not requested`；L41 `No working comfy-aimdo install detected`；L44/L50 `version: 0.5.6.dev26`；L92 横幅（3 正标志全 miss） |
| `_e2e_optin_conv.log` | `a23061966d5da84a351d6e90617773fe8ccc640fef9a94ce6943d2fb0733c801` | 10767 / 94 | L10 `published 1 SYCL queue(s)`；L12 `backend ready (mode=native_hook)`；L43 `DynamicVRAM support detected and enabled`；L94 横幅（3 正标志全 HIT、2 负标志 miss） |
| `_e2e_optin_ship.log` | `f592d47f60f0d007460ca761db5599c074cc62abaeafabf58c7aa44ef7e886ba` | 10829 / 93 | L10 `SHADOW-SHIP-MARKER ... model_management_loaded=False \| is_intel_xpu=None \| cli_args_loaded=True \| enables_dynamic_vram_present=True`；L11 `XPU backend not requested`；L42 `No working comfy-aimdo install detected`；L93 横幅（3 正标志全 miss） |
| `_e2e_optin_fixed.log` | `16b068aa51b7caf51e35de034aeae112dfa3d110e5df708e37061d303ce79e60` | 10767 / 94 | L10 `published 1 SYCL queue(s)`；L12 `backend ready (mode=native_hook)`；L43 `DynamicVRAM support detected and enabled`；L94 横幅（3 正标志全 HIT、2 负标志 miss） |

> 注：`_e2e_optin_fixed.log` / `_e2e_optin_control.log` 由 **comfyui-python-engineer** 的 harness 产出（本记录仅引用）；
> 其余两份（deployed / ship）与 `_e2e_optin_conv.log` 由本人产出。

---

## 4. 可复现 harness 调用（逐字可复跑）

公共前置：`cd E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable`（记为 `ROOT`）；
解释器 `ROOT\python_embeded\python.exe`。
**公共运行环境：`AIMDO_XPU_ENABLED` 未设；argv 中不含 `--enable-dynamic-vram`**（各 supervisor 仅传 `--disable-auto-launch` 与端口）。

### 4.1 部署态 dev26（无影子，最高证据）
```
cd ROOT
ROOT\python_embeded\python.exe -s <D>\_e2e_deployed_supervisor.py
```
supervisor 内实际拉起（逐字）：
```
ROOT\python_embeded\python.exe -s ROOT\ComfyUI\main.py --windows-standalone-build --disable-auto-launch --port 8195
（cwd=ROOT；env 无 AIMDO_XPU_ENABLED；无 --enable-dynamic-vram）
```

### 4.2 收敛版（影子 = dev26 包 + 非阻断 xpu.py）
```
cd ROOT
ROOT\python_embeded\python.exe -s <D>\_e2e_conv_supervisor.py
```
supervisor 内实际拉起：`python.exe -s <D>\_e2e_conv_launch.py --disable-auto-launch --port 8196`；
launcher（`_e2e_conv_launch.py`）：`sys.path.insert(0, <D>\_e2e_pkg_conv)` 后 `runpy.run_path(ComfyUI\main.py)`。

### 4.3 出货版（影子 = dev25 包 + `728d6bb` xpu.py）
```
cd ROOT
ROOT\python_embeded\python.exe -s <D>\_e2e_ship_supervisor.py
```
supervisor 内实际拉起：`python.exe -s <D>\_e2e_ship_launch.py --disable-auto-launch --port 8197`；
launcher（`_e2e_ship_launch.py`）：`sys.path.insert(0, <D>\_e2e_pkg_ship)` 后 `runpy.run_path(ComfyUI\main.py)`。

### 4.4 收敛版（engineer harness，引用）
```
cd ROOT
ROOT\python_embeded\python.exe -s <D>\_e2e_optin_supervisor.py     # → _e2e_shadow_launch.py；port 8199
```

**影子包身份（用于判定实际加载的 xpu.py）**

| 影子包 | 包基线 | `xpu.py` | `xpu.py` sha256 | 构建时间 |
|---|---|---|---|---|
| `_e2e_pkg_ship/` | dev25 (`g9b1efd905`) | `git 728d6bb:comfy_aimdo/xpu.py` | `b9cfb0f7f9d60b2b6f30fff82e568143b9ad133a95c2f65ff6d74ade8e481090` | 2026-10-09 01:38:38 |
| `_e2e_pkg_conv/` | dev26 (`g728d6bb78`) | 收敛版（`PATCH_xpu_opt_in_alignment.SUPERSEDED.patch` 应用到 `9b1efd905`） | `f36eb9dd5560a58ad5f13ddd2a80b18703ebdc8171d0863d994ac866a529c069` | 2026-10-09 01:42:07 |

> `_e2e_optin_ship.log` 的 L10 marker 直接证明加载的是 `_e2e_pkg_ship\comfy_aimdo\xpu.py`（排除"其实跑的是部署版"）。
> `_e2e_optin_conv.log` 未插 marker；其结论由**差异归因**得出：与部署 dev26 **同包、同 harness、同 argv/env**，
> 唯一差异是影子 `xpu.py`；部署 dev26（§2）为 miss，而收敛影子为 PASS，故 PASS 必归因于非阻断 `xpu.py`。

---

## 5. 证据等级声明

| 结论 | 等级 | 说明 |
|---|---|---|
| **部署 dev26 免 flag 不启用** | **部署产物级（强）** | 直接以真机 site-packages 已部署字节运行，无注入；xpu.py 与 `728d6bb` 逐字节相同已核实 |
| 出货 `728d6bb` 硬条件→ miss | 影子包/模块级 | 由 `_e2e_pkg_ship`（dev25 包 + 728d6bb xpu.py）复现，marker 证明加载 |
| **收敛版（非阻断）→ PASS** | **影子包/模块级** | 证明**该修法逻辑成立**；但当时**尚未经 CI 构建与 pip 部署**，故不等于"已上线可用" |
| 部署 dev25 基线 → miss | 影子包/模块级 | 见 `_e2e_optin_control.log` |

> 因此：**"出货版失效"是部署产物级结论；"收敛版修复"是模块级结论**。
> 收敛版升格为"已上线可用"需：CI 重建（dev27）→ pip 部署 → 部署产物级复验（由 runtime-verifier 执行）。

---

## 6. 根因（一句话，引用不展开）

`ComfyUI/main.py:74` 调用 `comfy_aimdo.control.init()`（→ `_xpu_opt_in()`），而 `import comfy.model_management`
在 `ComfyUI/main.py:258` 才执行 → 硬门禁版在该调用点的 `sys.modules.get("comfy.model_management")` 恒为 `None`
→ 条件 3 恒 False。

> 完整根因链、等价证据源论证与逐行分析，见 **task #21 / `EVIDENCE_optin_gate_race.md`**（由 comfyui-python-engineer 撰写），本文不重复。

---

## 附：相关产物

- 发现原始记录：`aimdo-xpu/FINDING_shipping_optin_broken.md`
- 收敛版补丁：`aimdo-xpu/PATCH_xpu_opt_in_alignment.SUPERSEDED.patch`
- harness 源码：`aimdo-xpu/_e2e_deployed_supervisor.py`、`_e2e_conv_supervisor.py`、`_e2e_conv_launch.py`、
  `_e2e_ship_supervisor.py`、`_e2e_ship_launch.py`、`_e2e_optin_supervisor.py`、`_e2e_shadow_launch.py`
- 影子包：`aimdo-xpu/_e2e_pkg_ship/`、`aimdo-xpu/_e2e_pkg_conv/`
