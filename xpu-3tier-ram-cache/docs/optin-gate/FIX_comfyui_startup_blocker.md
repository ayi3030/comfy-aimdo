# ComfyUI 启动阻塞修复 + 跨 revision 污染清理

**结论**：启动阻塞已修复（orphan `refusal` 块移除）；另发现并修复了 **第二处**跨 revision 污染（`minimax/model.py` 的 `input_act=`，运行时必崩）；并修复了上游参考副本。所有改动文件现仅剩「守卫」这一处意图内差异。

运行时：`E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\ComfyUI`（HEAD `b0b7435`）
上游参考：`C:/Users/HE/WorkBuddy/2026-10-08-05-10-30/ComfyUI-upstream`（HEAD `f856877`）

---

## 0. 崩溃证据（原始日志）

`aimdo-xpu/logs/p3_flag_20261009-012256.log`：

```
10: published 1 SYCL queue(s) to the native backend
42: comfy-aimdo inited for GPU: Intel(R) Arc(TM) B580 Graphics (VRAM: 11875 MB)
43: DynamicVRAM support detected and enabled
...
88: nodes.py, line 2430, in init_external_custom_nodes
89:     if refusal is not None:
90:        ^^^^^^^
91: NameError: name 'refusal' is not defined
```

→ aimdo XPU 侧完全正常；崩溃点是本地补丁注入的 orphan `refusal` 块。**无任何日志出现 `To see the GUI go to`**，即进程从未启动到就绪。

---

## 1. 备份清单

| 文件 | 备份路径 |
|---|---|
| 运行时 `nodes.py` | `E:\aiwork\...\ComfyUI\nodes.py.bak.20261009-012626` |
| 运行时 `comfy/ldm/minimax/model.py` | `E:\aiwork\...\ComfyUI\comfy\ldm\minimax\model.py.bak.20261009-012959` |
| 上游 `nodes.py` | `C:/Users/HE/WorkBuddy/2026-10-08-05-10-30/ComfyUI-upstream/nodes.py.bak.20261009-012829` |

（`nodes.py` / `model.py` 同时可由各自 git HEAD 还原。）

---

## 2. 移除 nodes.py 的两处注入垃圾（保留 NaN 守卫）

### 2.1 移除 orphan `refusal` 块（启动崩溃根因）

`git diff nodes.py` 中 `@@ -2385,6 +2427,10 @@` 的 4 行（≈2429-2432）已删除：
```python
-            if refusal is not None:
-                logging.warning(refusal)
-                continue
-
```
证据：运行时 HEAD 的 `nodes.py` **零** `refusal` 命中（`git show HEAD:nodes.py | grep refusal` 无输出）；该块仅由本地补丁引入，且 `refusal` 全程无赋值——来自上游 `f856877` 的 `nodes.py`（依赖 `app/governance.py`），而**运行时没有 `app/governance.py`**。

### 2.2 移除不存在的 `nodes_camera*` 内置节点条目

`git diff nodes.py` 中 `@@ -2544,6 +2590,8 @@` 的两行已删除：
```python
-        "nodes_camera.py",
-        "nodes_camera_angle.py",
```
证据：`comfy_extras/nodes_camera.py` 与 `nodes_camera_angle.py` 在运行时**均不存在**（`ls` 报 No such file）。保留的 `nodes_camera_trajectory.py`（:2534）**合法**——它在运行时 HEAD 的内置列表里，且文件真实存在。

### 2.3 最终 `git diff nodes.py`（仅守卫）

