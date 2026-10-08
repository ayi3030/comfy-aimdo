# DOC_INVENTORY_stale — `aimdo-xpu/*.md` 陈旧句清单（只读分析）

- 作者：intel-xpu-adapter（蓝驭芯）
- 日期：2026-10-09
- 性质：**纯只读分析报告**。本次未修改除本文件外的任何文件（未改 `aimdo-xpu/*`、未改 fork、未动 site-packages、未运行 ComfyUI）。
- 判据（team-lead 定义）：**任何会让读者得出与「当前事实」相反结论的句子，都算「已过时」，必须点名到句。**

---

## 0. 当前事实基线（判定「过时」的锚点）

> 以下为 2026-10-09 报告成文时的**已核实事实**。凡与该基线相反的文档句子 ＝ 过时。

| 编号 | 当前事实 | 核实方式 |
|---|---|---|
| F1 | 真机 = Windows 11 + **Intel Arc B580**；torch **2.14.0+xpu**；oneAPI **2026.1.0**（sycl9.dll）；`python_embeded` Python **3.13.14**。机器**有** Intel Arc 硬件。 | `VERIFY_real_b580.md` §1；`torch.xpu.is_available()=True` |
| F2 | **(a) Level Zero VMM 在 B580 上可用 = PASS**；**(b) PyTorch 接受并回收 VMM 设备 VA = PASS**。两项「最大不确定性」**已实机消除**。 | `VERIFY_real_b580.md` §2/§3 |
| F3 | `ZE_STRUCTURE_TYPE_PHYSICAL_MEM_DESC` 真值 **`0x20`**、`CONTEXT_DESC` **`0x0D`**，均**经真机驱动接受**。 | `实现说明.md` §2；`VERIFY_real_b580.md` §5 |
| F4 | 设备真实总量 **11.60 GiB**（`zeDeviceGetMemoryProperties`，name="DDR"），非 12 GiB 占位。 | `VERIFY_real_b580.md` §1 |
| F5 | XPU C 代码**已编译为 `aimdo_xpu.dll` 并部署到真机 site-packages、成功加载运行**（自 dev25 起）。 | `AUDIT_fork_and_artifact.md` §3/§D；`VERIFY_real_b580.md` §4 |
| F6 | 免 flag 门禁：**dev26（`728d6bb`）失效**（硬条件）；**dev28（`gc2cf98fb9`，当前已部署）已修复**（非阻断守卫 → 无 flag 自动启用）。 | 实测 site-packages `_version.py`=`0.5.6.dev28`/`gc2cf98fb9`；`xpu.py` sha256 `81726334f4e56b3e692191b93ac30539d8e375712c8cccb3170bfce7705b8d6a`，`_xpu_opt_in()` 末行 `return True`（L562） |
| F7 | `ComfyUI/nodes.py` 启动崩溃**已修复（task #14）**，flag/no-flag 两模式均到达服务器横幅。 | `FIX_comfyui_startup_blocker.md`；`VERIFY_p3_endtoend.md` §9 |
| F8 | 含大模型加载 / 三层卸载压力往返 —— **已于 2026-10-09 P4 验收通过（全绿，证据 `logs/acc_20261009-015303_*`）**。 | `VERIFY_p3_endtoend.md` §10（P4 验收） |

> ⚠️ 注意：本清单成文时 **F8 仍未验证**，故相关句子当时未列入过时。**2026-10-09 P4 验收已通过（全绿）**，F8 现转为「已验证」（更新见 §7）。

---

## 1. 陈旧句总览表

