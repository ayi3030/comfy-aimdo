# 独立审查报告：XPU 三层存储（RAM 中间缓存）改动

> 审查人：xpu-compat-reviewer（兼容性审查官）
> 日期：2026-10-09
> 待审对象：`comfyui-3tier-ram-cache.patch`（4 文件 / 11 处）
> 实现说明：`IMPL_3tier_ram_cache.md` · 方案：`PHASE2_3tier_design.md`
> 源码根：`E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/ComfyUI/`
> 约束遵守：**未修改**任何源码；**未运行** ComfyUI；**未改** `site-packages`；补丁重放全部在临时目录 `_replay/` 完成。

---

## 0. 总体判定（先给结论）

**判定：`可进入真机验证`（附 2 项必须在合并/出货前落实的加固项，均不阻塞 B580 真机跑）**

- **R1（成败第一判据）= 是**：XPU 分支的 `free_pins` 链路**确实会**最终调到 `ModelPatcherDynamic.partially_unload_ram`，从而扣减 `TOTAL_PIN_CACHE_MEMORY`；预算计数**可收敛**，RAM 层**不会**退化为死代码。
- 补丁**可重放**：`git apply -p0` 与 `patch -p0 --binary` 在干净副本上均**逐字节还原**工作区（4/4 md5 identical）。
- 4 文件语法 OK，改后 md5 与作者声明一致，工作区未被污染。

**必须在合并/出货前落实的最小修复清单（详见 R3 / X2）：**
1. **X2（代码，1 行）**：`xpu_ram_cache_enabled()` 存在混合机（Intel iGPU + NVIDIA/AMD）CUDA 模式下误触的漏洞，违反“CUDA 零变化”承诺。→ 改用“当前设备类型”判定。
2. **R3（文档，非代码）**：`IMPL_3tier_ram_cache.md` §6.4 关于“4 文件为纯 LF”的**前提是错的**（实为纯 CRLF），结论（必须用 `git apply` 或 `patch --binary`）仍正确。必须更正，否则 Phase 5 可能被误导去“转 LF”。

---

## 0.1 证据基线（可复现）

| 文件 | 工作区 md5（改后） | 与作者声明 | 行尾（字节级） |
|---|---|---|---|
| `comfy/model_management.py` | `6bd2186e174d3f4c2dfbce29dc8323a8` | ✅ 一致 | PURE_CRLF（CRLF=2311, bareLF=0） |
| `comfy/pinned_memory.py` | `c629da9c2089c7c75d3b53386f49fdc9` | ✅ 一致 | PURE_CRLF（CRLF=139, bareLF=0） |
| `comfy/model_patcher.py` | `d523b099933dad858e3a9e73a567c730` | ✅ 一致 | PURE_CRLF（CRLF=2190, bareLF=0） |
| `comfy/ops.py` | `96090a01b13e1b81907152324b6430a0` | ✅ 一致 | PURE_CRLF（CRLF=1825, bareLF=0） |

- 语法检查（`ast.parse`，不写 `__pycache__`）：4/4 `SYNTAX_OK`。
- 工作区无 `.rej`/`.orig` 残留；审查前后 md5 完全一致（证明审查过程零副作用）。

---

## R1（致命）预算计数能否收敛 —— **结论：收敛（是）**

**结论：`TOTAL_PIN_CACHE_MEMORY` 会增也会减。XPU 的 pin（`registered=False`）在 `free_pins` 链中不被过滤，会真正被销毁并扣账。RAM 层不会静默失效。**

### 完整调用链（逐跳，含 `model_management.py` 行号）

`ensure_pin_registerable`（XPU 分支，L787-790）：
```python
shortfall = total_pin_memory_used() + size - MAX_PINNED_MEMORY   # L787
if shortfall <= 0: return True                                    # L788-789
return free_pins(shortfall + PIN_PRESSURE_HYSTERESIS, evict_active=evict_active) >= shortfall  # L790
```

1. `free_pins(size, evict_active, loaded=False)` — **L728-732**
   ```python
   for subsets, current_prompt, active in pin_eviction_tiers(loaded, evict_active):   # L730
       freed += free_model_pins(size - freed, subsets, current_prompt, active)        # L731  ← registrations 用默认值
   ```
2. `pin_eviction_tiers` — **L693-709**：首档即 `(FAST_PIN_SUBSETS, False, False)`（L695）；`evict_active=True`（`ensure_pin_registerable` 默认）时还会追加 `(FAST_PIN_SUBSETS, *, True)` 等档（L704-707）→ **`weights-fast`/`patches-fast` 可被选中**。
3. `free_model_pins(size, subsets, current_prompt, active, registrations=False)` — **L674-691**：`registrations` 默认 **False** → **L681-682** 走 `else` 分支：
   ```python
   else:
       freed = model.partially_unload_ram(size, subsets=subsets)   # L682
   ```
4. `ModelPatcherDynamic.partially_unload_ram` — **`model_patcher.py:2107`**：**不检查 `registered`**，直接 `stack.pop()` → `hostbuf.truncate(offset, do_unregister=registered)`（L2121）→ **L2126-2129**：
   ```python
   elif not comfy.model_management.HOST_PIN_REGISTRATION_SUPPORTED:      # XPU 上为 True
       comfy.model_management.TOTAL_PIN_CACHE_MEMORY = max(0, ...- size) # 扣减 ✅
   ```

**⇒ 明确回答任务关键问句：是。** `free_pins → free_model_pins(registrations=False) → partially_unload_ram → 扣 TOTAL_PIN_CACHE_MEMORY`。XPU 的 `registered=False` pin **不会**被过滤（过滤只发生在 `unregister_inactive_pins`，而该函数**不在** `free_pins` 链上）。因此计数**只增不减的担忧不成立**。

### 记账闭合性核对（增/减两侧口径一致）
- 增：`pinned_memory.py:134` `TOTAL_PIN_CACHE_MEMORY += size`，其中 `size = hostbuf_to_tensor(...)[offset:offset+size]` 的长度 = `dest_size`（`vram_aligned_size`），`pin.nbytes == size`。
- 减：`partially_unload_ram` L2116 `size = pin.numel() * pin.element_size()` = 同一 `size`。两侧口径闭合 ✅。
- `_steal_pin`（`pinned_memory.py:17-46`）：**同尺寸桶**内交换（`buckets[size]`），字节净变化 0，**正确不动任何计数器** ✅。

