# ComfyUI Intel-XPU DynamicVRAM 门禁补丁

## 变更概览

| 项 | 值 |
|---|---|
| 目标文件 | `E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\ComfyUI\main.py` |
| 备份路径 | `E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\ComfyUI\main.py.bak.20261009-010833` |
| 改动函数 | `dynamic_vram_supported()`（原 :267，改后 :267-277） |
| 改动行数 | +4 行（2 行中文注释 + 2 行代码），无删除、无其他函数改动 |
| 兼容性 | CUDA / AMD / CPU 路径行为完全不变 |

## 改动点

`dynamic_vram_supported()` 原先只识别 NVIDIA 与 ROCm≥7.14 的 AMD，Intel XPU 永远返回 False，
导致 Intel Arc 用户必须显式传 `--enable-dynamic-vram` 才能启用 DynamicVRAM。
本补丁追加一个 Intel 分支，使 XPU 可用时返回 True，与 NVIDIA 分支同风格（不做多余版本判断）。

> ⚠️ **必要 ≠ 充分（2026-10-09 更正）**：本分支让 **ComfyUI 侧**门禁为 Intel XPU 放行，是**必要**的一环，
> 但**单独打它不足以**让 Intel 免 flag 自动启用 —— 还需 `comfy_aimdo` 侧的**第二段门禁** `_xpu_opt_in()` 放行，
> 其修复（非阻断守卫）直至 **`dev28` / `c2cf98fb9`** 才落地。详见文末「B580 自动启用结论」。

## 统一 diff

```diff
--- main.py.bak.20261009-010833	2026-10-09 01:08:33.622901500 +0800
+++ main.py	2026-10-09 01:08:38.943262900 +0800
@@ -270,6 +270,10 @@
     if comfy.model_management.is_amd():
         if comfy.model_management.rocm_version >= (7, 14):
             return True
+    # Intel XPU (Arc) 路径：is_intel_xpu() 内部已保证仅当 XPU 真实可用时才返回 True，
+    # 故与 NVIDIA 分支同风格直接放行，无需额外的版本判断，保持最小改动。
+    if comfy.model_management.is_intel_xpu():
+        return True
     return False
```

## 改后函数（原文回读，main.py :267-277）

```python
def dynamic_vram_supported():
    if comfy.model_management.is_nvidia():
        return True
    if comfy.model_management.is_amd():
        if comfy.model_management.rocm_version >= (7, 14):
            return True
    # Intel XPU (Arc) 路径：is_intel_xpu() 内部已保证仅当 XPU 真实可用时才返回 True，
    # 故与 NVIDIA 分支同风格直接放行，无需额外的版本判断，保持最小改动。
    if comfy.model_management.is_intel_xpu():
        return True
    return False
```

## 依赖符号确认

`comfy.model_management.is_intel_xpu()` 在本机 ComfyUI 中确实存在：

```
E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\ComfyUI\comfy\model_management.py:161:def is_intel_xpu():
    global cpu_state
    global xpu_available
    if cpu_state == CPUState.GPU:
        if xpu_available:
            return True
    return False
```

即该函数内部已含 `xpu_available` 守卫，仅在 XPU 真实可用时返回 True，因此补丁分支不会在非 Intel 机器上误触发。

## 验证结果

- `..\python_embeded\python.exe -m py_compile main.py` → 退出码 0（**通过**），无语法错误。
- diff 与备份逐行比对，仅新增 4 行，CUDA/AMD 分支的代码与缩进保持原样。

## 会话镜像（只读检查，未修改）

`C:\Users\HE\WorkBuddy\2026-10-08-05-10-30\ComfyUI-upstream\main.py` **存在**，
且其 `dynamic_vram_supported()`（:273-280）为**同样的未打补丁版本**（无 Intel 分支）。
本补丁**未**改动该镜像文件。

## B580 自动启用结论

> ⚠️ **2026-10-09 更正（task #21/#19 落地后）**：本节初版曾断言「打上本补丁后，Intel Arc B580 在默认启动条件下将**自动启用 DynamicVRAM，无需再传 `--enable-dynamic-vram`**」。**该断言在写作时为假** —— 本补丁只是**必要条件，不是充分条件**；真正自动启用还需 `comfy_aimdo` 侧**第二段门禁**放行，其修复直至 **`dev28` / `c2cf98fb9`** 才落地。

打上本补丁后，`ComfyUI main.py` 的门禁会为 Intel XPU 放行（**必要**一环）。但要让 Intel Arc 免 `--enable-dynamic-vram` **真正自动启用 DynamicVRAM**，需**同时**满足以下前置（列全，含初版遗漏的第二段门禁）：