| 文件 | 主题 | 是否被 dev26→dev28 推翻 | 过时句数 |
|---|---|---|---|
| `交付说明.md` | 交付/部署/风险（v2 视角） | 部分被 F1/F5 推翻（先于 dev 线） | **8** |
| `实现说明.md` | 逐文件改动笔记 | 部分被 F1/F5 推翻 | **1**（+1 低危） |
| `PHASE2_DESIGN.md` | 原始设计（Track 1/2） | 被 F1/F2/F5 推翻 | **5** |
| `REVIEW_phase4_xpu.md` | `dispatch.c` 逐字审查 | 被 F2/F5 推翻 | **2** |
| `PATCH_comfyui_intel_gate.md` | main.py 门禁补丁 | 被 F6 推翻（归因缺失） | **3** |
| `EVIDENCE_optin_ab.md` | 免 flag A/B 证据 | 被 F6 推翻 | **3** |
| `EVIDENCE_optin_gate_race.md` | 门禁竞态根因 | 被 F6 推翻 | **2** |
| `FINDING_shipping_optin_broken.md` | P0 出货失效（**已冻结**） | 被 F6 推翻 | **2** |
| `EVIDENCE_optin_race_matrix.md` | 三向矩阵冻结记录 | 被 F6 推翻 | **2** |
| `PATCH_xpu_opt_in_alignment.md` | 收敛版补丁说明 | 被 F6 推翻 | **4** |
| `VERIFY_e2e_hook_b580.md` | UR 钩子 E2E + 门禁根因 | 被 F6/F7 推翻 | **5** |
| `VERIFY_p3_endtoend.md` | P3 独立复验 | 被 F6/F7 推翻 | **3** |
| `VERIFY_task14_startup_unblocked.md` | task #14 启动复验 | 被 F6 推翻 | **1** |
| `AUDIT_fork_and_artifact.md` | fork/CI 产物审计 | 被 F6/F5 推翻 | **2** |
| `FIX_comfyui_startup_blocker.md` | nodes.py 启动修复 | 被 task #20 推翻 | **1** |
| `STATUS_v2_candidate.md` | v2 冻结轨道 | 未被推翻（仅范围未限定） | **0**（+1 低危） |
| `VERIFY_real_b580.md` | (a)/(b) 真机探针 | 未被推翻（仅措辞） | **0**（+1 低危） |
| **合计** | | | **44** |

---

## 2. 逐句清单（点名到句，含行号）

### `交付说明.md`（md5 `0094409920cddf53aff95c4b13216391`）— 8 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S1 | L10-11 | 「本环境**无 Intel Arc 硬件、无 CUDA / Level Zero 编译环境**，因此：……**全部 C 代码均「未编译 / 未实机验证」**」 | 与 F1/F5 相反（真机有 B580，C 代码已编译并部署运行） | 删除或改写为「历史前提（沙箱阶段）」并指向 `VERIFY_real_b580.md` |
| S2 | L13-15 | 「两项**必须**在 Arc 机器上完成的实机验证：(a) …… (b) ……」 | 与 F2 相反（(a)(b) 均已 PASS） | 改为「已由 `VERIFY_real_b580.md` 完成」 |
| S3 | L16 | 「`ZE_STRUCTURE_TYPE_PHYSICAL_MEM_DESC` 数值待核对。」 | 与 F3 相反（真值 0x20 已确认） | 删「待核对」字样 |
| S4 | L56-58 | 「torch 2.7.0（torch-xpu wheel）/ intel-extension-for-pytorch 2.7.10+xpu …… Python 3.11–3.12」 | 与 F1 相反（真机 torch 2.14.0+xpu / oneAPI 2026.1.0 / Py3.13.14） | 更新为实测版本矩阵 |
| S5 | L104 | 「(b) …… 此为最大不确定性。」 | 与 F2 相反（(b) 已 PASS） | 删除「最大不确定性」标注 |
| S6 | L113-114 | 「**R1**：……待 (a) 验证。**R2**：……待 (b) 验证（最大不确定性）。」 | 与 F2 相反 | 标记 R1/R2 已关闭 |
| S7 | L115 | 「`ZE_STRUCTURE_TYPE_PHYSICAL_MEM_DESC` 数值（0x1D）……待对照 `ze_api.h` 核对。」 | 与 F3 相反（0x1D 是错的，真值 0x20） | 更正为 0x20 |
| S8 | L117 | 「**R5**：设备总显存用占位值 `XPU_FALLBACK_VRAM`(12GiB)，正式实现应改用 `zeDeviceGetMemoryProperties`」 | 与 F4 相反（已接真实查询；实测 11.60 GiB） | 标记已实现 |