```diff
diff --git a/nodes.py b/nodes.py
index 12f152c..09f4e2e 100644
--- a/nodes.py
+++ b/nodes.py
@@ -317,6 +317,44 @@ class ConditioningSetTimestepRange:
         c = node_helpers.conditioning_set_values(conditioning, {"start_percent": start, "end_percent": end})
         return (c, )
 
+def _video_vae_identity(vae):
+    """给报错信息用的 VAE 标识，取不到文件名就退回类名。"""
+    patcher = getattr(vae, "patcher", None)
+    for attr in ("filename", "name"):
+        value = getattr(patcher, attr, None)
+        if isinstance(value, str) and value:
+            return value
+    return type(vae).__name__
+
+
+def _reject_non_finite_video(images, vae, latent, tiled=False):
+    """视频 VAE 解码结果含 NaN/Inf 时抛出可定位的异常。
+    ...（中文说明，略）...
+    """
+    if not bool(torch.isfinite(images).all()):
+        n_nan = int(torch.isnan(images).sum())
+        n_inf = int(torch.isinf(images).sum())
+        raise ValueError(
+            "Video VAE decode produced non-finite samples (NaN={}, Inf={} of {}); "
+            ...
+        )
+
+
 class VAEDecode:
@@ -342,6 +380,7 @@ class VAEDecode:
         if len(images.shape) == 5: #Combine batches
             images = images.reshape(-1, images.shape[-3], images.shape[-2], images.shape[-1])
+        _reject_non_finite_video(images, vae, latent)
         return (images, )
 
 class VAEDecodeTiled:
@@ -379,6 +418,9 @@ class VAEDecodeTiled:
         if len(images.shape) == 5: #Combine batches
             images = images.reshape(-1, images.shape[-3], images.shape[-2], images.shape[-1])
+        # 与 VAEDecode 同理：tiled 路径的NaN 同样会在写出时被 .byte() 静默转成纯黑。
+        # H3 官方工作流走的是 VAEDecode，但本节点是公开入口，漏掉它等于留了个后门。
+        _reject_non_finite_video(images, vae, latent, tiled=True)
         return (images, )
 
 class VAEEncode:
```
统计：`nodes.py | 42 ++++++`，**无 refusal、无 nodes_camera**。

---

## 3. 验证结果（pyflakes + py_compile）

在**隔离 venv** `C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/.pyflakes-venv`（由系统 Python 3.13 创建，**未触碰** python_embeded 的 site-packages）中安装 `pyflakes 4.0.3`。

| 文件 | pyflakes | py_compile |
|---|---|---|
| 运行时 `nodes.py` | rc=0，0 findings | OK |
| 运行时 `comfy/ldm/minimax/model.py` | rc=0，0 findings | OK |
| 运行时 `comfy_extras/nodes_audio.py` | rc=0，0 findings | OK |
| 运行时 `main.py`（对照） | rc=0，0 findings | OK |
| 上游 `nodes.py` | rc=0，0 findings | OK |

跨文件 F821 专项：**NO UNDEFINED NAMES**。
> 注意：pyflakes **不足以**发现 `input_act=` 这类缺陷——它是调用签名错误（运行时 `TypeError`），不是未定义名。此类需靠 API 审计（见 §4）。

---

## 4. 另外两个文件的审计（关键发现）

### 4.1 `comfy/ldm/minimax/model.py` —— ⚠️ 发现并修复第二处跨 revision 污染

运行时的 `model.py` 补丁含 **2 处**改动，其中 hunk 1 **不是守卫**：
```diff
@@ -208,7 +208,7 @@ class MLP(nn.Module):
     def forward(self, x):
-        return comfy.ops.linear_input_act(self.fc2, self.fc1(x), "swiglu")
+        return self.fc2(self.fc1(x), input_act="swiglu")
```

**判定：外来、且在本 revision 必崩。** 三重证据：

1. **API 不存在**：运行时 `comfy/ops.py` 的 `Linear.forward(self, *args, **kwargs)` → 基类 `torch.nn.Linear.forward(input)`，**不接受 `input_act`**。本机实测：
   `TYPEERROR on input_act= : Linear.forward() got an unexpected keyword argument 'input_act'`
   而 `o.linear_input_act(l, x, "swiglu")` 可正常进入计算。
2. **本 revision 的正确 API 是 `linear_input_act`**：它在运行时 `comfy/ops.py:976` 定义，并在 `lightricks/minimax.vae/qwen_image21/wan/llama` 等多处按 `linear_input_act(linear, x, act)` 使用。
3. **来自上游 revision**：两个 revision 的 `model.py` **HEAD 基线本身就不同**——
   - 运行时 `b0b7435` HEAD: `return comfy.ops.linear_input_act(self.fc2, self.fc1(x), "swiglu")`
   - 上游 `f856877` HEAD: `return self.fc2(self.fc1(x), input_act="swiglu")`
   上游 `comfy/ops.py:591` 确有 `Linear.forward(self, input, input_act=None, ...)`，故其写法在**上游**正确，但在**运行时**错误。

