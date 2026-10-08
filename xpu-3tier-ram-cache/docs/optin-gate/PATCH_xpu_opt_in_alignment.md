> **NOT APPLIED to any repo — but this is the CORRECT approach. The shipped dev26 gate (fork 728d6bb78) is the broken one; the fix is being re-landed as task #21.**

# task #17 / #19 — 对齐两段 DynamicVRAM 门禁（Intel 免 flag 自动启用）

- 实现者：comfyui-python-engineer（顾守成）
- 日期：2026-10-09
- 目标：Intel Arc 在**不带** `--enable-dynamic-vram` 时也能自动启用 DynamicVRAM
- 结论：**已修复并经真机 B580 A/B 实测验证（PASS）**
- 补丁：`aimdo-xpu/PATCH_xpu_opt_in_alignment.patch`（改 `comfy_aimdo/xpu.py` 单文件，+68/−7）
- 与 task #19 的关系：本文实现与 #19 规格**已收敛为同一版本**（见 §1.1），#19 负责 fork push + CI 重建 + 部署。

---

## 1. 决策：采用候选方向 (b)（改 comfy_aimdo 侧，令两段门禁同源）

任务给出的三个方向中，选 **(b)**：让 `xpu.py::_xpu_opt_in()` 与 ComfyUI 门禁采用**同源判据**。

- **(a)**（main.py 在门禁通过时对 `control.init` 传 `implementation="xpu"`）需改 **ComfyUI core `main.py`**，与约束「此改动属 comfy_aimdo 侧设计决策」不符。
- **(c)**（ComfyUI 侧设置等价 opt-in 环境/参数）同样要改 ComfyUI core，且引入隐式 env 耦合。
- **(b)** 完全落在 `comfy_aimdo/xpu.py` 内，无需改 ComfyUI core；让两段门禁**读同一判据**，从根上消除「对不上」。

> 关键：ComfyUI 侧 `main.py::dynamic_vram_supported()` 是**按厂商**判定的，已对 Intel XPU 返回 True。
> 任何走到 `setup_backend()` 的进程，都已经过 ComfyUI 自己那道门；此处不该再用一道更严的门把它拒掉。

### 1.1 与 task #19 规格的收敛
task #19 的规格是 `enables_dynamic_vram() AND is_intel_xpu()`。为**避免两个任务产出分歧版本**，本实现已按 #19 规格收敛：
激活条件 = `enables_dynamic_vram()` **且** `is_intel_xpu()`（后者在不可用/异常时不阻断）。
本机实测 `enables_dynamic_vram()=True`、`is_intel_xpu()=True`（`cpu_state=GPU, xpu_available=True`），故两种写法在本机行为一致；加上 `is_intel_xpu()` 只是多一道「torch 版本写着 xpu 但无可用设备」的确认。

---

## 2. 改动（`comfy_aimdo/xpu.py::_xpu_opt_in`）

判据按序：

1. `AIMDO_XPU_ENABLED=1` → 强制开；`=0` → **强制关**（新增逃生舱）。
2. ComfyUI 原生门禁：`comfy.cli_args.enables_dynamic_vram()` **且** `comfy.model_management.is_intel_xpu()`。
   - 无 `enables_dynamic_vram()`（旧版 ComfyUI）→ 退回 `args.enable_dynamic_vram` 判据。
   - `enables_dynamic_vram()` 抛异常（args 未就绪）→ 告警并退回旧判据。
   - `is_intel_xpu()` 不可用或抛异常 → **不阻断**（交由 `setup_backend` 的 fail-closed 兜底）。
3. `comfy.cli_args` 不可用（纯库/单测）→ 除 env 外返回 False，**保留旧的保守默认**。

**为什么新增 `AIMDO_XPU_ENABLED=0`**：改动后 XPU 默认开，用户需要「只让 AIMDO 不接管 XPU、但其余 DynamicVRAM 照常」的开关。

**`explicitly_requested` 不变**：显式 `control.init(implementation="xpu")` 仍直接放行，不受本函数影响。

---

## 3. 行为矩阵（单测逐条验证，见 §4.1）