### `registration_eviction_tiers`（L711-726）对 `*-fast` 子池的处理（任务要求顺带核对）
- `FAST_PIN_SUBSETS` 的档位第 4 元素恒为 `registrations=False`（L714/716/721/723），即 `*-fast` 的 pin 走 **销毁**（`partially_unload_ram`）而非解注册；非 fast 子池（`weights/patches/*-loaded`）为 `registrations=True`（走 `unregister_inactive_pins`）。
- 该函数只服务 CUDA 分支的 `free_registrations`；**XPU 分支走的是 `free_pins`，完全不经过本函数**。故对本改动无副作用，逻辑自洽 ✅。

**风险等级：通过。** 修复建议：无需改代码。建议在 Phase 5 加**运行时探针**证明收敛（见 §自检清单/S3）：记录并对比 `TOTAL_PIN_CACHE_MEMORY` 与 `loaded_ram_size()` 是否随压力上升到预算附近后趋于平稳且出现回落。

---

## R2（高）CUDA / ROCm / CPU 行为零变化 —— **结论：基本通过，但有 1 处混合机漏洞（见 X2）**

逐条列出补丁引入的每个新判断，给出在非 XPU 上的等价值与依据：

| 新增判断 | CUDA/ROCm 上取值 | CPU-only 上取值 | 与改前等价性 |
|---|---|---|---|
| `get_pin` 早退新增 `not HOST_PIN_REGISTRATION_SUPPORTED`（`pinned_memory.py:53`） | `not True` = False → 不触发，条件不变 | True，但该分支前 `pin is None` 已必真（见下）→ 无可观测变化 | ✅ 等价 |
| `ensure_pin_registerable` 顶层 `if MAX_PINNED_MEMORY <= 0: return False`（`model_management.py:777`） | MAX>0 → 不走 | MAX=-1 → 走 | ✅ 等价（见下方“副作用差异”核对） |
| `ensure_pin_registerable` 新增 `if HOST_PIN_REGISTRATION_SUPPORTED:` 分支（L780） | True → 原 `free_registrations(TOTAL_PINNED_MEMORY+size-MAX)` **逐字等价** | False → 但 MAX<=0 已在上一步返回 | ✅ 等价 |
| `pinned_memory.py:109` `if HOST_PIN_REGISTRATION_SUPPORTED:` 包住注册段 | True → 执行体与改前逐字相同 | False → 跳过；但 MAX<=0 时 `pin_memory` 更早 `_steal_pin` | ✅ 等价 |
| `pinned_memory.py:127` 记账分流 | True → `registered=True; TOTAL_PINNED_MEMORY += size`（=改前） | False 分支不达 | ✅ 等价 |
| `model_patcher.py:2126` `elif not HOST_PIN_REGISTRATION_SUPPORTED:` | `not True`=False → 语句被跳过 | Max<=0 时不达；即便达也不影响 CUDA 计数 | ✅ 等价 |
| `ops.py:233` `or xpu_ram_cache_enabled()` | 见下（**X2 漏洞**） | False（`is_intel_xpu()` False） | ⚠️ 混合机例外 |
| `HOST_PIN_REGISTRATION_SUPPORTED = is_nvidia() or is_amd()`（L1640） | True | False | 新增常量，见下 |

### 逐项证据与说明

**(a) `get_pin` 早退在 CPU-only 上的行为变化 —— 结论：不可观测，非回归。**
`get_pin` 首条件是 `pin is None or ...`（`pinned_memory.py:52`）。`pin` 只有在 `pin_memory` 成功建 pin 后才非 None，而 CPU-only 下 `MAX_PINNED_MEMORY` 恒为 -1（L1634，仅 `is_nvidia()/is_amd()` 与新 `is_intel_xpu()` 分支会赋值），`ensure_pin_registerable` 恒 False → `pin_memory` 走 `_steal_pin` → pin 恒 None。**故 CPU-only 下 `pin is None` 恒真，原代码根本走不到 `cudaHostRegister`**；新增项不改变可观测行为。⇒ 既非修 bug 也非引入回归，是**无操作**。
> 注：与作者在 §0.2/设计文档里“原代码在 CPU-only 会走到 cudaHostRegister”的表述不同——静态走查证明其不会到达。这不影响改动安全性，但建议更正文档，以免误导。

**(b) `ensure_pin_registerable` 新增 `MAX_PINNED_MEMORY <= 0` 提前返回是否有副作用差异 —— 无。**
原路径 `return free_registrations(TOTAL_PINNED_MEMORY + size - MAX_PINNED_MEMORY, ...)`；`free_registrations` 首行即 `if MAX_PINNED_MEMORY <= 0: return False`（`model_management.py:766-767`），**在进入任何驱逐逻辑前就返回**，无驱逐副作用。新顶层判断与之**结果与副作用完全一致** ✅。

**(c) `host_register_pin` 在 `HOST_PIN_REGISTRATION_SUPPORTED=True` 时逐字等价 —— 是。**
`model_management.py:1736`：`return torch.cuda.cudart().cudaHostRegister(ptr, size, 1) == 0`。
各原调用点：
- `pin_memory` 原 `if torch.cuda.cudart().cudaHostRegister(ptr, size, 1) == 0:` → 新 `if host_register_pin(ptr, size):` ✅
- `get_pin` 原 `... != 0` → 新 `if not host_register_pin(...)`:（`not(a==0)` ≡ `a!=0`）✅
- `host_unregister_pin`（L1742）`== 0`；`model_patcher.py:2095` 原 `!= 0` → 新 `if not host_unregister_pin(...)` ✅
⇒ **逐字等价**，无侧信道（无额外异常吞并、无参数改写）。

**(d) `ops.py` 新增 `or xpu_ram_cache_enabled()` 在 CUDA/ROCm/CPU 是否恒定 False —— 纯机为是，混合机为否（漏洞，见 X2）。**
`xpu_ram_cache_enabled()` = `is_intel_xpu() and MAX_PINNED_MEMORY > 0`（L1744-1750）。
`is_intel_xpu()` = `cpu_state == CPUState.GPU and xpu_available`（L161-167）。
- 纯 NVIDIA/AMD/CPU：`xpu_available=False` → False ✅。
- **混合机（Intel iGPU/Arc + NVIDIA/AMD）在 CUDA 模式下**：`xpu_available=True` 且 `cpu_state==GPU` → `is_intel_xpu()=True`；`MAX_PINNED_MEMORY` 由 `is_nvidia()` 分支赋为 `ram*0.40>0` → **`xpu_ram_cache_enabled()=True`** → `ops.py:232` 的 `fast_disk` 直通分支在 **CUDA 上被触发**。这是**真实的行为变化**（见 X2）。