### `实现说明.md`（md5 `058cf9b4db6777221c72f8342811b14f`）— 1 句（+1 低危）

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S9 | L12-13 | 「本沙箱**无 Intel Arc 硬件、无 CUDA / Level Zero 编译环境**，因此：**XPU 相关 C 代码本身仍未作为 comfy-aimdo 扩展整体编译/运行**」 | 与 F1/F5 相反；且与本文件 §0 紧随其后的「【更新】核心设计路径已获真机验证」**自相矛盾** | 头两句加「（沙箱阶段基线）」，或直接删 |
| — | L95 | 「**待 End-to-End（任务 #11）**：把上述路径编进 `aimdo_xpu` 扩展……」 | 低危：「编进扩展」已达成（F5）；大模型工作流**已 P4 验收通过** | 已补「扩展已编译部署；大模型压力往返已验收通过」 |

### `PHASE2_DESIGN.md`（md5 `d26185aa078c7b8c0bb9e8895121077e`）— 5 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S11 | L15 | 「版本矩阵 \| 推荐 \| torch 2.7.0 (torch-xpu wheel) / IPEX 2.7.10+xpu / Level Zero 驱动 Rolling 2506.18+ / Python 3.11–3.12。」 | 与 F1 相反 | 更新为实测矩阵 |
| S12 | L18 | 「**两项必须实机验证（本沙箱无 Intel 硬件，无法代验）：**」 | 与 F1/F2 相反 | 标记已完成 |
| S13 | L36 | 「风险：高。**本沙箱无 Intel 硬件，无法编译/实机验证**」 | 与 F1/F5 相反 | 同上 |
| S14 | L126-127 | 「R1：……待 (a) 验证。R2：……待 (b) 验证。」 | 与 F2 相反 | 关闭 R1/R2 |
| S15 | L128 | 「**R3**：Track 2 C 代码在沙箱无法编译，质量靠代码审查 + 用户实机验证」 | 与 F5 相反（XPU 后端已编译） | 标记已编译 |

### `REVIEW_phase4_xpu.md`（md5 `c753490864f5e9d003bd931d25a09186`）— 2 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S16 | L48-51 | 「## 3. 无法在本环境判定的项（须实机）- (a) …… 是否成功…… - (b) …… 能否识别/回收（最大不确定性）。」 | 与 F2 相反（(a)(b) 均 PASS） | 该节改标题为「曾须实机（现已由 `VERIFY_real_b580.md` 关闭）」 |
| S17 | L63 | 「R-g/R-h 潜在集成问题 \| **待实机/构建期确认**」 | 与 F5 相反（构建已完成、DLL 已部署加载） | 标记已确认 |

### `PATCH_comfyui_intel_gate.md`（md5 `63062387578876347afe1eeabbb200c8`）— 3 句（**误导类**）

| # | 行 | 过时/误导句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S18 | L80 | 「打上本补丁后，Intel Arc B580 在默认启动条件下将**自动启用 DynamicVRAM，无需再传 `--enable-dynamic-vram`**」 | 写作时该结论**为假**（被 `_xpu_opt_in()` 第二段门禁挡住，见 F6/`FINDING_…`）；现因 dev28 **另一处**修复才成立——**本补丁单独不足以成立**。读者会误以为「只改 main.py 就够了」。 | 补注：需 comfy_aimdo 侧 dev28 非阻断门禁配合 |
| S19 | L15-17 | 「导致 Intel Arc 用户必须显式传 `--enable-dynamic-vram` 才能启用 DynamicVRAM。本补丁追加一个 Intel 分支，使 XPU 可用时返回 True」 | 同上——暗示「本补丁即足够」 | 同上补注 |
| S20 | L80-86 | 「……但需同时满足以下两个前置（均与本次改动无关）……1.…… 2.…… 3.……」 | **前置清单遗漏了第二段门禁**（`comfy_aimdo._xpu_opt_in()`），读者按此清单会得出「前置齐了就自动启用」的错误结论 | 补第 4 前置：`comfy_aimdo` 侧门禁（dev28 已修） |