1. `enables_dynamic_vram()` 返回 True —— 即**未**设置 `--disable-dynamic-vram` / `--highvram` / `--gpu-only` / `--novram` / `--cpu`（cli_args.py:326-329）。
2. `comfy.model_management.torch_version_numeric >= (2, 8)` —— 否则 main.py:277 会走「Unsupported Pytorch」告警分支，回退 legacy ModelPatcher，不执行 `init_devices`。
3. **【第二段门禁 · 初版遗漏，关键】** `comfy_aimdo` 侧 `_xpu_opt_in()` 必须放行。调用链：
   `ComfyUI main.py:74 → comfy_aimdo.control.init() → xpu.setup_backend() → _xpu_opt_in()`。
   **该调用点早于 `main.py:258` 的 `import comfy.model_management`** —— 故任何依赖 `comfy.model_management.is_intel_xpu()` 的**硬条件**都会在此处恒为 False。
   - 含该缺陷的出货版 **`dev26`（`728d6bb`）实测免 flag 不启用**（失败态证据：`_e2e_optin_ship.log:11` `XPU backend not requested`、`:42` `No working comfy-aimdo install detected`）。
   - 修复版 **`dev28`（`c2cf98fb9`）**把条件 3 改为**非阻断守卫**（`comfy.model_management` 未导入 / 非 callable / 抛异常 → 放行），故免 flag 可自动启用（当前真机 site-packages 即 `0.5.6.dev28`）。
4. 运行期 `comfy_aimdo.control.init_devices(...)` 返回真值（comfy-aimdo 安装可用）。

> **结论修正**：本补丁**必要但单独不足**。若只打 `main.py` 而不把 `comfy_aimdo` 升到 **≥ `dev28`**，会出现「修了但没效果」的**静默失败** —— 控制流进了门禁分支，却被第二段门禁提前 `return False`。
> 完整根因见 `FINDING_shipping_optin_broken.md` / `EVIDENCE_optin_gate_race.md`；修复落地见 `AUDIT_fork_and_artifact.md` §F。

## 影响面

- **CUDA / CPU 路径：无影响**——未改动的分支代码逐字节保持。
- **AMD 路径：无影响**——ROCm 版本判断保持原样。
- **无 Intel 环境：无影响**——Intel 分支位于 `return False` 之前，仅当 `is_intel_xpu()` 为 True 时命中；非 Intel 机器该函数返回 False，与打补丁前一致。

---

## 单一真源回填 —— ⛔ 已中止（检测到真源与运行时存在额外分歧）

**结论：未做任何回填，未修改 `ComfyUI-upstream/main.py`。** 按 team-lead 指令 step 1，
两份文件除 4 行门禁补丁外**并不一致**，存在 8 处额外差异，属"其他本地编辑/不同 revision"，故 STOP 并上报。

**比对对象**
- 真源：`C:/Users/HE/WorkBuddy/2026-10-08-05-10-30/ComfyUI-upstream/main.py`（630 行，mtime 2026-10-08 05:12:38）
- 运行时预补丁备份：`E:\aiwork\...\ComfyUI\main.py.bak.20261009-010833`（620 行）

**`dynamic_vram_supported()` 门禁区域内两份文件完全一致**（都是未打补丁版本）；分歧全部在函数之外。

**额外分歧清单（`-` = 真源独有，`+` = 运行时独有）**

| # | 位置 | 差异 | 真源 | 运行时 |
|---|---|---|---|---|
| 1 | :19 | `from app import governance` | 有 | 无 |
| 2 | :131 附近 | `if args.enable_manager:` + `find_spec("comfyui_manager")` 块 | 无（该块在 :231 后） | 有（前置到 `apply_custom_paths()` 之前） |
| 3 | :190 | `governance.pack_module_spec(...)` vs `importlib.util.spec_from_file_location(...)` | governance 版 | importlib 版 |
| 4 | :213 | `refusal = governance.pack_refusal(module_path)` 拦截块 | 有 | 无 |
| 5 | :231 | `governance.initialize()` 调用 + enable_manager 块位置 | 有/在后 | 无/在前 |
| 6 | :357 | `server_instance.workflow_metadata = item[3].get("workflow_metadata", {})` | 有 | 无 |
| 7 | :381 | `server_instance.workflow_metadata = {}` | 有 | 无 |
| 8 | :548 | `governance.load_disabled_nodes(...)` / `governance.apply_disabled_nodes(...)` | 有 | 无 |

