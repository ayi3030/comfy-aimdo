# ⛔ P0 FINDING — 出货版自动启用（`728d6bb`, task #19）在真机上**不生效**

- 发现者：intel-xpu-adapter（蓝驭芯）
- 时间：2026-10-09（本地）
- 主机：Windows 11，Intel Arc B580，torch 2.14.0+xpu（真机）
- 结论：**task #19 推送的 `728d6bb`（CI #24）在「免 `--enable-dynamic-vram` 自动启用」目标上不生效**，
  且该版本**已被部署为真机 dev26**（`_version.py` = `0.5.6.dev26` / `g728d6bb78`，mtime 01:39:33）——
  **真机当前就是失效状态**。根因：`_xpu_opt_in()` 条件 3 依赖 `comfy.model_management.is_intel_xpu()`，
  而该模块在 `control.init()`（`main.py:74`）被调用时**尚未导入**（`main.py:258` 才导入）。

> ## ⚠️ 更新（2026-10-09，真机在线判定）
> 1. **CI #24 已完成并部署**：真机 site-packages 现为 **dev26 / g728d6bb78**；
>    其 `xpu.py` 与 `git 728d6bb:comfy_aimdo/xpu.py` **逐字节相同（忽略 CR/LF）** → 即硬门禁版。
> 2. **真机在线实测（部署版 dev26，无 shadow，无 flag）**：
>    `DynamicVRAM support detected and enabled` / `published 1 SYCL queue(s)` / `backend ready` **全 miss**；
>    出现 `XPU backend not requested` + `No working comfy-aimdo install detected`；日志含 `comfy-aimdo version: 0.5.6.dev26`。
>    → **当前部署的 dev26 未实现 #17 目标。**（`_e2e_optin_deployed_dev26.log`）
> 3. **三向实测矩阵**（同一 harness，真机 B580，均无 flag）：
>
>    | xpu.py | `_xpu_opt_in` 守卫 | 结果 |
>    |---|---|---|
>    | 部署 dev25（基线） | 无 | ❌ miss |
>    | **出货 dev26 / `728d6bb`** | **硬条件**（mm 缺失 → `return False`） | ❌ miss（真机现状） |
>    | 收敛版 `PATCH_xpu_opt_in_alignment.patch`（task #17） | **非阻断**（mm 缺失 → `return True`） | ✅ **PASS** |
>
>    唯一决定性差异 = 函数末尾 `return False` ↔ `return True`。
>    收敛版日志：`_e2e_optin_conv.log`（三个正标记 HIT，两个 NEG miss）。
>
> 4. **【收尾 · task #21 / #23】修复已落地并部署**：fork `ayi3030/comfy-aimdo` main `728d6bb` → `c7374f8` → **`c2cf98f`**（tip，无 force-push）。
>    - CI **run `37818729077`**（sha `c2cf98f`）= **SUCCESS**；wheel `comfy_aimdo-0.5.6.dev28-cp39-abi3-win_amd64.whl` sha256 `8f2aba921cc24a420bf1225dde6594b6a4cd20fc71848dd77eb20717982ee3f7`。
>    - 已部署到真机：`__version__=0.5.6.dev28` / `__commit_id__=gc2cf98fb9`；`aimdo_xpu.dll` sha256 `fc58a12ca7a2e5dbff2eccef4d7970d58e7934a18e939d5d7111d229fbd543ce`；`xpu.py` md5 `d90ebc8aceedd3364d2ffba182357daa`。
>    - 修复点：`_xpu_opt_in()` 条件 3 在 `comfy.model_management` 缺失时**不阻断**（`return True`），收口到 task #17 的同一实现；**两份分叉实现已合一**。
>    - 回滚点：`aimdo-xpu/rollback/dev26_g728d6bb78/`。
>    - ⚠️ **状态限定**：*以上为部署与门禁层面的既成事实；**端到端功能性验收（真机免 flag 跑真模型工作流）截至本注记时仍在进行，结论未出**。*
>    - 完整根因链见 `aimdo-xpu/EVIDENCE_optin_gate_race.md`（task #17 owner 撰）。
> 5. **【收尾 · task #23】端到端验收已通过（ALL GREEN）**：`aimdo-xpu/logs/acc_20261009-015303_*`，runner **exit 0 / ALL GREEN**，**全程未触发任何回退（no-fallback）**，且**未加 `--enable-dynamic-vram`**（免 flag）——门禁在 **21.2s** 内 ready，即 #17/#19 的既定目标已达成。产出物均**非退化**：
>    - SD1.5 文生图：`p3_small_00002_.png`（**375,299 B**）。
>    - MiniMax H3 视频：`p3_h3_00001_.mp4`（**1,101,392 B**，耗时 **198.99s**）；容器为**非退化真实视频** —— `mvhd timescale=96000 duration=496002` → **5.167s = 124 帧 @24fps**，`mdat` = **1,093,980 B**（存在真实码流，非空壳）。
>    - 显存分页/换出量级：**7 个模型**成功 staging，峰值需求 **41,826 MB vs 物理 11,876 MB = 3.52×**；单模型峰值 **19,995 MB = 物理的 1.68×** → **物理上不可能全量驻留**，侧面证明 VMM 分页换出链路确实在工作。
>    - ⚠️ **诚实边界**：本轮日志**未逐笔打印逐次 evict/offload 行**；命中压力关键词者均为 `[OmniXPU] AIMDO inference budget: devices=0 …` 提示行。故本条结论建立在「**机制确在使用 + 需求物理上不可能全驻留**」之上，**并非**来自观测到的卸载日志。
>    - 首轮 `acc_20261009-015132` 的 H3 失败系**工装缺陷**（模型名含正斜杠 → `value_not_in_list`），**非**产品缺陷；重跑即全绿。