**(e) `HOST_PIN_REGISTRATION_SUPPORTED` 求值时机与依赖 —— 安全。**
`is_intel_xpu`(L161) / `is_nvidia`(L426) / `is_amd`(L433) 均在 L1640 之前定义；`cpu_state` 在 L158 附近已完成初始化。L1640 在模块导入期求值一次，读到的是确定的 `cpu_state`，只依赖 `torch.version.cuda/hip`（常量）→ **无延迟初始化竞态** ✅。三函数均只读全局，无副作用。

**风险等级：有风险（仅 X2 项）。** CUDA/ROCm/CPU 纯机路径：**通过**。

---

## R3（高）行尾与补丁可重放性 —— **结论：可重放通过；但作者“纯 LF”前提错误（必须更正文档）**

### 字节级统计（精确分类，非 `grep '$'` 锚点）
方法：Python 逐字节 `\r\n` / 裸 `\n` / 裸 `\r` 计数（脚本 `aimdo-xpu/_review_eol.py`）。

```
=== working files (post-patch) ===
model_management.py   size=82602  CRLF=2311 bareLF=0 bareCR=0  -> PURE_CRLF
pinned_memory.py      size=5886   CRLF=139  bareLF=0 bareCR=0  -> PURE_CRLF
model_patcher.py      size=105536 CRLF=2190 bareLF=0 bareCR=0  -> PURE_CRLF
ops.py                size=88037  CRLF=1825 bareLF=0 bareCR=0  -> PURE_CRLF
=== backups (pre-patch) ===
model_management.py.bak...   CRLF=2208 bareLF=0 -> PURE_CRLF
pinned_memory.py.bak...      CRLF=126  bareLF=0 -> PURE_CRLF
model_patcher.py.bak...      CRLF=2186 bareLF=0 -> PURE_CRLF
ops.py.bak...                CRLF=1820 bareLF=0 -> PURE_CRLF
=== patch file ===
comfyui-3tier-ram-cache.patch  CRLF=277 bareLF=22 bareCR=0 -> MIXED
=== reference main.py (已知 CRLF) ===
main.py  CRLF=624 bareLF=0 -> PURE_CRLF
```

**关键事实：**
1. **4 个源文件（改前/改后）都是 PURE_CRLF，不是纯 LF。** 作者 §6.4 的“文件本身为纯 LF”是**错误前提**。
2. 补丁文件是 **MIXED**：`bareLF=22` 恰好全部是 diff **元数据行**（`--- `/`+++ `/`@@ ... @@`，共 22 行），**所有内容行（上下文/`+`/`-`）全部是 CRLF**（277 行）。这正是“把 CRLF 源文件 diff 出来”的产物特征。

### 干净副本重放（临时目录 `_replay/`，从备份还原）
```
A) git apply -p0 --check        -> GIT_APPLY_CHECK_OK
A2) git apply -p0 (real)        -> GIT_APPLY_OK
B) patch -p0 --binary           -> PATCH_BINARY_OK（4 文件全 patching）
C) patch -p0 (无 --binary)      -> 全部 hunk FAILED (different line endings)，exit=1
```
逐字节比对（对工作区）：
```
                    git             gnupatch_binary   gnupatch_plain(C)
model_management.py IDENTICAL       IDENTICAL         DIFFER(76815 vs 82602)
pinned_memory.py    IDENTICAL       IDENTICAL         DIFFER(4813 vs 5886)
model_patcher.py    IDENTICAL       IDENTICAL         DIFFER(105114 vs 105536)
ops.py              IDENTICAL       IDENTICAL         DIFFER(87612 vs 88037)
```
**⇒ `git apply -p0` 与 `patch -p0 --binary` 均逐字节还原工作区（4/4 IDENTICAL）；无 `--binary` 的 `patch` 0/N hunk 全失败。**

### 对“为什么必须 --binary”的正确解释（供文档更正）
不是“文件是 LF 所以需要 --binary”，恰恰相反：**文件与补丁内容行都是 CRLF**。MSYS 版 GNU `patch 2.7.6` 在无 `--binary` 时对 CR 的规范化行为与 CRLF 补丁不兼容，故误报 `different line endings` 并拒绝应用。

### Phase 5 部署应使用的打补丁方式（明确）
在 ComfyUI 根目录下，**二选一**：
```
git apply -p0 comfyui-3tier-ram-cache.patch      # 首选
patch -p0 --binary -i comfyui-3tier-ram-cache.patch
```
**禁止**：无 `--binary` 的 GNU `patch`；**禁止**任何“把文件转成 LF 再应用”的操作；补丁文件必须以二进制方式保存/传输，**禁止**用会重写行尾的编辑器另存。

**风险等级：有风险（文档前提错误），实现本身通过。**
**修复建议（可执行）**：
1. 更正 `IMPL_3tier_ram_cache.md` §6.4：把“文件本身为纯 LF”改为“4 文件与补丁内容行均为 CRLF；MSYS GNU patch 无 `--binary` 时因 CRLF 不兼容而误报，故须 `git apply` 或 `patch --binary`”。
2. 在 Phase 5 部署清单中固化“应用后校验 md5 必须等于上表 4 个值”，作为打补丁成功的唯一判据。
3. 交付补丁时附 `.sha256`，并在应用脚本中 `git apply --check` 前置。

---

## R4（中）无裸 CUDA 调用残留 —— **结论：通过**

全仓（`--include=*.py`，排除 `.bak`）检索：
```
torch.cuda.cudart(:  仅 model_management.py:1736 / :1742 两处代码（其余 2 处为注释）
cudaHostRegister / cudaHostUnregister:  仅 :1736 / :1742 两处代码（其余为注释）
```
`pinned_memory.py` / `model_patcher.py` / `ops.py` 已**无**任何 `cudaHostRegister/Unregister` 代码调用，本次改动路径上也不再有裸 `torch.cuda.cudart()`。

**同类调用（会抛 AssertionError 的 `torch.cuda.*`）是否出现在本次改动路径上 —— 否。**
本次改动路径涉及的函数（`get_pin` / `pinned_memory.pin_memory` / `pin_memory`(tensor) / `unpin_memory` / `unregister_inactive_pins` / `partially_unload_ram` / `handle_pin` / `ensure_pin_registerable` / `free_pins` 链 / `cast_to_gathered` / `read_tensor_file_slice_into`）中，只有 2 处 `torch.cuda.cudart()`（已在 helper 内守卫）。其余文件中大量 `torch.cuda.*`（`synchronize`/`memory_stats`/`get_device_properties`…）均为**改前既有**且**不在本次改动路径**上（多位于 `is_nvidia()` 分支或 `torch.cuda.is_available()` 守卫之后），非本次引入。