### `EVIDENCE_optin_ab.md`（md5 `87adb6269246d9a4cabf3a88ca2d54fe`）— 3 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S21 | L3 | 「The shipped dev26 gate (fork 728d6bb78) is the broken one; **the fix is being re-landed as task #21.**」 | 与 F6 相反（task #21 已完成，dev28 已部署） | 改「the fix was landed as dev28 (`c2cf98fb9`)」 |
| S22 | L8 | 「出货版 `728d6bb78` 的门禁是**坏的**」 | 「出货版」现指 dev28（已修），措辞过时 | 改「曾出货的 dev26」 |
| S23 | L90 | 「fork 克隆 …… HEAD 仍 `9b1efd905`，无本地 commit」 | 与 F6 相反（fork main 已推到 `728d6bb`→`c2cf98fb9`，已构建 dev28） | 更新 fork 状态 |

### `EVIDENCE_optin_gate_race.md`（md5 `caec90c4d127271d5f69fcbdb8644c42`）— 2 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S24 | L3 | 「The shipped dev26 gate (fork 728d6bb78) is the broken one; **the fix is being re-landed as task #21.**」 | 与 F6 相反（已落地 dev28） | 同 S21 |
| S25 | L9 | 「**出货版 dev26（fork `728d6bb78`）的免 flag 路径是断的**」 | 「出货版」现指 dev28，措辞过时 | 改「曾出货的 dev26」 |

> 说明：本文件的**根因分析（导入时序竞态、`main.py:74` vs `:258`）仍准确**，是 dev28 修复的依据；仅顶部「出货版 / 待 re-land」措辞过时。

### `FINDING_shipping_optin_broken.md`（md5 `e174b97d9718f4f9ec614b784bae53cb`）— 2 句 ❄️**已冻结**

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S26 | L1（标题） | 「⛔ P0 FINDING — 出货版自动启用（`728d6bb`, task #19）在真机上**不生效**」 | 与 F6 相反：**当前出货版是 dev28（已修）**；残留标题会使读者以为「当前出货版仍失效」 | ❄️ **不改写**（team-lead 已冻结此文件；其历史价值＝记录 dev26 缺陷）。仅登记。 |
| S27 | L139 | 「1. 在 CI #24 完成前，**不要**部署其产物为「已实现 #17 目标」。」 | 该建议已执行且已被 dev28 取代 | ❄️ 同上，不改写 |

> ❄️ 冻结约束来自 team-lead：**除 `comfyui-python-engineer` 可做文件名 token 修正外，任何人不改此文件**；不得恢复原文。本清单仅登记其过时性，供未来「补充说明/附录」时参考。

### `EVIDENCE_optin_race_matrix.md`（md5 `e23702c1c91ed2eed808b1e7c3613d0a`）— 2 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S28 | L16 | 「**出货 dev26 @`728d6bb`**（已部署）」 | 「已部署」现为 dev28（F6） | 改「曾部署的 dev26」 |
| S29 | L132 / L136 | 「收敛版（非阻断）→ PASS …… 但当时**尚未经 CI 构建与 pip 部署**」/「收敛版升格为「已上线可用」需：CI 重建（dev27）→ pip 部署 → 部署产物级复验」 | 与 F6 相反（收敛逻辑已由 dev28 构建部署上线） | 补注「已由 dev28 落地」 |

> 说明：本文件性质即「冻结记录」，其 `§5 证据等级声明` 本身带「截至成文时」语义；但 S28/S29 两句按字面会与当前事实相反，故列出。

