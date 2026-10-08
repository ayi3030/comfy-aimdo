# task #14 验证报告 — ComfyUI 启动阻塞已解除（真机 B580）

> ## ⚠️ 后续事实（2026-10-09 追加；**不改动下方原文**）
> 本文「启动阻塞已解除」结论**仍然成立**。唯 §3.3 称第二段门禁「属设计决策」为**当时**判断：
> - `_xpu_opt_in()` 第二段门禁已由 **`dev28` / `c2cf98fb9`**（非阻断守卫）修复；免 `--enable-dynamic-vram` 自动启用**生效**（待 P4 复验）。
> - **【2026-10-09 补记】免 flag 自动启用已由 P4 验收通过（全绿，证据 `logs/acc_20261009-015303_*`）**；上文「（待 P4 复验）」依此**升级为「已验收通过」**。
> - 下方**数值 / 日志一律保留**。归属：intel-xpu-adapter。

- 验证者：comfyui-python-engineer（顾守成）
- 时间：2026-10-09
- 主机：Windows 11 + Intel Arc B580；`python_embeded`（Python 3.13.14）；torch 2.14.0+xpu
- 被测：`E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\ComfyUI`（HEAD `b0b7435` = v0.39.0）
- 结论：**PASS —— 服务器横幅出现，启动阻塞解除**（`NameError: name 'refusal' is not defined` 已消失）

---

## 0. 核心结论

| 项 | 结果 | 证据 |
|---|---|---|
| 启动到服务器横幅 | **PASS** | `_e2e_startup.log:94` `[INFO] To see the GUI go to: http://127.0.0.1:8199` |
| `refusal` NameError | **已消失** | 全日志 `grep NameError|refusal|Traceback` → 0 命中 |
| 致命块是否仍在文件内 | **不在** | `grep -n refusal nodes.py` → 0 命中 |
| 不存在文件是否仍被引用 | **否** | `nodes_camera` 仅剩 `nodes_camera_trajectory.py`（该文件真实存在 10 KB） |
| 语法/编译 | **PASS** | `python_embeded\python.exe -m py_compile nodes.py` → exit 0 |
| aimdo XPU 后端 | 正常初始化 | `_e2e_startup.log:10/12` `published 1 SYCL queue(s)` / `backend ready (mode=native_hook)`；`:43` `DynamicVRAM support detected and enabled`；版本 `0.5.6.dev25` |

**修复现状说明**：task #14 要求的最小修复（删除 `refusal` 三行 + 删除 `nodes_camera.py`/`nodes_camera_angle.py` 两行）
**在验证时已存在于真机磁盘上**（现场有 `nodes.py.bak.20261009-012626`），应系并行任务 #16（owner: comfyui-python-engineer-2）
落地。本报告的角色是**独立验证「阻塞是否真正解除」**，未重复修改 `nodes.py`（避免与 #16 撞车）。

---

## 1. 验证方法（有界、可复现）

用 `aimdo-xpu/_e2e_startup_supervisor.py` 拉起真机 ComfyUI，有界等待服务器横幅，随后连同子进程树一并收掉：

```bash
E:/aiwork/.../python_embeded/python.exe -s ComfyUI/main.py \
    --enable-dynamic-vram --disable-auto-launch --port 8199
```

- 判定：出现 `To see the GUI go to` → PASS；出现 `Traceback`/`NameError` → FAIL；150s 超时 → UNKNOWN。
- 日志：`aimdo-xpu/_e2e_startup.log`（94 行）。
- 清理：`taskkill /F /T /PID`，无残留。

---

## 2. 与阻塞报告（`VERIFY_e2e_hook_b580.md` §4）的对照

| 阻塞报告断言 | 本次验证 |
|---|---|
| 崩在 `nodes.py:2430 if refusal is not None:` | 该行已不存在；启动不再经过任何 `refusal` 分支 |
| `--enable-dynamic-vram` 也崩 | 本次带该 flag，**正常启动到横幅** |
| 崩溃与 AIMDO/XPU 无关（隔离证明） | 一致：本次 aimdo XPU 路径全绿，且崩溃点确在 ComfyUI core |

---

## 3. 顺带发现（不阻塞启动，已回报 owner）

1. **`comfy/ldm/minimax/model.py:211` 疑似跨 revision 污染（建议 #16 复核，勿与运行时 revision 混用）**
   ```python
   -        return comfy.ops.linear_input_act(self.fc2, self.fc1(x), "swiglu")   # HEAD b0b7435 原样
   +        return self.fc2(self.fc1(x), input_act="swiglu")                       # 本地编辑
   ```
   本 revision 全仓库**没有任何 `def forward(..., input_act=...)`**（`grep` 证实），且该 `self.fc2(..., input_act=)` 写法
   **全仓库仅此一处**；其余 8 处（lightricks/wan/qwen_image21/minimax.vae/minimax_music/llama…）一律用
   `comfy.ops.linear_input_act(...)`（`comfy/ops.py:976` 确有该函数）。
   ⇒ 该改动很可能取自**更新 revision 的写法**，在本 revision 会在 MiniMax H3 MLP 前向时抛
   `TypeError: unexpected keyword argument 'input_act'`。**不影响启动**（仅在跑 MiniMax 模型时触发），但属功能性回归，建议 #16 决定回退。
2. **`custom_nodes/aimdo_diag.py` 导入失败**（`comfy_entrypoint ... did not return a ComfyExtension`，仅 WARNING）——与本适配无关，诊断脚本自身问题，非启动阻塞。
3. **`_xpu_opt_in()` 第二段门禁**（`VERIFY_e2e_hook_b580.md` §5）：不带 flag 时 DynamicVRAM 不会自动启用；与本次启动阻塞无关，属设计决策。

---

## 4. 未覆盖

- 含大模型加载 / 三层卸载压力往返的工作流级验收**未做**（`models/checkpoints`、`diffusion_models` 为空目录，无可用 checkpoint），与 #11 结论一致。
- 若 #16 后续**再修改 `nodes.py`**，本验证即失效，需重跑本脚本复验。