**风险等级：通过。** 修复建议：无。可选增强：给两个 helper 加 `logging` 级别的一次性 debug 日志（仅在非注册后端 emit），便于真机核证“XPU 上确实走守卫”。

---

## R5（中）快盘子池是否可驱逐 —— **结论：通过（可被驱逐）**

XPU 因未改 `fast_disk`（`ops.py:188/252`），pin 落在 `weights-fast`/`patches-fast`。核对驱逐分层：

- `pin_eviction_tiers`（`model_management.py:693-709`）：**首档即 `FAST_PIN_SUBSETS`**（L695 `(FAST_PIN_SUBSETS, False, False)`）；`evict_active=True` 时再追加 `(FAST_PIN_SUBSETS, False, True)`、`(FAST_PIN_SUBSETS, True, True)`（L704-707）→ 活动模型的 fast pin 也可被驱逐。
- `partially_unload_ram` 默认 `subsets` 明确含 `"weights-fast","patches-fast"`（`model_patcher.py:2107`），且 `free_model_pins` 会**显式传入** tier 的 `subsets`（L682），故 fast 子池被真正遍历。
- `models_for_pin_eviction`（L664-672）以 `pin_state["active"]/["current_prompt"]` 过滤，`active=None` 档位表示“任意”，覆盖活动模型。

⇒ `*-fast` 子池在上述 `free_pins` 链中**可被选中驱逐**，R1 不因 R5 失败。

**风险等级：通过。** 修复建议：无（副作用“XPU 的 RAM pin 属最先驱逐层”是方案 §3.2 的已知取舍）。

---

## R6（中）RAM 缓存是否真会被填充并命中 —— **结论：填充机制成立；命中走 torch 拷贝；C 侧 H2D 需实机验证**

### 走查结果（`ops.py:228-240` → `cast_to_gathered` → `read_tensor_file_slice_into`）

**首次 fault（RAM 未命中）：pin 会被填充。**
`handle_pin` pin 为 None → 建 pin（`pin_memory`→`get_pin`）→ `cast_maybe_lowvram_patch(source, pin, offload_stream, xfer_dest2=dest)`（`ops.py:240`）→ `cast_to_gathered(source, r=pin, r2=dest)`（`model_management.py:1569`）→ 对每个权重调 `read_tensor_file_slice_into(weight, dest_view=pin_view, destination2=dest_view)`。
- `pin.untyped_storage()._comfy_hostbuf` 已在 `pinned_memory.py:108` 挂上 → 命中 `memory_management.py:65` 的 hostbuf 分支：
  ```python
  hostbuf.read_file_slice(file_obj, info.offset, info.size,
                          offset=destination.data_ptr()-hostbuf.get_raw_address(),
                          stream=stream_ptr, device_ptr=device_ptr, device=destination2.device.index)  # L70-74
  ```
  ⇒ C 侧一次完成 **文件→hostbuf(RAM) 且 →device(VRAM)**。
- **即便该快路径因守卫条件（`memory_management.py:45-51`）返回 False**，回退分支 `dest_view.copy_(tensor)`（L1589）是 hostbuf 视图 ← mmap 张量的 **CPU→CPU 拷贝**，**同样会填充 hostbuf**；随后 `dest2_view.copy_(dest_view)`（L1591）再 hostbuf→VRAM。**故 pin 的填充有两条独立保障。**

**二次 fault（RAM 命中）：RAM→VRAM 由 torch 拷贝执行。**
`get_pin` 在 XPU 上早退返回 pin（`pinned_memory.py:52-57`）→ `ops.py:197 xfer_source=[pin]` → `handle_pin` 走 pin 非 None 分支 → `cast_maybe_lowvram_patch([pin], dest, offload_stream)`（L230）→ `cast_to_gathered([pin], r=dest, r2=None)` → `read_tensor_file_slice_into(pin, dest, destination2=None)`：line 40 交换为 `destination2=dest, destination=None` → line 57 需 `pin.untyped_storage()._comfy_tensor_file_slice`（pin **没有**该属性）→ 返回 False → 回退 `dest_view.copy_(pin)` = **普通 H2D torch 拷贝（pageable）**。
⇒ **不依赖 `HostBuffer.read_file_slice(device_ptr=...)`**，XPU 上走 torch 的 `.copy_()`，设备无关、可靠。

### 唯一“最硬前提”（需实机验证）
**首次 fault 的 hostbuf 快路径会 `return True` 并 `continue`，跳过回退拷贝。** 若 C 侧 `hostbuf_read_file_slice(..., device_ptr=X, device=idx)` 在 XPU 上**名义返回 True 却未真正写入显存**（桩/半实现），则**不会**被回退分支纠正 → **静默错误结果**（不是崩溃）。这是本次改动最需要真机用**逐字节比对**证伪的点（见 S4）。
> 依据：`host_buffer.py:104-110` 该函数返回 bool，失败才 raise；C 侧对 XPU 的 H2D 实现是本 fork 的已知薄弱面（fork 中 `xpu_host_register()` 本就为 no-op 桩）。

### 最小 harness（不运行 ComfyUI 也能证明“pin 被填充”）
**可行性：可行**——hostbuf 的填充在**纯 CPU 腿**即可证明（`read_file_slice` 的 `device_ptr=0` 分支即“纯宿主拷贝”，签名见 `host_buffer.py:104`）。建议 Phase 5 接受以下两种之一：

**H1（纯 `comfy_aimdo`，零 ComfyUI 依赖，可立即跑）：**
```python
# 只用 site-packages/comfy_aimdo，不 import comfy.*，不启动 ComfyUI
import os, tempfile, comfy_aimdo.host_buffer as hb
payload = bytes(range(256)) * 4096                      # 1 MiB
path = os.path.join(tempfile.gettempdir(), "aimdo_probe.bin")
open(path, "wb").write(payload)
b = hb.HostBuffer(0, 8*1024*1024, 16*1024*1024)         # 模拟 pinned_memory 的建法
b.extend(len(payload), register=False)
off = 0                                                 # 对应 offset=data_ptr-raw
with open(path, "rb") as f:
    b.read_file_slice(f, 0, len(payload), offset=off, stream=0, device_ptr=0, device=-1)  # 纯宿主腿
# 读回校验
import ctypes
buf = (ctypes.c_uint8 * b.size).from_address(b.get_raw_address())
got = bytes(buf[:len(payload)])
print("HOSTBUF_FILLED_OK" if got == payload else "HOSTBUF_FILLED_FAIL")
```
预期输出 `HOSTBUF_FILLED_OK`。若得 FAIL → “文件→RAM” 腿在 XPU 构建下也不成立，**直接阻塞**。

