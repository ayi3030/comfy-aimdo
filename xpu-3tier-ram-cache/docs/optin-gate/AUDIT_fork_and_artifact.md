# AUDIT — ayi3030/comfy-aimdo fork 与 XPU CI 产物

> ## ⚠️ 后续事实（2026-10-09 追加；**不改动下方原文**）
> §1 / §2 为**审计时快照**（当时 main HEAD `9b1efd905`、最新成功运行 #23）。据此判断「当前 fork / CI 状态」会与事实相反：
> - fork `main` 已推进：`728d6bb`（dev26）→ **`c2cf98fb9`（dev28，当前部署）**；CI 已新增对应运行。
> - **当前部署状态见文末 §D / §E / §F（滚动追加，最新段 §F = dev28）。**
> - 下方**数值 / sha / 行号 / 日志一律保留**。归属：intel-xpu-adapter。

**审计人**: intel-xpu-adapter-2
**日期**: 2026-10-09
**性质**: 只读审计。未推送、未修改 fork、未触碰任何 ComfyUI 安装。
**审计目标**: 判定最新一轮 XPU CI 产物是否可安全部署到真机 B580。

---

## 1. Fork 分支 HEAD（已核实）

| 分支 | 完整 SHA | 来源 |
|---|---|---|
| `main` | `9b1efd905bf68086a8195026c28fda1602b80f37` | `git ls-remote` |
| `master` | `bebca5f33916b3417582177597fc734641fcfb29` | `git ls-remote` |

命令：`git ls-remote https://github.com/ayi3030/comfy-aimdo refs/heads/main refs/heads/master`

> `main` 领先 `master`（master HEAD = run #20 的 `bebca5f33`），最新工作都在 `main` 上。

---

## 2. XPU workflow 最近 12 次运行（已核实）

Workflow: `.github/workflows/build-xpu-windows.yml`（"Build AIMDO XPU native (Windows, oneAPI/DPC++)"）

| # | run_id | created_at (UTC) | status | conclusion | head_sha | branch |
|---|---|---|---|---|---|---|
| 23 | 37773546816 | 2026-10-08T11:57:47Z | completed | **success** | `9b1efd905` | main |
| 22 | 37772643945 | 2026-10-08T11:49:43Z | completed | success | `6b140206f` | main |
| 21 | 37771189129 | 2026-10-08T11:36:38Z | completed | success | `46ef29be1` | main |
| 20 | 37763242745 | 2026-10-08T10:24:19Z | completed | success | `bebca5f33` | master |
| 19 | 37762672869 | 2026-10-08T10:19:07Z | completed | **failure** | `0472cc2e6` | master |
| 18 | 37761734619 | 2026-10-08T10:10:35Z | completed | success | `3f0497359` | master |
| 17 | 37759346306 | 2026-10-08T09:49:46Z | completed | success | `06341ac86` | master |
| 16 | 37747885855 | 2026-10-08T08:08:36Z | completed | success | `ce5821b15` | master |
| 15 | 37745681536 | 2026-10-08T07:48:02Z | completed | success | `b19fefed6` | master |
| 14 | 37745503023 | 2026-10-08T07:46:17Z | completed | success | `7637d3e1c` | master |
| 13 | 37745273628 | 2026-10-08T07:44:09Z | completed | success | `385d7ddcc` | master |
| 12 | 37744951391 | 2026-10-08T07:41:03Z | completed | success | `7db416d98` | master |

- **最新成功运行 = #23 / run_id `37773546816`**，sha `9b1efd905`，branch `main`，created_at `2026-10-08T11:57:47Z`。与 team-lead 提供的信息一致。
- 最新一次失败为 #19（`0472cc2e6`，master），其后 #20 已恢复成功。

---

## 3. 下载的产物（已核实）

来源：run #23（`37773546816`）的两个 artifact，均未过期（expired=false）。
下载目录：`C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/ci_artifacts/`

| artifact 名 | artifact zip 路径 | zip 大小 | 解出文件（绝对路径） | 文件大小 | SHA256 |
|---|---|---|---|---|---|
| `comfy-aimdo-xpu-wheel` | `.../ci_artifacts/comfy-aimdo-xpu-wheel.zip` | 154272 B | `.../ci_artifacts/comfy-aimdo-xpu-wheel/comfy_aimdo-0.5.6.dev25-cp39-abi3-win_amd64.whl` | 155135 B | `f8e2617a953c23a65fb5748a34e45b14e2030ca50d342842854223f8b8876f43` |
| `aimdo_xpu.dll` | `.../ci_artifacts/aimdo_xpu.dll.zip` | 103358 B | `.../ci_artifacts/aimdo_xpu.dll/aimdo_xpu.dll` | **238592 B** | `b4241291e962450ed71cd0da9a2a00ff5b1de4e26d86f6b434bc52af5011ce2c` |

绝对路径（完整）：
- DLL: `C:\Users\HE\WorkBuddy\2026-10-08-23-50-54\aimdo-xpu\ci_artifacts\aimdo_xpu.dll\aimdo_xpu.dll`
- WHL: `C:\Users\HE\WorkBuddy\2026-10-08-23-50-54\aimdo-xpu\ci_artifacts\comfy-aimdo-xpu-wheel\comfy_aimdo-0.5.6.dev25-cp39-abi3-win_amd64.whl`