| # | `AIMDO_XPU_ENABLED` | cli `enables_dynamic_vram()` | `is_intel_xpu()` | `_xpu_opt_in()` |
|---|---|---|---|---|
| 1 | 未设 | 无 `comfy.cli_args` | — | **False**（保留旧保守默认） |
| 2 | `1` | — | — | **True** |
| 3 | `0` | — | — | **False** |
| 4 | 未设 | True | True | **True** ← #17/#19 目标 |
| 5 | 未设 | True | False | **False**（无可用设备不启用） |
| 6 | 未设 | True | 未加载/异常 | **True**（不阻断，交 fail-closed） |
| 7 | 未设 | False（如 `--disable-dynamic-vram`） | True | **False** |
| 8 | `0` | True | True | **False**（强制关优先） |
| 9 | 未设 | 旧版无判据，flag=True | True | **True** |
| 10 | 未设 | 旧版无判据，flag=False | True | **False** |
| 11 | 未设 | 判据抛异常，flag=True | True | **True**（回退+告警） |
| 12 | 未设 | 判据抛异常，flag=False | True | **False** |

---

## 4. 验证证据（真机 B580）

### 4.1 单元测试（12/12 符合预期）
AST 抽取 `_xpu_opt_in`，注入伪 `comfy.cli_args` / `comfy.model_management` 覆盖 §3 全部组合。

### 4.2 真实模块集成测试
真机 `python_embeded\python.exe` 导入**真实** `comfy.cli_args` + `comfy.model_management`：
```
enables_dynamic_vram() = True
is_intel_xpu()         = True
cpu_state              = CPUState.GPU
xpu_available          = True
>>> _xpu_opt_in() [modified, no flag] = True
```

### 4.3 端到端 A/B（真机 ComfyUI，均**不带** `--enable-dynamic-vram`）
方法：把安装版 `comfy_aimdo` 整包复制为「影子包」，仅替换 `xpu.py`；用运行期 `sys.path` 注入（`_e2e_shadow_launch.py`）拉起 ComfyUI——**全程未改动 site-packages**。

| 检查 | FIXED（改后，含收敛版） | CONTROL（原样） |
|---|---|---|
| `published 1 SYCL queue(s)` | ✅ HIT | miss |
| `XPU backend ready (mode=native_hook)` | ✅ HIT | miss |
| `DynamicVRAM support detected and enabled` | ✅ HIT | miss |
| `XPU backend not requested` | miss | ✅ HIT |
| `No working comfy-aimdo install detected` | miss | ✅ HIT |
| 服务器横幅 | ✅ | ✅ |

- FIXED 日志：`aimdo-xpu/_e2e_optin_fixed.log`；CONTROL 日志：`aimdo-xpu/_e2e_optin_control.log`
- 唯一差异 = `xpu.py` 一处 → 归因明确。

---

## 5. 约束合规自查

| 约束 | 结论 |
|---|---|
| CUDA/AMD 行为不得改变 | **满足**：`_xpu_opt_in` 仅在 `setup_backend()`（XPU 专属）内被调用 |
| 改动落在 comfy_aimdo 侧 | **满足**：仅改 `comfy_aimdo/xpu.py` |
| 不直改 site-packages | **满足**：E2E 用影子包 + `sys.path` 注入；site-packages 未动（已核实与 git HEAD blob 逐字节相同）；影子包已清理 |
| 向后兼容 | **满足**：无 `comfy.cli_args` 保留旧默认；旧版 ComfyUI 退回 flag；异常有回退+告警 |

---

## 6. 落地方式（task #19：fork push + CI 重建 + 部署）

**基线**：fork `ayi3030/comfy-aimdo` `main` HEAD `9b1efd905`（= 真机 dev25 源码）。

```bash
git -C <fork-clone> apply aimdo-xpu/PATCH_xpu_opt_in_alignment.patch
git -C <fork-clone> checkout -b feat/xpu-auto-enable
git -C <fork-clone> commit -am "fix(xpu): auto-enable DynamicVRAM on Intel without --enable-dynamic-vram"
git -C <fork-clone> push fork feat/xpu-auto-enable   # 或直接推 main，触发 build-xpu-windows.yml
```
- 补丁已用 `git apply --check -R` 验证与 fork 工作区一致。
- 纯 Python 改动，DLL 无源码改动（CI 仍会重编）。
- #19 另含 `run_intel_gpu.bat` 加 `AIMDO_XPU_ENABLED=1`（即时兜底，无需等重建）——由 #19 owner 执行。

---

## 7. 回退

- 用户级：`AIMDO_XPU_ENABLED=0`，或 `--disable-dynamic-vram`。
- 版本级：回滚 dev25（见 `AUDIT_fork_and_artifact.md` D.6）。

---

## 8. 未覆盖 / 待办

- 本次 E2E 为**启动级**（后端激活 + 横幅）；**未**跑含大模型加载的三层卸载压力往返（`checkpoints`/`diffusion_models` 为空），同 #11/#14。
- 本补丁**未**推送 fork、**未**触发 CI 重建（无推送凭据）——属 task #19。