### `PATCH_xpu_opt_in_alignment.md`（md5 `de026294d6bf2269a6e1c2860ac59132`）— 4 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S30 | L1 | 「The shipped dev26 gate (fork 728d6bb78) is the broken one; **the fix is being re-landed as task #21.**」 | 与 F6 相反 | 同 S21 |
| S31 | L10 | 「与 task #19 的关系：本文实现与 #19 规格**已收敛为同一版本**」 | 与事实相反：#19 出货的 `728d6bb` 把条件 3 做成**硬门禁**，与本文件「非阻断」实现**并未同一版本**（正是 P0 根因） | 改为「曾声称已收敛，实际分歧见 `FINDING_…`；最终由 dev28 落地非阻断版」 |
| S32 | L113 | 「**基线**：fork `ayi3030/comfy-aimdo` `main` HEAD `9b1efd905`」 | 与 F6 相反（main 已到 `c2cf98fb9`） | 更新 fork HEAD |
| S33 | L137 | 「本补丁**未**推送 fork、**未**触发 CI 重建（无推送凭据）」 | 与 F6 相反（已推送并构建 dev28） | 标记已落地 |

### `VERIFY_e2e_hook_b580.md`（md5 `b7a4a55f378ee254dd00ebc936b7d147`）— 5 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S34 | §0 表 L21 | 「真机 ComfyUI 是否无 flag 自动启用 DynamicVRAM \| **FAIL**（被 `_xpu_opt_in()` 挡住）」 | 与 F6 相反（dev28 已 PASS） | 改写为「曾 FAIL；dev28 后 PASS」 |
| S35 | §0 表 L22 | 「ComfyUI 能否启动到服务横幅 \| **FAIL**（`nodes.py` 的 `NameError`，与 AIMDO 无关）」 | 与 F7 相反（已修） | 标记已解决 |
| S36 | §0 L25-27 | 「当前端到端验收被**两个与本适配无关的阻塞项**挡住……」 | 与 F6/F7 相反（两个阻塞项均已解除） | 更新结论 |
| S37 | §4（L106-152） | 「阻塞项一：ComfyUI `nodes.py` 启动崩溃（与 AIMDO 无关）」整节 | 与 F7 相反 | 加「已由 task #14 修复」抬头 |
| S38 | §5（L155、L166） | 「阻塞项二：comfy_aimdo `_xpu_opt_in()` 第二段门禁」/「`_xpu_opt_in()` 仅在 `AIMDO_XPU_ENABLED=="1"` 或……返回 True」 | 与 F6 相反（dev28 改为非阻断） | 加「dev28 已修」抬头 |

### `VERIFY_p3_endtoend.md`（md5 `a71a8bdc08b74268c7621a6c52744308`）— 3 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S39 | L7-11 | 「结论速览：**未通过验收**」+「整体验收结论不变：**未通过**」 | 与 F6 相反（门禁已由 dev28 修复；#9 相关 FAIL 应转 PASS） | 待 P4 复跑后更新总评 |
| S40 | §5 裁决 L123-126 | 「「无需 `--enable-dynamic-vram` 即在 Intel 上自动启用 DynamicVRAM」= **NO**」 | 与 F6 相反 | 标「当时 NO；dev28 后 YES（待 P4 复验）」 |
| S41 | §9.5 L240 | 「免 flag 自动启用（第 5 节交付目标）：**仍未解决** ❌（阻塞点未动）」 | 与 F6 相反（阻塞点已修） | 同上 |

> 说明：本文件 §9 已把「启动阻塞」转 PASS（F7），故 §6 的 nodes.py 崩溃描述被 §9 覆盖，不重复计数。

### `VERIFY_task14_startup_unblocked.md`（md5 `d83d73ba5e896b38c56a4498a0faf879`）— 1 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S42 | §3.3 L66 | 「**`_xpu_opt_in()` 第二段门禁**（……）：不带 flag 时 DynamicVRAM 不会自动启用；……属设计决策。」 | 与 F6 相反（已修，不再是「设计决策」） | 标记 dev28 已修 |