> **一致性交叉校验（已核实）**：wheel 内 `comfy_aimdo/aimdo_xpu.dll`（238592 B）与独立 artifact `aimdo_xpu.dll` **逐字节相同**（SHA256 均为 `b4241291...c1ce2c`）。两个 artifact 来自同一次构建，内容自洽。

> 302→S3 下载说明：按 team-lead 提示，用 NoRedirect 拦截 302 后以**不带 Authorization** 的裸请求取 S3 对象，两个 artifact 均下载成功。

---

## 4. PE 导出表断言（已核实，PASS）

解析对象：`ci_artifacts/aimdo_xpu.dll/aimdo_xpu.dll`
- 文件大小：**238592 字节**
- Machine: `x64`（0x8664），PE32+（magic 0x20b），SubsystemVersion 6.0，12 个 section
- 导出符号总数：**87**

| 符号 | 结果 |
|---|---|
| `xpu_get_total_vram_usage` | **PASS** |
| `xpu_get_peak_total_vram_usage` | **PASS** |
| `xpu_allocator_get_memory_stats` | **PASS** |

**导入的 syclN.dll（已核实）**：`sycl9.dll` ✅（符合预期）。完整导入表（16 个）：

```
ze_loader.dll, CFGMGR32.dll, dxgi.dll, SETUPAPI.dll, KERNEL32.dll,
api-ms-win-core-memory-l1-1-5.dll, api-ms-win-core-memory-l1-1-6.dll,
MSVCP140.dll, libmmd.dll, sycl9.dll, VCRUNTIME140.dll,
api-ms-win-crt-stdio-l1-1-0.dll, api-ms-win-crt-heap-l1-1-0.dll,
api-ms-win-crt-runtime-l1-1-0.dll, api-ms-win-crt-string-l1-1-0.dll,
api-ms-win-crt-environment-l1-1-0.dll
```

其他关键导出（节选，与本次修复相关的口径函数）：`xpu_get_vram_capacity`、`xpu_get_vmm_stats`、`xpu_allocator_empty_cache`、`xpu_allocator_reset_peak_stats`、`xpu_set_queues`、`xpu_ur_hook_*`（一整套 hook 统计接口）均存在。

> **真机运行时依赖（已核实，非阻塞）**：DLL 导入 `sycl9.dll` / `ze_loader.dll`。`System32` 中确实没有 `sycl9.dll`（见 `_probe_env.txt`），但这**不构成阻塞**——运行时来自 pip 安装的 oneAPI 运行时栈（`intel-sycl-rt 2026.1.0`、`intel-cmplr-lib-rt/ur`、`intel-opencl-rt` 等），且 `import comfy_aimdo` 会连带 `import torch`（torch 2.14.0+xpu），torch-xpu 在导入时注册了 DLL 搜索目录，故 `sycl9.dll`（位于 `python_embeded/Library/bin/`）可被解析。实测该 DLL 在 `import torch` 后可被 `ctypes.CDLL` 正常加载并导出 `alloc_fn`/`xpu_get_total_vram_usage`/`xpu_allocator_get_memory_stats`（由 intel-xpu-adapter 于真机 B580 验证，见 `VERIFY_real_b580.md`）。**无需手动补装 oneAPI。**
>
> （勘误：本节初版曾据此判定「必须由部署方补齐 oneAPI runtime」，属过度保守，现更正。）

---

## 5. 提交差异 `6b140206f...9b1efd905`（已核实）

GitHub compare API：status = `ahead`，ahead_by = **2**，behind_by = 0，total_commits = 2。
merge_base / base_commit = `6b140206f`（= 真机已部署版本）。

**新增 2 个提交：**

1. **`630f5ca5c`** — `fix(xpu): 让L3/L2 读 torch 原生口径，并给静默降级加告警`
   - 背景：M2 的 `aimdo_xpu_memory_stats` 把 reserved 与 allocated 双双映射成 Book A 当前值以消除双记账，副作用是任何经 `torch.xpu.memory_stats()` 计算的 cached 量恒为 0 → L3 trim 与 `anticipated_growth` 失效，且**全程无报错（静默降级）**。
   - 改动：`control.get_xpu_torch_reserved_growth` / `model_vbar._release_native_cache` 改为读 torch 原生口径，并对静默降级加告警。

2. **`9b1efd905`** — `fix(xpu): 按失败原因区分 torch 原生口径的降级告警`（HEAD）
   - `xpu.torch_reserved_stats` 有三个互斥的 None 原因：`not_installed`（M2 包装器未安装）/ `call_failed`（torch 侧 `memory_stats` 抛异常，驱动/XPU 上下文问题）/ `empty_stats`（返回空读数）。原先统一返回 None，告警只有一句「返回 None」，会误导排查方向。改动按原因区分告警文案。

**文件变动（3 个文件，全部为修改）：**

| 文件 | 状态 | +新增 | -删除 |
|---|---|---|---|
| `comfy_aimdo/control.py` | modified | +46 | -2 |
| `comfy_aimdo/model_vbar.py` | modified | +34 | -8 |
| `comfy_aimdo/xpu.py` | modified | +33 | -6 |

**小结**：本轮 delta 是**纯 Python 侧的记账/口径修复 + 告警可观测性改进**，共 3 个文件、约 +113/-16 行。涉及 L3/L2 卸载与 torch 原生 reserved/cached 口径，**属于行为修正类改动，风险点集中在「改口径后 L3 trim 是否按预期触发」**。C++/XPU DLL 侧无源码级变更迹象（compare 未列出任何 native/CMake 文件），但注意：**每次 push 都会重编 DLL，故本次 DLL 与上一版 DLL 在二进制层面仍可能有差异（构建时间戳 / 同源码重编）**。