**处理**：已把运行时该行**还原为运行时 HEAD 原式** `comfy.ops.linear_input_act(self.fc2, self.fc1(x), "swiglu")`。
还原后 `model.py | 30 ++++++`，只剩 `_reject_non_finite_minimax_out` 守卫（与上游 stat 一致）；pyflakes 0、py_compile OK。

> H3 视频每层 MLP 都走 `MLP.forward`，此 hunk 若保留，ComfyUI 启动后**一旦开始生成即 `TypeError`**。属"启动能过、生成必崩"的隐性阻塞。

### 4.2 `comfy_extras/nodes_audio.py` —— ✅ 干净

`git diff` 仅含 `_vae_identity` + `_reject_non_finite_audio` 两个守卫块及其在 `vae_decode_audio` 的调用，无任何外来改动。两 revision 的 `nodes_audio.py` HEAD 基线**逐字节相同**。pyflakes 0、py_compile OK。`torch` 已导入（:3）。

### 4.3 运行时四文件最终状态

```
 comfy/ldm/minimax/model.py  | 30 ++++   (仅守卫)
 comfy_extras/nodes_audio.py | 40 ++++   (仅守卫)
 main.py                     |  4 ++++  (Intel XPU 门禁，保留)
 nodes.py                    | 42 ++++   (仅守卫)
```
四个文件都**只剩意图内改动**，无任何 `refusal` / `nodes_camera` / `input_act=` 残留。

---

## 5. 上游参考副本修复（`ComfyUI-upstream/nodes.py`）

上游工作副本被污染得**比预期更多**：不只 `refusal` 赋值行被删，`from app import governance`（:42）与 `load_custom_node` 里的 `governance.pack_module_spec` 分支（:2266）**同样被删**。若只按原指令补回赋值行，`governance` 仍是无定义名——**上游会带病**。

故按"恢复上游自身一致性"的目标，把三条 governance 相关行**全部还原**（上游确有 `app/governance.py`，`pack_refusal` 定义于 :128）：

- `from app import governance`（导入）
- `spec_from_file_location = governance.pack_module_spec if module_parent == "custom_nodes" else importlib.util.spec_from_file_location`
- `refusal = governance.pack_refusal(module_path)`（赋值行）

修复后上游 `git diff nodes.py` = **仅守卫（42 insertions）**，与 f856877 HEAD 一致 + 守卫；pyflakes 0、py_compile OK。

---

## 6. ⚠️ 遗留风险：`sync_to_runtime.sh` 的全量覆盖在跨 revision 下不安全

`sync_to_runtime.sh` 对三份文件是**整文件 `cp`（上游 → 运行时）**，而非仅同步守卫。由于两侧 revision 不同，整文件覆盖会**把运行时不兼容的代码灌进来**：

| 文件 | 两 revision HEAD 基线 | 整文件同步后果 | 是否安全 |
|---|---|---|---|
| `comfy_extras/nodes_audio.py` | **逐字节相同** | 覆盖等价 | ✅ 安全 |
| `nodes.py` | 不同（上游含 governance） | 运行时被写入 `from app import governance` → 运行时无 `app/governance.py` → **ImportError，无法启动** | ❌ 不安全 |
| `comfy/ldm/minimax/model.py` | 不同（:211 `linear_input_act` vs `input_act=`） | 运行时被写入 `input_act=` → **TypeError** | ❌ 不安全 |

**建议**：在这两个 revision 对齐之前，`sync_to_runtime.sh` 对 `nodes.py` / `model.py` **只可 `--check`，不可写入**；或把同步改为"按符号/按 hunk 的最小同步"而非整文件覆盖。（本项超出本次授权范围，仅上报，未改脚本。）

---

## 7. 结论：ComfyUI 现在应当可以启动

- 启动崩溃点（`nodes.py:2430` orphan `refusal`）**已物理移除**；日志中该行不再存在。
- 四文件 pyflakes 0 / py_compile OK。
- aimdo XPU 与 Intel 门禁均未受影响（`main.py` 的门禁补丁 +4 行保持原样）。
- ⚠️ 生成阶段另有一处必崩（`model.py` 的 `input_act=`）**已一并修复**，否则 H3 生成会 `TypeError`。

**未做**：未启动 ComfyUI（交由 runtime-verifier 复跑 P3）；未改 `main.py` / `comfy_aimdo` / `site-packages`。