---

## 1. 结论先行（实测）

用「影子包 + 运行期 sys.path 注入」在真机**不带** `--enable-dynamic-vram` 拉起真实 ComfyUI，
影子包 = 部署 dev25 包 + `xpu.py` 替换为 **`git 728d6bb:comfy_aimdo/xpu.py`**：

| token（期望） | 出货版 `728d6bb` 实测 |
|---|---|
| `DynamicVRAM support detected and enabled` | **miss** ❌ |
| `published 1 SYCL queue(s) to the native backend` | **miss** ❌ |
| `backend ready (mode=native_hook)` | **miss** ❌ |
| `No working comfy-aimdo install detected` | **HIT**（回落） |
| `XPU backend not requested; using native PyTorch XPU allocator` | **HIT**（门禁拒了） |
| 服务器横幅 | HIT（启动正常） |

日志：`aimdo-xpu/_e2e_optin_ship.log`（supervisor 输出 `RESULT: PASS(banner)`，但三个**正**标记全 miss）。

---

## 2. 决定性证据（证明影子确被加载 + 根因）

在同一影子 `xpu.py` 的 `_xpu_opt_in()` 入口插入一次性诊断，重跑后日志逐字：

```
[WARNING] SHADOW-SHIP-MARKER xpu.py=C:\Users\HE\WorkBuddy\2026-10-08-23-50-54\aimdo-xpu\_e2e_pkg_ship\comfy_aimdo\xpu.py \
  | model_management_loaded=False | is_intel_xpu=None \
  | cli_args_loaded=True | enables_dynamic_vram_present=True
```

逐项含义：
- `xpu.py=...\_e2e_pkg_ship\...` → **影子包确被加载**（排除"其实跑的是部署版"的歧义）。
- `model_management_loaded=False` → 调用 `_xpu_opt_in()` 时 `comfy.model_management` **不在 `sys.modules`**。
- `is_intel_xpu=None` → 出货版条件 3 里的 `callable(is_intel) and is_intel()` → **False** → 不放行。
- `cli_args_loaded=True` / `enables_dynamic_vram_present=True` → ComfyUI 侧判据本已就绪、且会返回 True，
  **唯独被 `comfy.model_management` 这层依赖挡死**。

（诊断插入后已还原影子为出货版原始字节：sha256 `b9cfb0f7f9d60b2b6f30fff82e568143b9ad133a95c2f65ff6d74ade8e481090`。）

---

## 3. 根因：导入顺序竞态

`ComfyUI/main.py` 的真实行号（部署运行时）：

```
74:  if enables_dynamic_vram():
77:      comfy_aimdo.control.init(simple_vram_headroom=..., nvml_pressure=...)   # ← 这里会调 _xpu_opt_in()
81:      comfy_aimdo.control.init(simple_vram_headroom=...)
84:      comfy_aimdo.control.init()
...
258: import comfy.model_management                                                # ← 直到这里才导入
```

`_xpu_opt_in()` 在 **line 74~84** 被调用，而 `comfy.model_management` 到 **line 258** 才导入。
因此出货版条件 3 中的 `sys.modules.get("comfy.model_management")` 恒为 `None`，
在**任何真实无 flag 启动**中都必然返回 False。

> 换言之：`is_intel_xpu()` 这道「原子守卫」把判据从「ComfyUI 已判定动态显存开启」变成了
> 「ComfyUI 已判定开启 **且** 某个尚未导入的模块可用」，后者在真实启动序列下恒假。

---

## 4. 两个相互冲突的实现（关键协调问题）

同一 task，仓库里存在**两份不同实现**，且**代码不同**：