---

## 6. Wheel 内容与版本（已核实）

- 文件名：`comfy_aimdo-0.5.6.dev25-cp39-abi3-win_amd64.whl`（155135 B）
- **dist-info 目录名**：`comfy_aimdo-0.5.6.dev25.dist-info`
- `comfy_aimdo/_version.py`：
  - `__version__ = version = '0.5.6.dev25'`
  - `__version_tuple__ = (0, 5, 6, 'dev25')`
  - `__commit_id__ = commit_id = 'g9b1efd905'` ✅ 与 run #23 的 head_sha 一致
- wheel 内含模块：`_version.py`、`aimdo_xpu.dll`(238592B)、`control.py`、`host_buffer.py`、`malloc_graph.py`、`model_mmap.py`、`model_vbar.py`、`storage.py`、`torch.py`、`vram_buffer.py`、`xpu.py`

> 版本推进：真机当前 `0.5.6.dev23` / `g6b140206f` → 本产物 `0.5.6.dev25` / `g9b1efd905`，**恰好跨越 dev23→dev25 两个 commit（dev24=630f5ca5c，dev25=9b1efd905）**，与 compare 的 2 个提交吻合。

---

## 7. 结论与部署判定

**导出符号、DLL 大小、sycl 依赖、wheel 版本/commit 四项断言全部通过，产物内部自洽。** 版本号 `dev25` / commit `g9b1efd905` 与目标 run #23 完全对应，不存在「artifact 与提交错配」风险。

**可部署的前提条件（需部署方确认）：**
1. **oneAPI runtime — 已满足（非阻塞）**：DLL 依赖 `sycl9.dll` + `ze_loader.dll`。真机经 pip 安装了完整 oneAPI 运行时（`intel-sycl-rt 2026.1.0` 等），且 `comfy_aimdo` 导入链会 `import torch`（2.14.0+xpu），torch-xpu 注册的 DLL 目录使 `sycl9.dll` 可解析。**无需额外补装。**（初版曾标为「必须由部署方补齐」，现更正为已满足。）
2. **本次为行为修正（L3/L2 口径）**：部署后需重点复验「L3 trim 是否按预期触发、`torch.xpu.memory_stats()` cached 量是否非 0、降级告警文案是否符合新口径」。建议保留回退（真机现有 dev23 备份）。
3. 未做任何功能/性能实测——**性能影响未知，不背书**。

**审计判定**：产物本身**干净、自洽、可安全用于部署验证**；是否 deploy 取决于真机 `sycl9.dll` 运行时可用性（条件 1）。

---

## 附：置信度标注

| 结论 | 置信度 |
|---|---|
| fork main/master SHA | 已核实（git ls-remote） |
| run #23 为最新成功、sha=9b1efd905 | 已核实（REST API） |
| artifact 下载与大小/SHA256 | 已核实（本地计算） |
| 3 个导出符号存在、导入 sycl9.dll、DLL=238592B | 已核实（PE 解析） |
| 6b14020→9b1efd9 两个提交与文件 delta | 已核实（compare API） |
| wheel 版本/commit/dist-info | 已核实（zip 读取） |
| 真机 sycl9.dll 缺失 | 已核实（本机 `_probe_env.txt`） |
| 运行时是否可用 | **已核实**（intel-sycl-rt + torch-xpu 注册 DLL 目录；真机 CDLL 加载成功） |
| L3 trim 修复的实际行为 | **待实机验证** |

---

# Deployment log — 部署 dev25 (g9b1efd905) 到真机 B580

**执行人**: intel-xpu-adapter-2
**时间**: 2026-10-09
**目标**: `E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\python_embeded\Lib\site-packages`

## D.0 预检（PASS）
- **无锁定进程**：`tasklist` 查询 `python.exe` / `pythonw.exe` 均返回「没有运行的任务匹配」（无 python 进程）。PASS。
- **运行时前置**：`python_embeded\Library\bin\sycl9.dll` 存在（3819648 B）。PASS。
- 目标 `python_embeded\python.exe` 存在（105696 B）。

## D.0b 部署前状态核查（重要发现：预存不一致）
对部署前 site-packages 的 `comfy_aimdo/` 做逐字节比对（对齐 git blob，忽略 CRLF）：

| 文件 | 已安装 == 6b140206f? | 已安装 == 9b1efd905? |
|---|---|---|
| control.py | False | **True** |
| model_vbar.py | False | **True** |
| xpu.py | False | **True** |

- 部署前 `_version.py` 声称 `0.5.6.dev23 / g6b140206f`，**但其 Python 源码已与 dev25(g9b1efd905) 逐字节相同**；`aimdo_xpu.dll` 的 sha256 = `561dc44618c95da07b9ef2d23fdbb146d10bd18cb4c8a81561307fa8cb64949e`（与 dev25 wheel 内 DLL `b4241291...` **不同**）。
- 即：**部署前该安装是混合态**（dev25 源码 + dev23 的 `_version.py` + 另一个构建的 DLL）。这正是 `ci_watch.py` 注释里警示的「极具迷惑性」的不一致。
- 附带核实：dev25 wheel 的 `control.py` 与 git `9b1efd905` 的 blob 规范化后**一致**（差异仅为 CRLF/LF），确认 wheel 确由 `9b1efd905` 构建。