### `AUDIT_fork_and_artifact.md`（md5 `6f4220310c14c011609ef407453fc97b`）— 2 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S43 | §1 L14 | 「\| `main` \| `9b1efd905bf68086a8195026c28fda1602b80f37` \| `git ls-remote` \|」 | 与 F6 相反（main 现已 `c2cf98fb9`） | 更新 fork HEAD（或注明「审计时快照」） |
| S44 | §2 L42 | 「**最新成功运行 = #23 / run_id `37773546816`**」 | 与 F6 相反（已有 dev26/#24、dev28 的更新运行） | 注明本表为审计时快照；当前态见 §F |

> 说明：本文件 §D/§E/§F 为**滚动追加的部署日志**，已含 dev25→dev26→dev28 记录，**最新段（§F）为当前态**；仅 §1/§2（审计时快照）过时。

### `FIX_comfyui_startup_blocker.md`（md5 `3ac052d08cebbcda057ec376747ec8ea`）— 1 句

| # | 行 | 过时句（逐字） | 为何过时 | 建议动作 |
|---|---|---|---|---|
| S45 | §6 L204 | 「**建议**：…… `sync_to_runtime.sh` 对 `nodes.py` / `model.py` **只可 `--check`，不可写入**；……（本项超出本次授权范围，仅上报，未改脚本。）」 | 被 task #20 推翻（`sync_to_runtime.sh` 已加固：跨 revision 文件禁止整文件覆盖） | 补注「已由 task #20 加固」 |

---

## 3. 低危 / 需限定（未计入 44）

| 文件 | 行 | 句子 | 说明 |
|---|---|---|---|
| `实现说明.md` | L95 | 「待 End-to-End（任务 #11）……编进 `aimdo_xpu` 扩展」 | 扩展已编译部署；大模型压力往返**已 P4 验收通过** → 已处理 |
| `STATUS_v2_candidate.md` | L11 | 「`src-xpu/dispatch.c` 从未被任何编译器编译过。」 | 该句**在 v2 轨道范围内仍成立**（v2 的 `dispatch.c` 确未编译）；但**范围未限定**，易被读成「XPU 后端从未编译」（与 F5 相反）→ 建议加「（指 v2 的 `dispatch.c`；出货 路 A 的 `dispatch.cpp` 已编译部署）」 |
| `VERIFY_real_b580.md` | §6 | 「**未**运行完整 ComfyUI 工作流 / 真实模型加载 / VPU 卸载往返」 | 「完整 ComfyUI 工作流」现已在 flag 模式下跑过（`VERIFY_e2e_hook_b580.md` §3）；仅大模型三层卸载仍未跑 → 建议收窄为「未跑大模型三层卸载往返」 |

---

## 4. 计数与结论

- **已过时句子总数 = 44**（点名到句，见 §2；按文件分布见 §1）。
  - 归因：**被 dev26→dev28 门禁修复（F6）推翻** ≈ 26 句（PATCH_gate / 两份 EVIDENCE / FINDING / race_matrix / PATCH_alignment / VERIFY_e2e_hook / VERIFY_p3 / VERIFY_task14 / AUDIT 的部分）。
  - 归因：**被「真机 B580 存在 + (a)(b) PASS + C 代码已编译」推翻（F1/F2/F5）** ≈ 16 句（交付说明 / 实现说明 / PHASE2 / REVIEW）。
  - 归因：**被 task #14 / #20 推翻（F7 + sync 加固）** = 2 句（VERIFY_e2e_hook §4 / FIX_startup §6）。
  *(分类有交叉，按「主因」归口，合计仍为 44。)*
- **另有 3 句低危/需加范围限定**（§3），未计入 44。
- **零过时**：`STATUS_v2_candidate.md`（自觉冻结快照）、`VERIFY_real_b580.md`（(a)/(b) 事实源）——仅 §3 列出的措辞项。
- **F8（大模型三层卸载压力往返）**：本清单成文时未验证，故未列入过时；**2026-10-09 P4 已验收通过**（更新见 §7）。