| | **出货版 `728d6bb`**（作者 intel-xpu-adapter-2 / task #19） | **本地补丁 `PATCH_xpu_opt_in_alignment.patch`**（作者 comfyui-python-engineer / task #17） |
|---|---|---|
| 位置 | 已推送 `ayi3030/comfy-aimdo` main；CI #24 构建中 | 仅文件，**未推送** |
| 条件 3 判据 | `enables_dynamic_vram()` **AND** `comfy.model_management.is_intel_xpu()` | 仅 `enables_dynamic_vram()`（无 mm 依赖） |
| `AIMDO_XPU_ENABLED=0` 强制关 | ❌ 无 | ✅ 有 |
| 真机无 flag 实测 | **miss（不生效）** | **HIT（生效）**（`_e2e_optin_fixed.log`） |

- 依据 task #17 说明，`comfyui-python-engineer` 的实现本就是**候选人 (b)「同源判据」**，且真机 A/B 已 PASS。
- `intel-xpu-adapter-2` 的实现（`#19`）**额外加了 `is_intel_xpu()` 守卫**，正是该守卫导致失效。
- 二者**并非同一份代码的两次验证**，而是**两个不同实现**；出货的是失效的那一份。

（`is_intel_xpu()` 守卫在此处**既不必要也有害**：`_xpu_opt_in()` 只在 `setup_backend()` 内被调用，
而 `setup_backend()` 只在 `control.implementation=="xpu"` 时进入，后者由 `detect_vendor()` 依据
`'+xpu' in torch.version` 判定 —— 已是 XPU 专属路径。叠加 `comfy.model_management` 只带来时序竞态。）

---

## 5. 影响面

- **CI run #24**（`head_sha=728d6bb`，`in_progress`，见下）产出的 dev26 **不会**实现「Intel 免 flag 自动启用」。
- 若按现流程部署 dev26，`--enable-dynamic-vram` 路径仍正常（条件 2），但 #17/#19 的核心目标落空，
  且对外表现为「修复了但没效果」，难以察觉。

CI 侧证据（GitHub REST API，2026-10-09 实测）：
- `GET /repos/ayi3030/comfy-aimdo/commits/main` → `sha=728d6bb78e8241211f914387453ebf9912f4b0f9`，
  message = "feat(xpu): auto-enable DynamicVRAM on Intel XPU without --enable-dynamic-vram"。
- `GET .../actions/workflows/build-xpu-windows.yml/runs` → run **#24**, `head_sha=728d6bb`, `status=in_progress`,
  `created_at=2026-10-08T17:33:19Z`。

---

## 6. 建议修复（供 owner 采用）

**最小且正确**：让条件 3 **不依赖** `comfy.model_management`。二选一：

- **(推荐) 采用 task #17 的既有实现**（`PATCH_xpu_opt_in_alignment.patch`）：条件 3 只读
  `comfy.cli_args.enables_dynamic_vram()`，`_xpu_opt_in()` 只在 XPU 路径可达，无需 mm 守卫。
  真机已 A/B PASS。
- 或保留 `is_intel_xpu()` 语义但改为**惰性/健壮判据**（例如用 `torch.version`/`torch.xpu.is_available()`
  代替 `comfy.model_management`），避免依赖 main.py:258 才导入的模块。

**流程建议**：
1. 在 CI #24 完成前，**不要**部署其产物为「已实现 #17 目标」。
2. 以修正版提交（新 commit）→ 触发 CI #25 → 部署 → 由我（或 runtime-verifier）重跑 §1 的免 flag E2E。
3. 同步：统一 #17 / #19 两份实现，避免仓库内继续存在两个"自动启用"版本。

---

## 7. 复现步骤（可重放）

```bash
# 1) 构建影子包：部署 dev25 包 + 出货版 xpu.py
F=<fork-clone>                       # ayi3030/comfy-aimdo
D=<deliverables>/aimdo-xpu
cp -r <site-packages>/comfy_aimdo "$D/_e2e_pkg_ship/comfy_aimdo"
git -C "$F" show 728d6bb:comfy_aimdo/xpu.py > "$D/_e2e_pkg_ship/comfy_aimdo/xpu.py"

# 2) 无 flag 拉起真实 ComfyUI（_e2e_ship_launch.py 在 sys.path[0] 注入影子包）
<portable>/python_embeded/python.exe -s "$D/_e2e_ship_supervisor.py"
# 观察 _e2e_optin_ship.log：三个正标记应 HIT，实际 miss
```

产物：
- `_e2e_ship_launch.py` / `_e2e_ship_supervisor.py` / `_e2e_pkg_ship/`
- `_e2e_optin_ship.log`（出货版，无 flag，miss）
- 对照：`_e2e_optin_fixed.log`（#17 实现，HIT）/ `_e2e_optin_control.log`（dev25 原样，miss）