## D.1 备份（PASS，写入前完成）
备份目录：`C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/rollback/dev23_g6b140206f/`

| 备份项 | 绝对路径 | 文件数 |
|---|---|---|
| 整个包目录 | `.../rollback/dev23_g6b140206f/comfy_aimdo/` | 11（=源 11） |
| 元数据 | `.../rollback/dev23_g6b140206f/comfy_aimdo-0.5.6.dev23.dist-info/` | 5（=源 5） |

- 校验：`_version.py` 内容 = `0.5.6.dev23` / `g6b140206f`，与源一致。文件计数一致。PASS。
- 说明：备份为**部署前实际状态的逐字节副本**（即上面 D.0b 描述的混合态）。回滚会精确还原到该状态。

## D.2 安装（PASS）
**方法**：`python_embeded\python.exe -m pip install --no-deps --no-index --force-reinstall <whl>`（pip 26.2.1 可用，未走 zip 兜底）。
- pip 记录：先卸载了被识别到的旧 `comfy-aimdo 0.5.6.dev16`，再安装本 wheel，退出码 0。
- **注**：pip 末尾打印「Successfully installed comfy-aimdo-0.5.6.dev23」为**误导性文案**——因部署前同时存在 dev23 与 dev16 两个 dist-info，`importlib.metadata` 按字母序读到 dev23。以磁盘实际状态为准（见 D.4），安装内容确为 dev25。

## D.3 清理陈旧元数据（PASS）
- 删除 `comfy_aimdo-0.5.6.dev23.dist-info/`（本次手动删除）。
- `comfy_aimdo-0.5.6.dev16.dist-info/`：pip 安装阶段已自动卸载移除。
- 删除 `comfy_aimdo/__pycache__/`。
- 现存 comfy_aimdo* 项：`comfy_aimdo/`、`comfy_aimdo-0.5.6.dev25.dist-info/`（另有 3 个**部署前既有**的 `.bak` 目录：`comfy_aimdo-0.5.5.dist-info.bak`、`comfy_aimdo.bak.0.5.5`、`comfy_aimdo.bak.pre_oomfix`——非本次范围，未触碰）。

## D.4 验证结果（全部 PASS）

| # | 验证项 | 期望 | 实测 | 结果 |
|---|---|---|---|---|
| 1 | `_version.py` `__version__` | `0.5.6.dev25` | `0.5.6.dev25` | **PASS** |
| 1 | `_version.py` `__commit_id__` | `g9b1efd905` | `g9b1efd905` | **PASS** |
| 2 | `comfy_aimdo/aimdo_xpu.dll` 大小 | 238592 B | 238592 B | **PASS** |
| 2 | `aimdo_xpu.dll` sha256 | `b4241291...1ce2c` | `b4241291e962450ed71cd0da9a2a00ff5b1de4e26d86f6b434bc52af5011ce2c` | **PASS** |
| 3 | DLL 导入表 sycl | `sycl9.dll` | `['sycl9.dll']`（87 导出） | **PASS** |
| 4 | import 冒烟测试（plain） | 打印 dev25/g9b1efd905 | stdout=`0.5.6.dev25 g9b1efd905`，exit=0，stderr 空 | **PASS** |
| 4 | import 冒烟测试（PATH 加 `Library/bin`） | 同上 | stdout=`0.5.6.dev25 g9b1efd905`，exit=0，stderr 空 | **PASS** |

冒烟测试命令：
```
python.exe -c "import comfy_aimdo, comfy_aimdo.control, comfy_aimdo.xpu; import comfy_aimdo._version as v; print(v.__version__, v.__commit_id__)"
```
`comfy_aimdo` / `.control` / `.xpu` 三个模块均导入成功（即 native DLL 经 sycl9.dll 解析成功），无 stdout/stderr 异常。**未启动 ComfyUI**，未触碰 `ComfyUI/main.py`。

## D.5 部署后状态
- 安装一致性：`comfy_aimdo/`、`_version.py`、`aimdo_xpu.dll` **三者现在全部为 dev25 / g9b1efd905，内部自洽**（消除了 D.0b 的混合态）。

## D.6 回滚命令（如需恢复到部署前状态）
```bash
SP="E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/python_embeded/Lib/site-packages"
RB="C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/rollback/dev23_g6b140206f"
rm -rf "$SP/comfy_aimdo" "$SP/comfy_aimdo-0.5.6.dev25.dist-info"
cp -r "$RB/comfy_aimdo" "$SP/comfy_aimdo"
cp -r "$RB/comfy_aimdo-0.5.6.dev23.dist-info" "$SP/comfy_aimdo-0.5.6.dev23.dist-info"
```
> 回滚后 `_version.py` 会显示 `0.5.6.dev23 / g6b140206f`，但注意其源码实为 dev25、DLL 为 `561dc4...` 变体（即精确还原部署前的混合态）。