**H2（H2D 腿，需 XPU，仍是独立脚本、不启动 ComfyUI）：** 建一个小的 `torch.empty(..., device="xpu")` 作为 `device_ptr`/`device`，重复上面调用后 `torch.xpu.synchronize()`，与 `payload` 逐字节比对，预期一致。

**风险等级：有风险（需实机验证，含“静默错误”可能）。** 修复建议（若 H2 证伪）：
- 在 `read_tensor_file_slice_into` 的 hostbuf 分支加“H2D 结果校验开关”：当 `device != -1` 时，若平台为 XPU 且 C 侧能力未证，则**强制走回退分支**（把 `read_tensor_file_slice_into` 的 hostbuf 分支改为在 XPU 上先做宿主填充、再显式 `dest2_view.copy_()`），以牺牲少量性能换取确定性。
- 或：在 `host_register_pin`/新增 `host_h2d_supported()` 里增加一个 XPU 能力探测（首次调用做一次小 buffer 往返校验），探测失败即降级为回退路径。

---

## X1（提示，非风险）`pinned_hostbuf_size` 在 XPU 上的输出变化 —— 预期且必要

`pinned_hostbuf_size`（`model_management.py:1712-1715`）= `max(0, int(min(size, MAX_PINNED_MEMORY)*2))`。
- 改前 XPU：`MAX=-1` → `min(size,-1)=-1` → `max(0,-2)=0` → `HostBuffer(0, 8MiB, 0)` 的 `max_mmap_size=0`（`host_buffer.py:81`）。
- 改后 XPU：`MAX=预算` → `min(model_size, 预算)*2` → `max_mmap_size>0`。
- 影响：这实际是**让 hostbuf 可增长**的必要条件（`max_mmap_size=0` 时 `extend` 很可能失败 → 直接 `_steal_pin`，RAM 层仍建设不起来）。⇒ 属**正向且必要**的连带改动。

**建议**：在 IMPL 文档补一句“XPU 预算同时通过 `pinned_hostbuf_size` 决定 HostBuffer 的 `max_mmap_size`，这是 RAM 层可增长的前提”，便于后续维护者理解耦合。**无需改代码。**

---

## X2（有风险，代码，必须修）`xpu_ram_cache_enabled()` 混合机误判

**场景：** 机器同时有 Intel iGPU/Arc 和 NVIDIA（或 AMD）dGPU，且用户以 CUDA/ROCm 运行（本 portable 是 XPU 构建，`torch.xpu.is_available()` 在混合机为 True）。
**触发条件：** `cpu_state==GPU` 且 `xpu_available==True` 且 `MAX_PINNED_MEMORY>0`（由 `is_nvidia()` 分支赋值）。
**后果：** `xpu_ram_cache_enabled()` 返回 True → `ops.py:232` 的 `fast_disk` 直通分支在 **CUDA 上被触发** → 对 fast_disk 模型也建 RAM pin，偏离 stock 行为、额外占用宿主内存。**违反“CUDA/ROCm 行为零变化”承诺（低频，但真实）。**
**根因：** `is_intel_xpu()` 只判断“系统里有 XPU 且当前是 GPU 模式”，**不判断“当前实际选中设备就是 XPU”**。

**可执行修复（1 行）：**
```python
def xpu_ram_cache_enabled():
    """XPU 三层存储：RAM 中间缓存是否启用。仅当“当前实际设备”为 XPU 时生效，
    避免 Intel iGPU + NVIDIA/AMD 混合机在 CUDA 模式下误触。"""
    return get_torch_device().type == "xpu" and MAX_PINNED_MEMORY > 0
```
（等价替代：`return is_intel_xpu() and not HOST_PIN_REGISTRATION_SUPPORTED and MAX_PINNED_MEMORY > 0`，因混合机 CUDA 模式下 `HOST_PIN_REGISTRATION_SUPPORTED=True`。）
**验证方法（静态）：** 在 `model_management.py` 内确认 `get_torch_device()` 在 L1744 处可调用（该函数在 L~200 已定义，安全）。

---

## 自检清单（用户可在自己机器上逐项打勾）

- [ ] **S1 无 Intel 环境导入不报错**：在无 XPU 的机器 `import comfy.model_management`（或直接启动 ComfyUI）不抛异常；`HOST_PIN_REGISTRATION_SUPPORTED` 为 False，`xpu_ram_cache_enabled()` 为 False。
- [ ] **S2 纯 NVIDIA/AMD CUDA 机**：`xpu_ram_cache_enabled()` 为 False；`HOST_PIN_REGISTRATION_SUPPORTED` 为 True；`get_pin` 仍走注册逻辑（看日志 `Enabled pinned memory ...`）。
- [ ] **S2b 混合机（Intel iGPU + NVIDIA/AMD）CUDA 模式**：`xpu_ram_cache_enabled()` **应为 False**（若为 True → X2 未修）。
- [ ] **S3 XPU 预算生效**：启动日志出现 `Enabled XPU RAM cache ...`；`torch.xpu.is_available()` 为 True、设备名正确。
- [ ] **S3b 计数收敛（R1 实证）**：运行中观测 `TOTAL_PIN_CACHE_MEMORY` 随 `loaded_ram_size()` 上升，到预算附近后**出现回落**（证明 `partially_unload_ram` 扣账生效），而非单调到顶后 `ensure_pin_registerable` 恒 False。
- [ ] **S4 三档命中可区分 + 数据正确（R6）**：`MiniMax H3` 等工作流出图；用 H2 脚本证明 hostbuf 填充与 H2D 逐字节一致；或对同一 seed 出图与“禁用 RAM 缓存”逐像素/哈希比对一致。
- [ ] **S5 回滚**：`AIMDO_XPU_RAM_CACHE_GB=0` 启动 → 日志 `XPU RAM cache disabled ... disk passthrough mode`；行为退回磁盘直通，出图与改前一致。
- [ ] **S6 CUDA/CPU 回归**：非 XPU 机器出图结果与改动前逐像素/哈希对比一致（或至少 CUDA 机走查 + 出图无异常）。
- [ ] **S7 行尾/部署**：应用补丁后 4 文件 md5 等于本报告 §0.1 的值（唯一判据）。