**特征判断**：真源含一整套 `app.governance` 集成（模块导入、pack/refusal、disabled_nodes 治理、workflow_metadata 透传），
运行时不具备；且 `enable_manager` 预检块在运行时被前移到 `apply_custom_paths()` 之前。二者疑似**不同 revision / 不同分支**，
并非"只差我的 4 行补丁"。

**风险**：若强行把运行时（或仅 4 行补丁）覆盖回真源，会**删除真源的 governance 治理能力**（相当于回退上游功能）；
若仅把 4 行补丁打到真源，两份文件仍会有上表 8 处差异，无法满足"byte-for-byte 一致"的要求。

**待 team-lead 决策**：请确认 (a) 哪一份是期望的 revision 基线；(b) 运行时的 governance 缺失与 enable_manager 前移是有意为之还是陈旧；
(c) 是否授权以真源为基线重跑 `sync_to_runtime.sh` 全量同步（会改变运行时行为，需实机复验）。
在收到决策前，保持真源与运行时**均不再改动**。

> **后续更正（team-lead 已核实）**：上述"分歧"属预期内的**不同 revision**，非事故。
> `ComfyUI-upstream/` 是 `https://github.com/Comfy-Org/ComfyUI.git` 的 checkout，位于 commit **`f856877`**（`main.py` 630 行，含 `app/governance.py`、`app/asset_export.py`）；
> 运行时 portable 构建位于 **`b0b7435`**（0.39.0，无 governance/asset_export）。
> `sync_to_runtime.sh` 的"单一真源"规则**仅适用于其列明的 3 个 guard 文件**，从不由 `main.py` 整体同步。
> 因此本门禁补丁**只打运行时、不回填真源**。镜像任务已由 team-lead 取消。

---

## 可重放补丁

**产物**：`C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/comfyui-intel-gate.patch`（480 字节，`main.py` → `main.py`）

**版本基线**

| 位置 | 仓库/revision | `main.py` | 备注 |
|---|---|---|---|
| `ComfyUI-upstream/`（真源 checkout） | Comfy-Org/ComfyUI @ **`f856877`** | 630 行 | 含 `app/governance.py`、`app/asset_export.py` |
| 运行时 `E:\aiwork\...\ComfyUI` | portable **`b0b7435`**（v0.39.0） | 624 行（补丁后） | **无** `app/governance.py` / `app/asset_export.py` |

**为什么补丁仅限运行时**：两者是**不同 revision**。`sync_to_runtime.sh` 的"单一真源 = ComfyUI-upstream"规则**只覆盖该脚本列明的 3 个 guard 文件**，从不整文件同步 `main.py`；跨 revision 同步 `main.py` 会回退运行时的行为（governance/asset_export、enable_manager 位置、workflow_metadata 等差异）。故本补丁**只作用于运行时**，`ComfyUI-upstream/main.py` 保持原样、不予修改。

**行尾（CRLF）说明**：运行时 `main.py` 为 CRLF。首次用编辑器插入的 4 行曾为 LF，导致文件出现**混合行尾**（`patch` 报 "different line endings"）。已从备份 `main.py.bak.20261009-010833` 重建，4 行插入内容统一为 CRLF；当前 `bare_LF=0`、`lines=624`、`py_compile` 通过。补丁内容语义不变（仅新增 4 行）。

**重放命令（ComfyUI 升级后使用）**

```bash
cd <ComfyUI 目录>
# 方式一：GNU patch（务必带 --binary，避免 patch 在文本模式归一化 CRLF 导致误判）
patch -p0 --binary < comfyui-intel-gate.patch
# 方式二：git（务必 autocrlf=false，按字节精确还原）
git -c core.autocrlf=false apply comfyui-intel-gate.patch
```

**重放验证结果（对预补丁备份，零 fuzz）**

| 命令 | dry-run/check | 实际应用后与运行时对比 |
|---|---|---|
| `patch -p0 --binary --dry-run` | exit 0，无 fuzz | `cmp` **byte-identical** |
| `git -c core.autocrlf=false apply --check` | exit 0 | `cmp` **byte-identical** |
| `patch -p0`（默认文本模式） | exit 1，报 `different line endings` | —— 故**不采用**，改用 `--binary` |

即：补丁可**零 fuzz 干净重放**，且重放产物与当前运行时 `main.py`（md5 `acd92cf2089c5ebf37414067e5ec4e3f`）**逐字节一致**。