## D.7 sycl9.dll 前提核实（勘误，非阻塞）
部署后经核实：`python_embeded` 内已 pip 安装完整 oneAPI 运行时栈（`intel-sycl-rt 2026.1.0`、`intel-cmplr-lib-rt/ur 2026.1.0`、`intel-opencl-rt`、`intel-pti` 等），且 `import comfy_aimdo` → `import torch`（torch 2.14.0+xpu，`torch.xpu.is_available()==True`），torch-xpu 注册的 DLL 目录使 `Library/bin/sycl9.dll` 可被解析。因此本部署的「sycl9 运行时」前提**已满足**，无需手动补 oneAPI。intel-xpu-adapter 已在真机 B580 上以 `ctypes.CDLL` 成功加载 `aimdo_xpu.dll` 并枚出 `alloc_fn`/`xpu_get_total_vram_usage`/`xpu_allocator_get_memory_stats`（见 `VERIFY_real_b580.md`），与本部署日志结论一致。

---

# Deployment log #2 — 部署 dev26 (g728d6bb) 自动启用补丁到真机 B580

**执行人**: intel-xpu-adapter-2
**时间**: 2026-10-09
**目标**: 让 Intel XPU DynamicVRAM 无需 `--enable-dynamic-vram` 自动启用（fork CI 重编 + launcher 双保险）。

## E.1 Fork 改动（已推送）
- 仓库：`ayi3030/comfy-aimdo`，分支 `main`（fast-forward，未 force-push）。
- 提交：`728d6bb78e8241211f914387453ebf9912f4b0f9`（`9b1efd9..728d6bb  HEAD -> main`）。
- 文件：`comfy_aimdo/xpu.py`（+36/-2），仅改 `_xpu_opt_in()`：新增**条件 3** —— `comfy.cli_args.enables_dynamic_vram()` 为真（缺该 helper 时回落 `args.enable_dynamic_vram`）**且** `comfy.model_management.is_intel_xpu()` 为真 → 返回 True。保留原 env(`AIMDO_XPU_ENABLED=1`) 与 `--enable-dynamic-vram` 两条路径；CUDA/ROCm 不受影响（条件 3 仅对 Intel XPU 生效）；`comfy.*` 缺失时返回 False 且不抛异常；docstring 已更新（不再写「Mirrors the community fork's opt-in」）。
- API 核实（本地实机源码）：`enables_dynamic_vram()` @ `comfy/cli_args.py:326`；`is_intel_xpu()` @ `comfy/model_management.py:161`。
- 本地单测：8/8 逻辑用例 PASS（无 comfy/env、env=1、flag、条件3 真/假、helper 抛异常、缺 model_management 等），`py_compile` OK。

## E.2 CI 构建（PASS）
- run id：`37817467297`（event=push，branch=main，sha=`728d6bb78`）。
- 结论：**success**（created 2026-10-08T17:33:19Z，完成约 17:39Z）。
- 产物：
  - `comfy_aimdo-0.5.6.dev26-cp39-abi3-win_amd64.whl`（155720 B），artifact zip 154860 B → `ci_artifacts_728d6bb/comfy-aimdo-xpu-wheel/`
  - `aimdo_xpu.dll`（238592 B），artifact zip 103358 B → `ci_artifacts_728d6bb/aimdo_xpu.dll/`

## E.3 产物验证（全部 PASS）
| 项 | 实测 | 结果 |
|---|---|---|
| `_version.py` `__version__` | `0.5.6.dev26` | PASS |
| `_version.py` `__commit_id__` | `g728d6bb78`（== 推送 sha 728d6bb） | PASS |
| dist-info | `comfy_aimdo-0.5.6.dev26.dist-info` | PASS |
| wheel 内 xpu.py 含新门槛 | `enables_dynamic_vram`=True, `is_intel_xpu`=True | PASS |
| PE 导出 `xpu_get_total_vram_usage` | 存在 | PASS |
| PE 导出 `xpu_get_peak_total_vram_usage` | 存在 | PASS |
| PE 导出 `xpu_allocator_get_memory_stats` | 存在 | PASS |
| DLL 导入 sycl | `['sycl9.dll']` | PASS |
| DLL 大小 / sha256 | 238592 B / `b1fa82fb2035397daacc65360ed2c7854c6de8a137de485fd0602bfce507a18c` | PASS |
| wheel DLL == standalone DLL | 逐字节相同 | PASS |

## E.4 备份（PASS，写入前完成）
- dev25 回滚点：`rollback/dev25_g9b1efd905/`（`comfy_aimdo/` 11 文件 + `comfy_aimdo-0.5.6.dev25.dist-info/` 8 文件；`_version.py`=dev25/g9b1efd905；DLL sha `b4241291...`）。
- launcher 备份：`rollback/launcher/run_intel_gpu.bat.orig`（104 B，sha256 `e2cc72b85bc517cc42366382df9c31c4395e554d1c5819ff0a518884ea4b7629`）。

## E.5 部署（PASS）
- **方法**：`python_embeded\python.exe -m pip install --no-deps --no-index --force-reinstall <dev26 whl>`，exit 0。
- 清理：仅剩 `comfy_aimdo-0.5.6.dev26.dist-info`（旧 dev25 dist-info 已被 pip 卸载移除）；删除 `__pycache__`。
- 安装后逐字节校验：installed `xpu.py` == wheel `xpu.py`（sha `cd539fca...b42c`）；installed `aimdo_xpu.dll` == wheel DLL（sha `b1fa82fb...`）。均 **PASS**。
- import 冒烟测试：stdout=`0.5.6.dev26 g728d6bb78`，exit=0，stderr 空。**PASS**。未启动 ComfyUI。