### 建议处理优先级（供 team-lead 决策）
1. **P0 误导**：`PATCH_comfyui_intel_gate.md` §「B580 自动启用结论」（S18-S20）——是唯一会主动误导「只改 main.py 即足够」的文档。
2. **P1 措辞**：`FINDING_shipping_optin_broken.md`（S26/S27，❄️冻结，仅登记）、`EVIDENCE_optin_ab.md` / `EVIDENCE_optin_gate_race.md` / `PATCH_xpu_opt_in_alignment.md` 顶部「出货版 / 待 re-land」（S21-S25、S30-S33）。
3. **P2 快照标注**：`AUDIT_fork_and_artifact.md` §1/§2（S43/S44）、`VERIFY_*` 系列（S34-S42）。
4. **P3 前提更新**：`交付说明.md` / `实现说明.md` / `PHASE2_DESIGN.md` / `REVIEW_phase4_xpu.md`（S1-S17）——属「沙箱阶段前提」被真机推翻，可在文首加一句「本文为沙箱阶段前提，实机结论以 `VERIFY_real_b580.md` 为准」批量处理。

---

## 5. 方法与边界

- 方法：逐字通读 `aimdo-xpu/*.md` 全部 17 个文件；对每句判断是否与 §0 基线相反；反例（如「未跑大模型压力往返」）不列入。
- 基线来源：`VERIFY_real_b580.md`（(a)(b)/11.60GiB/64KiB）、实测 `site-packages/_version.py`=`0.5.6.dev28`/`gc2cf98fb9` 与 `xpu.py` sha256（F6）、`FIX_comfyui_startup_blocker.md`（F7）。
- 边界：**本次为只读分析**。除本报告外未创建/修改任何文件；未改 fork 历史；未动 site-packages；未运行 ComfyUI。行号为报告成文时各文件当前行号，文件若被编辑行号会漂移。

---

## 6. 更正执行记录（2026-10-09 追加）

team-lead 已授权**定点更正**（只改已被当前事实坐实的句子），并区分两套改法：

- **叙述文档**（`PATCH_comfyui_intel_gate.md` / `交付说明.md` / `实现说明.md` / `PHASE2_DESIGN.md` / `REVIEW_phase4_xpu.md`）＝**改写误导句**。
- **证据档案**（`EVIDENCE_*` / `VERIFY_*` / `AUDIT_*`）＝**仅顶部追加「⚠️ 后续事实」注解，原文数值/sha/行号/日志不改**。
- **排除**：`FINDING_shipping_optin_broken.md`（未触碰）；F8（大模型端到端 / 三层卸载压力往返）相关表述当时保持原样（**现 P4 已验收通过**，见 §7）。

- 已执行：**19 条句子级更正 + 8 处证据注解**（≤ 本盘点 44 句；未扩大范围）。
- 详细逐条「文件+行号+原文+新文+依据」与**逐文件改前/改后 md5**：见 **`aimdo-xpu/DOC_CORRECTIONS_log.md`**。

---

## 7. F8 状态升级（2026-10-09 追加，最终验收后）

**F8「大模型三层卸载压力往返」已由 P4 验收通过，全绿（exit 0 / ALL GREEN，主配置通过未触发回退）。**

- 证据：`aimdo-xpu/logs/acc_20261009-015303_*`；部署闸 `0.5.6.dev28` / `gc2cf98fb9`。
- 因此本清单及各处 F8 相关的**暂缓 / 未验证标记，均已升级为「已验收通过」**，覆盖文件：`交付说明.md`（新增「最终验收结论」节）、`实现说明.md`、以及证据档案 `EVIDENCE_optin_ab.md` / `VERIFY_e2e_hook_b580.md` / `VERIFY_p3_endtoend.md` / `VERIFY_task14_startup_unblocked.md` / `VERIFY_real_b580.md` 的**注解**（原文数值/sha/行号/日志未动）。
- 诚实边界与量化事实见 `交付说明.md`「最终验收结论」节。
