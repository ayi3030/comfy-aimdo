# EVIDENCE — `_xpu_opt_in()` 导入时序竞态（ship FAIL vs fixed PASS）

> ## ⚠️ 后续事实（2026-10-09 追加，task #21/#19 落地后；**不改动下方原文**）
> 本文**根因分析（导入时序竞态：`main.py:74` 调用点 vs `main.py:258` 导入点）仍然准确**，是 dev28 修复的依据。
> 但顶部「the fix is being re-landed as task #21」与结论行「出货版 dev26……是断的」为**当时状态**：
> - 修复**已落地部署**：非阻断 `_xpu_opt_in()` 随 **`dev28` / `c2cf98fb9`** 上线；当前出货 = dev28（**已修正**），原「出货版」指 dev26。
> - 下方**数值 / sha / 行号 / 日志一律保留**。归属：intel-xpu-adapter。

> **NOT APPLIED to any repo — but this is the CORRECT approach. The shipped dev26 gate (fork 728d6bb78) is the broken one; the fix is being re-landed as task #21.**
>
> ⚠️ **证据等级：模块级 / 影子包 A/B（module-level），非「部署产物验收」。** 下列运行用「影子包（整包复制安装版 `comfy_aimdo`，仅替换 `xpu.py`）+ 运行期 `sys.path` 注入」在真机 B580 上拉起的 ComfyUI 进程得出；**未改 site-packages、未 push**。因此证明的是**该函数逻辑**在真机 ComfyUI 进程里的行为，不构成本身对 dev26 产物的验收。

- 关联：task #17（我的实现）/ #19（落地 728d6bb）/ **#21（修复）**
- 真机：Windows 11 + Intel Arc B580；`python_embeded`（Python 3.13.14）；torch 2.14.0+xpu
- 结论：**出货版 dev26（fork `728d6bb78`）的免 flag 路径是断的；根因是 `_xpu_opt_in()` 条件 3 对 `comfy.model_management` 的导入时序依赖。**

---

## 1. 根因（调用时序，逐行）

免 flag 启动时，`_xpu_opt_in()` 在 **`comfy.model_management` 尚未 import** 的时刻被调用，导致条件 3 中的设备断言恒为 `None`：

| 步 | 位置 | 事实 |
|---|---|---|
| 1 | `ComfyUI\main.py:74` | `if enables_dynamic_vram():` 默认无 flag 时**必进** → `comfy_aimdo.control.init(...)` |
| 2 | `ComfyUI\main.py:258` | `import comfy.model_management` ← **比第 1 步晚 184 行** |
| 3 | `comfy_aimdo\control.py:341-351` | `init()` 末尾 `if implementation == "xpu": setup_backend(..., explicitly_requested=False)` |
| 4 | `comfy_aimdo\xpu.py:430` | `if not explicitly_requested and not _xpu_opt_in():` ← **全仓唯一调用点**，无第二次机会 |
| 5 | — | 故第 4 步执行时 `sys.modules.get("comfy.model_management")` **恒为 None** |

**两种实现的分野**（同一守卫，语义相反）：

| 实现 | 守卫在「mm 未加载」时 | 结果 |
|---|---|---|
| **出货版 `728d6bb`**（坏） | `is_intel = None` 参与 `and` → `True and None` → `bool(None)=False`（**硬条件**） | 条件 3 **恒 False** → 后端永不激活 |
| **本仓正确版**（我的方案） | 取不到 / 非 callable / 抛异常 → **不阻断**，继续 `return True`（**非阻断**） | 条件 3 **为 True** → 后端激活 ✅ |

真机 marker（出货版，`_e2e_optin_ship.log:10`）逐字：
```
[WARNING] SHADOW-SHIP-MARKER xpu.py=...\_e2e_pkg_ship\comfy_aimdo\xpu.py |
          model_management_loaded=False | is_intel_xpu=None |
          cli_args_loaded=True | enables_dynamic_vram_present=True
```
—— `model_management_loaded=False`、`is_intel_xpu=None` 直接坐实第 5 步；且 `enables_dynamic_vram_present=True`，说明**只有设备守卫这一环坏掉**。