## E.6 launcher 改动（PASS）
`run_intel_gpu.bat`（CRLF 保留）现为：
```
set AIMDO_XPU_ENABLED=1
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --enable-dynamic-vram
pause
```
保留原 `--enable-dynamic-vram`，仅在其上方新增 `set AIMDO_XPU_ENABLED=1`（双保险）。

## E.7 协调观察（需注意）
- 执行 pip 安装时 `tasklist` 短暂出现 `python.exe` 进程（PID 1580，后变为其它 PID 后消失）——期间进程列表显示持续有短生命周期 python 进程产生/退出，疑似有其它 worker/verifier 在同一 `python_embeded` 上活动。安装本身 exit 0、文件校验正确；但**若有并发进程正在运行该环境，其内存中的代码可能是替换前的**，建议 verifier 做一次冷启动复验。
- pip 安装前报「Found existing installation: comfy-aimdo 0.5.6.dev26」，暗示在我这轮 pip 之前该环境已被装过 dev26（可能另一 worker 亦在部署同一产物）。最终状态与 wheel 逐字节一致，无影响，但属需知晓的并发迹象。

## E.8 回滚命令（恢复 dev25）
```bash
SP="E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/python_embeded/Lib/site-packages"
RB="C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/rollback/dev25_g9b1efd905"
rm -rf "$SP/comfy_aimdo" "$SP/comfy_aimdo-0.5.6.dev26.dist-info"
cp -r "$RB/comfy_aimdo" "$SP/comfy_aimdo"
cp -r "$RB/comfy_aimdo-0.5.6.dev25.dist-info" "$SP/comfy_aimdo-0.5.6.dev25.dist-info"
# launcher 回滚：
cp "C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/rollback/launcher/run_intel_gpu.bat.orig" "E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/run_intel_gpu.bat"
```

---

# Deployment log — dev26 (g728d6bb78) 自动启用 XPU DynamicVRAM

**执行人**: intel-xpu-adapter-2 ｜ **时间**: 2026-10-09 ｜ **任务**: #17/#19

## E.1 fork 改动（已推送）
- fork `ayi3030/comfy-aimdo`，branch `main`：`9b1efd905` → **`728d6bb78e8241211f914387453ebf9912f4b0f9`**（fast-forward，未 force-push）。
- 唯一改动文件：`comfy_aimdo/xpu.py`，**+36/−2**。`_xpu_opt_in()` 新增**第三条件**：`comfy.cli_args.enables_dynamic_vram()`（缺失时回退 `args.enable_dynamic_vram`）**且** `sys.modules["comfy.model_management"].is_intel_xpu()` 为真 → True。
- 保留原有两条（`AIMDO_XPU_ENABLED=1`、`--enable-dynamic-vram`）；`try/except` 包裹，`comfy.*` 缺失时返回 False 不抛异常；CUDA/ROCm 不受影响（条件 3 仅对 Intel 生效）。
- API 已核实存在：`cli_args.py:326 enables_dynamic_vram()`、`model_management.py:161 is_intel_xpu()`。
- 离线单测 8/8 PASS（含 env/flag/第三条件/异常/无 comfy 分支），`py_compile` OK。

## E.2 CI
- run **37817467297**（build-xpu-windows.yml，sha `728d6bb78`，event push）→ 结论 **success**（约 5.5 min）。

## E.3 产物（已核实）
下载目录 `aimdo-xpu/ci_artifacts/run_37817467297/`

| 产物 | 路径 | 大小 | sha256 |
|---|---|---|---|
| wheel | `.../comfy-aimdo-xpu-wheel/comfy_aimdo-0.5.6.dev26-cp39-abi3-win_amd64.whl` | 155720 B | `5156e09b1b3eb2b0a891ee19b33be7b111d216994ae1aa38772436e55fadf261` |
| DLL | `.../aimdo_xpu.dll/aimdo_xpu.dll` | 238592 B | `b1fa82fb2035397daacc65360ed2c7854c6de8a137de485fd0602bfce507a18c` |

- wheel `_version.py`：`__version__='0.5.6.dev26'`，`__commit_id__='g728d6bb78'` ✅（与 run sha 一致）；dist-info = `comfy_aimdo-0.5.6.dev26.dist-info`。
- wheel 内 `xpu.py` 含 `enables_dynamic_vram` / `is_intel_xpu` / `AIMDO_XPU_ENABLED` ✅。
- PE 导出：`xpu_get_total_vram_usage` / `xpu_get_peak_total_vram_usage` / `xpu_allocator_get_memory_stats` 全 **PASS**；导入 `sycl9.dll` **PASS**。
- wheel 内 DLL 与独立 artifact DLL 同 sha256（`b1fa82fb...`，逐字节相同）。

## E.4 备份（写入前完成）
- dev25 回滚点：`aimdo-xpu/rollback/dev25_g9b1efd905/`（`comfy_aimdo/` 11 文件 + `comfy_aimdo-0.5.6.dev25.dist-info/` 8 文件；`_version.py`=dev25/g9b1efd905，DLL sha=`b4241291...`）。
- launcher：`aimdo-xpu/rollback/launcher/run_intel_gpu.bat.orig`（原文件 sha256 `e2cc72b85bc517cc42366382df9c31c4395e554d1c5819ff0a518884ea4b7629`）。