---

## 实机验证步骤 + 失败回退

| 步骤 | 命令 | 预期输出 | 不符时的回退动作 |
|---|---|---|---|
| V1 打补丁 | `cd <ComfyUI> && git apply -p0 --check comfyui-3tier-ram-cache.patch` 然后 `git apply -p0 ...` | 无输出=可应用；应用后 4 md5 = §0.1 | 改用 `patch -p0 --binary`；仍失败→停止，检查补丁/行尾，勿转 LF |
| V2 语法 | `python -c "import ast;[ast.parse(open(f,'rb').read()) for f in [...]]"` | 无异常 | 回滚备份 `.bak.20261009-032052` |
| V3 启动门禁 | 正常启动（不设 env） | 日志 `Enabled XPU RAM cache <MB>` | 未见→检查 `is_intel_xpu()` 与 DISABLE_PINNED_MEMORY（集成显卡会被强制禁 pin） |
| V4 填充证明 | 运行 §R6 的 H1 脚本 | `HOSTBUF_FILLED_OK` | FAIL→“文件→RAM”腿不成立，返工 |
| V5 H2D 证明 | 运行 §R6 的 H2 脚本（XPU） | 逐字节一致 | 不一致→按 R6 修复建议强制走回退路径 |
| V6 收敛证明 | 运行大模型，采 `TOTAL_PIN_CACHE_MEMORY`/`loaded_ram_size()` | 到顶后回落、非恒 False | 不回落→按 R1 排查 `models_for_pin_eviction` 过滤（是否 model 未进 `current_loaded_models`/`is_dynamic()`） |
| V7 回滚 | `set AIMDO_XPU_RAM_CACHE_GB=0` 启动 | `XPU RAM cache disabled ... disk passthrough mode` | 未回退→检查 env 解析 `_xpu_ram_cache_gb_from_env` |
| V8 出图 | 跑 MiniMax H3 | 出片，结果与非缓存对照一致 | 异常→先设 env=0 退回磁盘直通再定位 |

---

## 附：本次审查用到的复现命令（便于复核）

```bash
# md5
cd <ComfyUI>/comfy && md5sum model_management.py pinned_memory.py model_patcher.py ops.py
# 行尾（脚本见 aimdo-xpu/_review_eol.py）
<python_embeded>/python.exe aimdo-xpu/_review_eol.py comfy/model_management.py ...
# 重放（临时目录）
mkdir -p _replay/comfy && cp comfy/*.bak.20261009-032052 -> _replay/comfy/*.py
cd _replay && git apply -p0 <patch>            # 或 patch -p0 --binary -i <patch>
# 比对：与工作区 md5 逐字节 IDENTICAL（见 §R3）
# 裸 CUDA 调用
grep -rn "torch\.cuda\.cudart(" --include=*.py <ComfyUI>/comfy | grep -v .bak
```

---
---

# §R2-Round2（修正后再次送审）：复核 X2 一行修复

> 日期：2026-10-09 · 审查人：xpu-compat-reviewer
> 变更：`model_management.py` `xpu_ram_cache_enabled()`（新 `:1744-1757`）
> 改前：`return is_intel_xpu() and MAX_PINNED_MEMORY > 0`
> 改后：`return get_torch_device().type == "xpu" and MAX_PINNED_MEMORY > 0`
> 约束遵守：未改源码；未运行 ComfyUI；重放在 `_replay2/` 临时目录；未动 site-packages。

## 判定：**X2 已闭合**（代码正确、无回归；附一处必须纠正的“事实澄清”，见下）

---

## 1. `get_torch_device()` 是否每条返回路径都带 `.type`？—— **通过（无新崩溃风险）**

完整函数体（`model_management.py:195-213`）逐分支枚举：

| 分支 | 返回 | 是否 `torch.device`（`.type` 可用） |
|---|---|---|
| `if directml_enabled:` | `directml_device`（`torch_directml.device(...)`） | ✅ 是（torch.device） |
| `if cpu_state == MPS:` | `torch.device("mps")` | ✅ 是 |
| `if cpu_state == CPU:` | `torch.device("cpu")` | ✅ 是 |
| `else` → `is_intel_xpu()` | `torch.device("xpu", idx)` | ✅ 是 |
| `else` → `is_ascend_npu()` | `torch.device("npu", idx)` | ✅ 是 |
| `else` → `is_mlu()` | `torch.device("mlu", idx)` | ✅ 是 |
| `else`（末档） | `torch.device(torch.cuda.current_device())` = `torch.device("cuda", idx)` | ✅ 是 |

**无一条返回 `None` / `str`。** `get_torch_device()` 的 6 类返回全部是 `torch.device`（`torch.device(int)` 亦构造 `device(type='cuda', index=int)`）。⇒ `.type` 恒可用，**不引入新的崩溃风险**。

## 2. 混合机“is_intel_xpu() 为 True 但当前设备为 cuda”是否可达？—— **不可达（关键澄清）**

**结论：该状态在真实代码里构造不出来，因此无法按原话“证明新表达式在此状态返回 False”。** 我给出的是诚实的反证，而非默认通过：

- `get_torch_device()` 在 GPU 态**先判 `is_intel_xpu()`**（L206-207），只要 `is_intel_xpu()` 为 True 就返回 `xpu`；CUDA 只在**最后的 `else`** 且 `is_intel_xpu()/is_ascend_npu()/is_mlu()` 全 False 时才返回。
- ⇒ **在 `else`（GPU）路径上，`get_torch_device().type == "xpu"` ⟺ `is_intel_xpu()`**，逻辑恒等。
- 设备选择侧：`cpu_state` 在 GPU/CPU/MPS 间取值（`model_management.py:61/134/159`），**没有任何“优先 cuda 而非 xpu”的开关**；`--cuda-device`/`--default_device`/`--oneapi_device_selector`（`main.py:91-110`）只调整可见设备，**不会在 xpu 可用时把计算设备切到 cuda**。故“混合机跑 CUDA ⇒ is_intel_xpu() True”的并列前提**自相矛盾**（真跑起来 `get_torch_device()` 会返回 xpu）。