---

## 2. 双日志对照（ship FAIL vs fixed PASS）

- ship（`aimdo-xpu/_e2e_optin_ship.log`，跑 fork `728d6bb` 的 xpu.py）
- fixed（`aimdo-xpu/_e2e_optin_fixed.log`，跑本仓正确版 xpu.py）
- 两者均为影子包 + 运行期 `sys.path` 注入，均**不带** `--enable-dynamic-vram`。

| token | ship（`_e2e_optin_ship.log`） | fixed（`_e2e_optin_fixed.log`） |
|---|---|---|
| `published 1 SYCL queue(s)` | **miss** | **HIT** (L10) |
| `XPU backend ready (mode=native_hook)` | **miss** | **HIT** (L12) |
| `DynamicVRAM support detected and enabled` | **miss** | **HIT** (L43) |
| `XPU backend not requested` | **HIT** (L11) | miss |
| `No working comfy-aimdo install detected` | **HIT** (L42) | miss |
| `To see the GUI go to`（服务器横幅） | HIT (L93) | HIT (L94) |

补充对照（本仓另一次 control，跑安装版**原样** xpu.py）：`_e2e_optin_control.log` 同为 FAIL 形态（L10 `not requested`、L41 `No working…`）——与 ship 一致，佐证「原样/出货版都断，只有正确版通」。

---

## 3. 等价证据源（修法的立足点）

`ComfyUI\comfy\model_management.py:128`：
```python
xpu_available = torch.xpu.is_available()
```
`is_intel_xpu()`（同文件 `:161-167`）仅读该全局量：
```python
def is_intel_xpu():
    if cpu_state == CPUState.GPU:
        if xpu_available:
            return True
    return False
```
⇒ **`torch.xpu.is_available()` 与 `is_intel_xpu()` 是同一份证据**，只是前者**比后者早 184 行就可获得**（无需 `import comfy.model_management`）。这正是修法立足点：若要在 `_xpu_opt_in()` 里做设备断言，可用 `torch.xpu.is_available()`（或干脆去掉断言），而**不能**依赖尚未加载的 `comfy.model_management`。

---

## 4. `AIMDO_XPU_ENABLED=1` 会**短路**并**掩盖**缺陷

`_xpu_opt_in()` 条件 1（`os.environ.get("AIMDO_XPU_ENABLED") == "1"`）**最先返回 True**，根本不进入条件 3：

- 因此 `run_intel_gpu.bat` 若设了 `AIMDO_XPU_ENABLED=1`，免 flag 也能激活——**但这是被短路出来的假绿**，不代表门禁修好。
- **验收要求**：必须在**不带 `AIMDO_XPU_ENABLED`**（且不带 `--enable-dynamic-vram`）的条件下运行，才真正检验条件 3；否则会把 dev26 的缺陷掩盖过去。

---

## 5. 触发面 / 边界

- 仅影响 **Intel XPU 免 flag 自动启用**（条件 3）。带 `--enable-dynamic-vram`（条件 2 显式 flag）或 `AIMDO_XPU_ENABLED=1`（条件 1）时不会触发该缺陷。
- 与 CUDA/ROCm 无关（该函数仅在 XPU `setup_backend()` 内调用）。
- 与 task #14 的 `nodes.py` 启动崩溃无关（两个独立阻塞项）。

---

## 6. 证据等级声明（重要）

- 本文件证据为 **模块级 / 影子包 A/B**，在真机 B580 的 ComfyUI 进程内采集，但**非部署产物验收**。
- 涉及文件仅**读取**：`_e2e_optin_ship.log`、`_e2e_optin_fixed.log`、`_e2e_optin_control.log`（及团队产出的 ship/conv 影子脚本与影子包，未改动）。
- **未 push、未改任何 clone 工作树、未动 site-packages、未运行 ComfyUI**（本次文档整理阶段）。