## E.5 安装
`python_embeded\python.exe -m pip install --no-deps --no-index --force-reinstall <dev26 whl>` → pip 卸载 dev25、安装 dev26，退出码 0。清理：移除 `comfy_aimdo-0.5.6.dev25.dist-info/` 与 `comfy_aimdo/__pycache__/`。现存：`comfy_aimdo/`、`comfy_aimdo-0.5.6.dev26.dist-info/`（另有 3 个既有 `.bak` 目录未动）。

## E.6 部署后验证（全部 PASS）
| 项 | 期望 | 实测 | 结果 |
|---|---|---|---|
| `_version.py` version | 0.5.6.dev26 | `0.5.6.dev26` | **PASS** |
| `_version.py` commit | g728d6bb78 | `g728d6bb78` | **PASS** |
| **部署文件**含条件 3（grep）| 有 | `xpu.py` 第 487/490/492/498/510/521 行含 `AIMDO_XPU_ENABLED`/`enables_dynamic_vram`/`is_intel_xpu`；函数体完整含第三条件 | **PASS** |
| 部署文件 vs wheel 逐字节 | 相同 | `xpu.py`/`control.py`/`_version.py` sha256 全相同 | **PASS** |
| DLL 大小 | 238592 B | 238592 B | **PASS** |
| DLL sha256 | b1fa82fb... | `b1fa82fb2035397daacc65360ed2c7854c6de8a137de485fd0602bfce507a18c` | **PASS** |
| import 冒烟 | dev26/g728d6bb78 | stdout=`0.5.6.dev26 g728d6bb78`，exit=0，stderr 空 | **PASS** |

未启动 ComfyUI（交由 runtime-verifier）。未触碰 ComfyUI 源码。

## E.7 launcher
`run_intel_gpu.bat` 现为（CRLF 保持）：
```
set AIMDO_XPU_ENABLED=1
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --enable-dynamic-vram
pause
```
双重保险：即使 fork 的第三条件在某路径未命中，`AIMDO_XPU_ENABLED=1` 亦会强制启用。

## E.8 回滚（如需）
```bash
SP="E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/python_embeded/Lib/site-packages"
RB="C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/rollback/dev25_g9b1efd905"
rm -rf "$SP/comfy_aimdo" "$SP/comfy_aimdo-0.5.6.dev26.dist-info"
cp -r "$RB/comfy_aimdo" "$SP/comfy_aimdo"
cp -r "$RB/comfy_aimdo-0.5.6.dev25.dist-info" "$SP/comfy_aimdo-0.5.6.dev25.dist-info"
# launcher: cp "C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/rollback/launcher/run_intel_gpu.bat.orig" "<...>/run_intel_gpu.bat"
```

---

# Deployment log — dev28 (gc2cf98fb9) 修复 _xpu_opt_in 时序竞态（task #21）

**执行人**: intel-xpu-adapter-2 ｜ **时间**: 2026-10-09

## F.1 缺陷（`728d6bb` / dev26 出货版，已由 intel-xpu-adapter 真机证伪）
`_xpu_opt_in()` 条件 3 依赖 `comfy.model_management.is_intel_xpu()`，而 `ComfyUI/main.py` 在 **~line 77** 就调用 `comfy_aimdo.control.init()`（→ 进入本函数），`import comfy.model_management` 要到 **~line 258**。故真实启动调用点上 `sys.modules.get("comfy.model_management")` 恒为 None → 条件 3 恒 False → 免 flag 自动启用失效。
真机证据：`FINDING_shipping_optin_broken.md`（shadow 标记 `model_management_loaded=False`；三个正标记全 miss）。

## F.2 修复
让条件 3 **不阻断**：`enables_dynamic_vram()` 为真后，若 `comfy.model_management` 未加载 / 抛异常 → 直接放行（`return True`），交由 `setup_backend` 自身 fail-closed 兜底；仅当该模块已加载且其 `is_intel_xpu()` 明确为 False 时才拒绝。另加 `AIMDO_XPU_ENABLED=0` 强制关。与 task #17 收敛版同源。

## F.3 fork / CI
- fork `ayi3030/comfy-aimdo` `main`：`728d6bb` → **`c7374f8`** → **`c2cf98fb986bacea374fddcd2a97a2b8cbb1714d`**（fast-forward）。
  - 注：`c7374f8` 与 `c2cf98f` 两提交**逻辑等价**（AST 去 docstring 后完全相同），差异仅在 docstring/注释；`c2cf98f` 为 tip。二者均触发 CI。
- CI run **37818729077**（sha `c2cf98f`）= **success**（~5 min）。

## F.4 产物（已核实）
下载目录 `aimdo-xpu/ci_artifacts/run_37818729077/`

| 产物 | 大小 | sha256 |
|---|---|---|
| `comfy_aimdo-0.5.6.dev28-cp39-abi3-win_amd64.whl` | 156570 B | `8f2aba921cc24a420bf1225dde6594b6a4cd20fc71848dd77eb20717982ee3f7` |
| `aimdo_xpu.dll` | 238592 B | `fc58a12ca7a2e5dbff2eccef4d7970d58e7934a18e939d5d7111d229fbd543ce` |

- wheel：`0.5.6.dev28` / `gc2cf98fb9`；含 `enables_dynamic_vram`+`is_intel_xpu`+`=0` 逃生舱+非阻断逻辑。PE 导出三项 PASS；导入 `sycl9.dll` PASS。wheel 内 DLL 与独立 DLL 同 sha256。