**枚举证明**（纯逻辑复刻脚本 `aimdo-xpu/_round2_sim.py`，枚举 `cpu_state × xpu/npu/mlu 可用 × directml × nvidia/amd × 预算`）：
```
total states enumerated : 384
states where OLD != NEW : 32     ← 全部满足 [dml=True, cpu_state=GPU, xpu_av=True, device='privateuseone']，旧的 True / 新的 False
```
即：**新旧表达式的唯一差异集，恰好是 `--directml` 且 `xpu` 可用**的状态；在**所有非 directml 状态（含 CUDA/ROCm/CPU/MPS 以及“混合机”）上 OLD == NEW，差异为 0**。
- 在 `--directml` 分支，`get_torch_device()` 返回 DirectML 设备（`privateuseone`）→ 新式 `False`（**正确**：算子在 DirectML，不该开 XPU RAM 缓存）；旧式 `is_intel_xpu()` 可能 `True`（**错误**）。⇒ 该修复**确实修掉了一个（冷门但真实）分歧**。
- 而 team-lead 要求的“cuda 当前设备 + is_intel_xpu() True”这一**假设状态**：若人工 stub 成 `get_torch_device()->cuda`，则新式 `False`、旧式 `True`（脚本已演示）；但**真实 `get_torch_device(cpu_state=GPU, xpu_available=True, ...)` 返回 `'xpu'`**，故新式在该真实状态下仍为 `True`。⇒ 该假设状态不可由真实代码进入。

**对 X2 原始描述的自我更正**：Round-1 中我把 X2 表述为“混合机 CUDA 模式会被污染”，措辞**过强**——经本轮反证，该场景在本仓设备选择逻辑下不可达。修复后的代码**更稳健、语义更正确**（严格以“当前计算设备”为准，并额外覆盖 `--directml`），但请 Phase 5 **不要**把它当作“修了一个会在混合机复现的 bug”。

**文档精度建议（1 处，非阻塞）**：新 docstring 里“在混合机（Intel 核显 + NVIDIA/AMD 独显）上即使 ComfyUI 跑在 CUDA 模式、is_intel_xpu() 仍可能为 True”这一**场景描述不准确**（该模式下 `get_torch_device()` 会返回 xpu，ComfyUI 实为 XPU 运行）。建议改写为准确理由，例如：
> “`is_intel_xpu()` 反映设备可用性；而 `--directml` 等情况下计算设备并非 XPU，故一律以 `get_torch_device().type` 为准，避免在非 XPU 计算设备上误触 fast_disk 分支。”
（仅改注释文字，**不改行为**。）

## 3. XPU 正常路径是否受损？—— **通过**

XPU 运行态：`get_torch_device()` → `torch.device("xpu", idx)` ⇒ `.type == "xpu"` 为 True，结果**仅由 `MAX_PINNED_MEMORY > 0` 决定**，与改前 `is_intel_xpu() and MAX>0` 完全一致（改前在 XPU 上 `is_intel_xpu()` 本就为 True）。**语义等价，正常路径不受损。**

## 4. CUDA / ROCm / CPU / MPS 恒 False？—— **通过（推理链成立）**

- CUDA：`cpu_state==GPU`、`xpu_available==False` → `get_torch_device()` 末档 → `"cuda"` → `False` ✅
- ROCm：同上（ROCm 经 `torch.cuda` 暴露，`.type=="cuda"`）→ `False` ✅
- CPU：`args.cpu` → `cpu_state==CPU` → `"cpu"` → `False` ✅
- MPS：`cpu_state==MPS` → `"mps"` → `False` ✅
（另：这些状态下 `is_intel_xpu()` 本就 False，故新旧皆 False，无行为变化。）

## 5. 行尾是否被破坏？—— **通过（4 文件仍 PURE_CRLF）**

字节级（脚本 `aimdo-xpu/_review_eol.py`）：
```
model_management.py  CRLF=2318 bareLF=0 bareCR=0 -> PURE_CRLF
pinned_memory.py     CRLF=139  bareLF=0 bareCR=0 -> PURE_CRLF
model_patcher.py     CRLF=2190 bareLF=0 bareCR=0 -> PURE_CRLF
ops.py               CRLF=1825 bareLF=0 bareCR=0 -> PURE_CRLF
```
X2 的多行 docstring 编辑**未引入任何 bare-LF**；`model_management.py` CRLF 由 2311→2318（+7，新增注释行）。✅

## 6. 补丁是否仍可重放？—— **通过（两种方式均逐字节 identical）**

补丁现状：`comfyui-3tier-ram-cache.patch`，306 行、15547 字节、MIXED（CRLF=284 + bareLF=22，22 行为 `---/+++/@@` 元数据行，与 Round-1 同构）。
干净副本（`_replay2/`，由 `.bak.20261009-032052` 还原）：
```
git apply -p0 --check   -> OK
git apply -p0           -> OK
patch -p0 --binary -i   -> OK
```
逐字节比对结果：
```
model_management.py  git IDENTICAL  gnupatch_binary IDENTICAL
pinned_memory.py     git IDENTICAL  gnupatch_binary IDENTICAL
model_patcher.py     git IDENTICAL  gnupatch_binary IDENTICAL
ops.py               git IDENTICAL  gnupatch_binary IDENTICAL
```
⇒ **与工作区 4/4 逐字节一致。** 部署方式不变：`git apply -p0`（首选）或 `patch -p0 --binary`；**禁止**无 `--binary` 的 `patch`、禁止转 LF。

## 7. 实测 md5（供 Phase 5 文件闸引用）—— **与作者声明一致**

```
0a9040f28c8c747b52b92009d18bf82f  comfy/model_management.py
c629da9c2089c7c75d3b53386f49fdc9  comfy/pinned_memory.py
d523b099933dad858e3a9e73a567c730  comfy/model_patcher.py
96090a01b13e1b81907152324b6430a0  comfy/ops.py
```
（`model_management.py` 由 Round-1 的 `6bd2186e...` 变为 `0a9040f2...`，其余 3 个未变，符合“仅 X2 一行+docstring”的预期。）
补丁文件 md5（仅供参考）：`7c6118e27a4e070c74c664387f0e9013`。

## 8. Round-2 复核结论汇总

| 复核项 | 结论 |
|---|---|
| 1. `get_torch_device()` 返回类型 | 通过（恒为 `torch.device`，无 None/str） |
| 2. 混合机“is_intel_xpu True + cuda 设备”可达性 | **不可达**（原 X2 描述过强，已自我更正）；修复实际生效点 = `--directml` 分支 |
| 3. XPU 正常路径 | 通过（语义等价，仅由预算门控） |
| 4. CUDA/ROCm/CPU/MPS 恒 False | 通过 |
| 5. 行尾 PURE_CRLF | 通过 |
| 6. 补丁可重放且逐字节 identical | 通过 |
| 7. md5 断言 | 通过（与作者一致） |