## F.5 备份 + 安装
- 备份（写入前）：`aimdo-xpu/rollback/dev26_g728d6bb78/`（`comfy_aimdo/` 11 文件 + dev26 dist-info 8 文件）。
- `pip install --no-deps --no-index --force-reinstall <dev28 whl>`：卸载 dev26、安装 dev28，退出码 0。清理 dev26 dist-info + `__pycache__`。

## F.6 部署后验证（全部 PASS）
| 项 | 实测 | 结果 |
|---|---|---|
| `_version.py` | `0.5.6.dev28` / `gc2cf98fb9` | **PASS** |
| 部署 `xpu.py` 含非阻断门禁 | 行 492–561 含 `enables_dynamic_vram`/`is_intel_xpu`/`override=="0"`/`不可用即不阻断` | **PASS** |
| 部署文件 vs wheel | `xpu.py`/`_version.py`/`control.py`/`aimdo_xpu.dll` **逐字节相同** | **PASS** |
| DLL | 238592 B，sha256 `fc58a12c...` | **PASS** |
| import 冒烟 | stdout `0.5.6.dev28 gc2cf98fb9`，exit 0，stderr 空 | **PASS** |

**关键证明（对【已部署】模块，用 embedded python，脚本 `_proof_optin.py`）**：
```
deployed module: ...\site-packages\comfy_aimdo\xpu.py
comfy.model_management loaded at import time: False      # ← 真实启动时序
REAL-STARTUP (mm absent, enables=T, no env): True -> PASS  # ← 修复点（旧版此处 False）
mm present & is_intel=F: False -> PASS
env=0 force-off: False -> PASS ; env=1 force-on: True -> PASS
```
即：在真实「`comfy.model_management` 尚未导入」的调用点上，`_xpu_opt_in()` 现在返回 True，与 dev26 出货版的恒 False 形成对照。

## F.7 launcher（同 #19，已生效）
`run_intel_gpu.bat` 含 `set AIMDO_XPU_ENABLED=1`（兜底），备份 `rollback/launcher/run_intel_gpu.bat.orig`。

## F.8 回滚
```bash
SP="E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/python_embeded/Lib/site-packages"
RB="C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/rollback/dev26_g728d6bb78"
rm -rf "$SP/comfy_aimdo" "$SP/comfy_aimdo-0.5.6.dev28.dist-info"
cp -r "$RB/comfy_aimdo" "$SP/comfy_aimdo"
cp -r "$RB/comfy_aimdo-0.5.6.dev26.dist-info" "$SP/comfy_aimdo-0.5.6.dev26.dist-info"
```

## F.9 待办
- 真机**免 flag 端到端**（真 ComfyUI 拉起，三标记 HIT）由 intel-xpu-adapter / runtime-verifier 用其 shadow harness 复跑（本机未运行 ComfyUI）。
- `c7374f8`/`c2cf98f` 双提交建议后续收敛（本会话未改写历史，未 force-push）。

---

## 8. 独立复核（intel-xpu-adapter，2026-10-09）

对第 1/3/6 节的关键断言做独立复算（不依赖 -2 的中间产物），结果与主审计完全一致：

| 复核项 | 本次独立结果 | 与主审计 |
|---|---|---|
| main HEAD | `9b1efd905bf68086a8195026c28fda1602b80f37`（git ls-remote） | 一致 |
| master HEAD | `bebca5f33916b3417582177597fc734641fcfb29`（git ls-remote） | 一致 |
| 独立 DLL sha256 | `b4241291e962450ed71cd0da9a2a00ff5b1de4e26d86f6b434bc52af5011ce2c`（238592 B） | 一致 |
| wheel sha256 | `f8e2617a953c23a65fb5748a34e45b14e2030ca50d342842854223f8b8876f43` | 一致 |
| wheel `_version.py` | `0.5.6.dev25` / `g9b1efd905` | 一致 |
| wheel 内 DLL sha256 | `b4241291…11ce2c`（与独立 DLL **逐字节相同**） | 一致 |

**XPU 领域一致性备注**：DLL 导入 `ze_loader.dll` 与 `sycl9.dll`，与 oneAPI/Level Zero 运行时的预期依赖链吻合（Windows 下 Level Zero loader 的标准文件名即 `ze_loader.dll`，与 `libze_loader.so.1` 对应），无异常依赖。

**结论**：任务 #8 交付物（本文件）经独立复核，事实无误，可交部署方（任务 #10/#11）使用。DLL 运行时依赖 `sycl9.dll` 的补齐来源仍需部署方核对真机。

---

## 9. 补充说明：`c7374f8` 与 `c2cf98f` 双提交（intel-xpu-adapter，2026-10-09）

fork `ayi3030/comfy-aimdo` main 上存在两个提交 `c7374f8` 与 `c2cf98f`，经 **AST 比对（剥离 docstring）确认为逻辑等价**；其中 **`c2cf98f` 是 tip**，也是**实际被 CI 构建并部署**的那个（CI run `37818729077` → wheel → 已部署 `0.5.6.dev28 / gc2cf98fb9`）。

**本条为补充说明，不改动本文件既有的任何结论、数值、sha256 与行号。** 以文档记录替代历史改写：`__commit_id__=gc2cf98fb9` 已写死在部署产物内，任何 squash / rebase / force-push 都会使已部署字节与上游失联，故**不重写 fork main 历史**。