**最终判定：`X2 已闭合`。** 无阻塞、无返工项；仅建议改一句 docstring 的“场景描述”以精确化（非阻塞，不影响行为与验收）。

---

## §R2-Round2b（addendum）：docstring 精确化后续改动复核

**背景**：受 §R2-Round2 第 2 项“文档精度建议”驱动，作者于 **2026-10-09 03:35** 又改了一次 `model_management.py`（仅 docstring）。文件 md5 由 `0a9040f2...` 变为 **`0f7c056c...`**（其余 3 文件未变）。此处复核该 delta。

**delta（唯一改动，docstring 文本）**：
```
- 在混合机（Intel 核显 + NVIDIA/AMD 独显）上即使 ComfyUI 跑在 CUDA 模式、is_intel_xpu() 仍可能为 True；
- 又因 is_nvidia()/is_amd() 成立使 MAX_PINNED_MEMORY>0，旧的 is_intel_xpu() 写法会让 ops.py:232 的 or 在 CUDA 上被触发，
- 破坏"CUDA 零变化"铁律。故此处一律以 get_torch_device().type 为准。请勿"简化"回 is_intel_xpu()。
+ 必须判"当前选中的计算设备"而非"XPU 是否可用"：
+ is_intel_xpu() 只反映 XPU 可用性；而 get_torch_device() 里 directml 分支
+ （model_management.py:198-200）是它的最前置短路——在 --directml 下即使 XPU 可用，
+ 实际计算设备也不是 XPU（:206 的 is_intel_xpu() 分支根本轮不到）。
+ 若用 is_intel_xpu()，就会在 DirectML 上因 MAX_PINNED_MEMORY>0 而误触发 ops.py:232 的 or，
+ 去建 RAM pin，破坏"CUDA/DirectML 零变化"铁律。故此处一律以 get_torch_device().type 为准。
```
（函数**行为行** `return get_torch_device().type == "xpu" and MAX_PINNED_MEMORY > 0` **未变**。）

**复核结论**：
- ✅ 新 docstring 的场景描述**与我 Round-2 的反证一致**（`--directml` 是 `get_torch_device()` 的最前置短路，见 `model_management.py:198-200`；`:206` 的 xpu 分支在其后）——**不再声称“混合机跑 CUDA”这一不可达场景**，误导已消除。
- ✅ 引用行号核对无误（198-200 = directml 分支；206 = `if is_intel_xpu():`）。
- ✅ **纯注释改动，零行为影响**；`xpu_ram_cache_enabled()` 的判定逻辑未变，Round-2 的全部结论继续成立。
- ✅ 行尾仍 **PURE_CRLF**（CRLF=2318, bareLF=0，size 83187）。
- ✅ 补丁已**同步再生成**（`a517cfa1...`，306 行/15600 字节/MIXED 同构，含新 docstring）；实测 `git apply -p0` 与 `patch -p0 --binary` 重放后**逐字节还原当前工作区（4/4 IDENTICAL）**。

**md5 更新（本条 addendum 取代 §7 中的旧值）**：
```
0f7c056c0d45645f81b4256f3d56703a  comfy/model_management.py      <-- 以本值为准
c629da9c2089c7c75d3b53386f49fdc9  comfy/pinned_memory.py
d523b099933dad858e3a9e73a567c730  comfy/model_patcher.py
96090a01b13e1b81907152324b6430a0  comfy/ops.py
a517cfa1fc6f2be64fc0a47546494c54  comfyui-3tier-ram-cache.patch
```
（同步写入 `aimdo-xpu/PHASE5_EXPECTED_MD5.txt`，供 Phase 5 文件闸引用。）

**判定：`X2 已闭合`（维持），docstring 精确化已落实且无副作用。**

---

## §R6-Followup（addendum）：本报告 §R6 给出的 H1/H2 harness 已被实测证伪

> 追加于 2026-10-09（Phase 5 真机验收之后）。**仅追加，不改动既有观测/数值/行号。**

本报告 **§R6** 曾给出“最小 harness（不启动 ComfyUI）”的 H1/H2 代码，并据此宣称“R6 机制成立、需实机验证”。**Phase 5 真机验收把这些探针实测证伪**（根因经真机验证，非推断）：

| 被证伪者 | 根因（真机实测） | 后果 |
|---|---|---|
| §R6 的 H1 代码 / `probe_h1_hostbuf_fill.py` | `comfy_aimdo/host_buffer.py:6` 在 **import 期** `lib = control.lib`；独立脚本未调用 `control.init()`（且真机实测还需 `control.init_devices()`，见 `main.py:285`）→ `lib` 恒 `None`（`control.py:12`→`:251` 才赋值）→ argtypes 未绑定 → `HostBuffer()` 抛 `AttributeError`；**仅 `init()` 不够，缺 `init_devices()` 时 `hostbuf_*` 会 access violation** | 恒 `HOSTBUF_API_FAIL`，**拿不到真值**（把探针缺陷伪装成功能失败） |
| §R6 的 H2 代码 / `probe_h2_hostbuf_h2d.py` | 同上根因；“非 XPU skip”分支被崩溃误判为 `API_FAIL` | 假失败 |
| `probe_s3b_launch.py`（工装，非本报告 §R6） | 不提交 prompt；且对 `LoadedModel` 误调 `loaded_ram_size()`（实为 `ModelPatcher` 方法，`model_patcher.py:2072`；范例 `model_management.py:1057`）→ 异常被吞 | `loaded_ram_mb` 恒 0 的伪证据 |

**重要澄清（结论未倒）**：被证伪的是**我给出的探针实现**，**不是** R1/R6 结论本身。验收方用替换件（`yan_h1_ram_fill.py` / `yan_h2_ram_h2d.py` / `yan_s3b_run.py`）在真机取得可信结果：**H1/H2 各 32 MiB 逐字节一致**；**S3b 缓存 0→8178 MiB 触顶后回落（峰后 drop 270 MiB）**——即 §R6 与 R1 的通过判据在真机**成立**。

**指向**：详见 `PHASE5_PROBE_KIT.md` 顶部新增的「⚠️ 后续事实：三个探针已被真机实测证伪」一节（含替换件与真机数值）。**请勿复用 §R6 的 H1/H2 代码或 `probe_h1/h2/s3b_*.py`。**

**方法论备注**：本次说明“工具本身也要被审查”——探针在真机、逐字节的位置被证伪，正是“先证伪再花 GPU”的流程在起作用。
