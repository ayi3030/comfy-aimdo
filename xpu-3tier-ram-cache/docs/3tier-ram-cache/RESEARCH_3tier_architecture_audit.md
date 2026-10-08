# 上游三层存储架构审计报告（RESEARCH_3tier_architecture_audit）

- 任务：Phase1-B —— 上游三层存储架构审计（只读，未修改任何文件）
- 审计对象：`E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/ComfyUI/`（含 `comfy_aimdo` 二进制侧通过 Python API 暴露的契约）
- 审计人：comfyui-python-engineer-3
- 方法：逐行静态阅读调用链；所有结论均给出 `文件:行`，可被逐行复核
- 说明：`comfy_aimdo` 为原生扩展（无 .py 源码），其内部实现不在本次审计范围；本报告只审计**它被上游调用的契约与调度点**，凡涉及二进制内部行为处均显式标注「未查证/需实机验证」。

---

## 0. 一句话结论（先给答案）

上游代码里**三层存储结构是真实存在的**：磁盘（mmap）、RAM（hostbuf pin 缓存）、VRAM（VBAR + cast buffer）三层都有完整的落地代码与管理 API。**但 RAM 层是一个「条件性缓存」**：它是否被使用，由 `fast_disk` 策略（`ops.py:232`）与 `args.high_ram` 共同决定。

用户观察到的「**显存不足直接取硬盘、没有内存这一步**」——**在代码上部分成立**：

- 当模型被判定为 `fast_disk=True`（磁盘足够快，例如 NVMe；Windows 走 `comfy_aimdo.storage.fast_disk` 探测）时，一旦 VBAR 里已有签名（即该区域「曾经 stage 过」），上游会**刻意跳过 RAM pin 的创建**，直接走 `read_file_to_device`：**文件 → 显存**（`memory_management.py:57-63`）。这就是用户看到的「没有内存这一步」。
- 当 `fast_disk=False`（慢盘）时，RAM 层**是活跃的**：走 `hostbuf.read_file_slice`：**文件 → hostbuf(RAM) → 显存**（`memory_management.py:65-75`）。

也就是说：**不是「没有 RAM 层」，而是「快盘策略把 RAM 层旁路了」**。这是一个有意的性能取舍，而不是缺失。

---

## 1. 分层示意图（文字版）

```
                      ┌──────────────────────────────────────────────────────────┐
                      │  调用入口：ops 层取权重                                      │
                      │  comfy/ops.py:340  cast_bias_weight(s, ...)                │
                      │     └─ ops.py:375  hasattr(s,"_v") and s.weight.device != device
                      │           └─ ops.py:380  cast_modules_with_vbar([s],...)   │  ← 预取/取数入口
                      │           └─ ops.py:383  resolve_cast_module_with_vbar(s)  │  ← 消费已就位的 buffer
                      └────────────────────────────┬─────────────────────────────┘
                                                   │
        ═══════════════════════════════════════════▼═══════════════════════════════════════════
                                    三层存储 & 搬运（唯一搬运原语）
        ═══════════════════════════════════════════════════════════════════════════════════════

  [层3 磁盘 / DISK]  ── 权威 backing store（始终存在）
      · 载体：mmap 的 safetensors 只读内存视图
      · 标记：comfy/utils.py:150-152  setattr(storage,"_comfy_tensor_file_slice", TensorFileSlice(file_ref, lock, offset, size))
      · 数据来源：comfy/utils.py:105-107  comfy_aimdo.model_mmap.ModelMMAP(ckpt) → get_file_handle()/get()
        → 因此 s.weight / s.bias 本身= 文件切片 CPU 张量（object dtype 视图）

             │
             │  搬运原语（唯一）：comfy/memory_management.py:18 read_tensor_file_slice_into(tensor, destination, stream, destination2)
             │
             ├───────────(支1, memory_management.py:57-63) destination is None ─────────────┐
             │            comfy_aimdo.host_buffer.read_file_to_device(...)                    │
             │            = 文件 → 显存   ★【快盘 fast_disk 旁路路径】                          │
             │                                                                               │
             ▼                                                                               │
  [层2 RAM / HOSTBUF 缓存]  ── 条件性缓存（可被旁路 / 可被驱逐）                              │
      · 载体：comfy_aimdo.host_buffer.HostBuffer → hostbuf_to_tensor(hostbuf) 切片
      · 标记：comfy/pinned_memory.py:103-104  pin.untyped_storage()._comfy_hostbuf = hostbuf
      · 池：6 个子池 pin_state{...}  model_patcher.py:1783-1789 / 1883-1888
      · 搬运：memory_management.py:65-75  hostbuf.read_file_slice(..., device_ptr=<VRAM>)      │
             │  = 文件 → hostbuf(RAM) → 显存   ★【慢盘完整三层路径】      ◄─────────────────────┘
             │
             ▼
  [层1 VRAM / 显存工作集]
      · 载体A：VBAR 虚拟地址区域   comfy/model_patcher.py:1811  comfy_aimdo.model_vbar.ModelVBAR(size*10, device.index)
      ·        模块挂载：model_patcher.py:1993  m._v = vbar.alloc(v_weight_size)
      ·        解析入口：comfy/ops.py:168  comfy_aimdo.model_vbar.vbar_fault(s._v)（返回 signature=是否驻留）
      ·        驱逐：model_patcher.py:2050  vbar.free_memory(memory_to_free)
      · 载体B：cast buffer（离线流）  comfy/ops.py:162-165  get_aimdo_cast_buffer(...)

        ═══════════════════════════════════════════════════════════════════════════════════
        管理层 API（谁在管三层的进出）
        ═══════════════════════════════════════════════════════════════════════════════════
        RAM 层规模/驱逐： model_patcher.py:2072 loaded_ram_size / :2076 pinned_memory_size /
                          :2080 unregister_inactive_pins / :2107 partially_unload_ram
                         model_management.py:674 free_model_pins / :693 pin_eviction_tiers /
                          :728 free_pins / :747 ensure_pin_budget / :757 free_registrations
        RAM 缓存水线   ： memory_management.py:178 set_ram_cache_release_state(callback, headroom)
                          execution.py:748（每个 prompt 设） / :841（finally 复位）
        VRAM 层规模     ： model_patcher.py:1815 loaded_size()（vbar.loaded_size() + model_loaded_weight_memory）
```

---

## 2. (a) 「已 staged、当前不在显存」的权重的完整调用链

### 2.1 调用链（逐跳，含行号）

从「ops 层需要一个权重做前向」开始：

| # | 位置 | 动作 |
|---|------|------|
| 1 | `comfy/ops.py:340` | `cast_bias_weight(s, input, dtype, device, ...)` 被某个 `comfy_cast_weights=True` 的 module（Linear/Conv 等）在前向里调用 |
| 2 | `comfy/ops.py:375` | 命中分支 `hasattr(s, "_v") and s.weight.device != device`（CPU 上的 mmap 权重 vs 目标 device） |
| 3 | `comfy/ops.py:375-381` | 若非 prefetch 命中（`hasattr(s,"_prefetch")` 为 False）：调 `cast_modules_with_vbar([s], ...)`（`ops.py:380`），随后 `sync_stream` |
| 4 | `comfy/ops.py:168` | `signature = comfy_aimdo.model_vbar.vbar_fault(s._v)`；`ops.py:169` `vbar_signature_compare(signature, s._v_signature)` |
| 5 | `comfy/ops.py:177-179` | **若 resident=True：数据已在 VRAM，直接 `s._prefetch=prefetch; continue`，不搬运** |
| 6 | `comfy/ops.py:182` | 非驻留：`xfer_dest = aimdo_to_tensor(s._v, device)`（若签名非空，接收目标=VBAR 区；否则=None） |
| 7 | `comfy/ops.py:187-197` | 组装搬运源：`xfer_source=[s.weight, s.bias]`（文件切片）；`fast_disk = s._pin_state["fast_disk"]`；`subset = "weights-fast" if fast_disk else "weights"`；`pin = get_pin(s, subset)`；若拿到 pin → `xfer_source=[pin]`（**源换成 RAM hostbuf**） |
| 8 | `comfy/ops.py:208-211` | `dest_size = vram_aligned_size(xfer_source)`；若 `xfer_dest is None` → `xfer_dest = get_cast_buffer(dest_size)`（借用 cast buffer 当 VRAM 目标） |
| 9 | `comfy/ops.py:228-237` | `handle_pin(s, pin, xfer_source, xfer_dest, ...)`：**这是「文件/内存/显存」三选一的决策点** |
| 10 | `comfy/ops.py:226 → model_management.py:1548` | `cast_to_gathered(xfer_source, r=<pin 或 None>, stream, r2=<VRAM dest>)` |
| 11 | `comfy/model_management.py:1555-1563` | `dest_views = interpret_gathered_like(tensors, r)`、`dest2_views = interpret_gathered_like(tensors, r2)`；对每个 tensor 调 `read_tensor_file_slice_into(tensor, dest_view, stream, destination2=dest2_view)` |
| 12 | `comfy/memory_management.py:18` | **唯一搬运原语**，落到下述三支之一 |
| 13 | `comfy/model_management.py:1565-1570` | 若 `read_tensor_file_slice_into` 返回 False → 走 `mark_mmap_dirty` + `dest_view.copy_(tensor)` + `dest2_view.copy_(...)`（普通 device copy 兜底） |
| 14 | `comfy/ops.py:270` `resolve_cast_module_with_vbar` | 用 `prefetch["xfer_dest"]` / `cast_dest` 经 `interpret_gathered_like`（`ops.py:287`）还原出 weight/bias 张量视图，交给前向 |
| 15 | `comfy/ops.py:334-335, 392` | `prefetch["resident"]=True`、`delattr(s,"_prefetch")`，本次调用结束 |

### 2.2 最终落到 `read_tensor_file_slice_into` 的哪一支？

由 `ops.py:228-237 handle_pin` 决定，分三种情形：

**情形 ①：pin 已存在（权重已在 RAM 缓存）**
- `ops.py:229-231`：`cast_maybe_lowvram_patch([pin], dest=xfer_dest, ...)` → `cast_to_gathered([pin], r=xfer_dest, r2=None)`
- 进 `read_tensor_file_slice_into(pin, destination=VRAM, destination2=None)`：
  - `memory_management.py:40-42`：`destination.device.type != "cpu"` 且 `destination2 is None` → **交换**：`destination2 = destination; destination = None`
  - `memory_management.py:36-38`：pin 的 storage 没有 `_comfy_tensor_file_slice` → **返回 False**
  - 回落 `model_management.py:1568` `dest_view.copy_(tensor)` → **纯 RAM→VRAM device copy**（不经文件）
- **结论：落到「非文件分支」——由 `copy_` 完成的 RAM→VRAM。**

**情形 ②：pin 不存在，但本次允许创建（慢盘 / 首次 stage）**
- `ops.py:232` 条件 `signature is None or not fast_disk or args.high_ram` 为真 → `pin_memory(...)`（`ops.py:233`）→ 再 `get_pin`（`ops.py:234`）
- `ops.py:235`：`cast_maybe_lowvram_patch(source=xfer_source(文件切片), pin(CPU hostbuf), offload_stream, xfer_dest2=xfer_dest(VRAM))` → `cast_to_gathered(source, r=pin, r2=VRAM)`
- 进 `read_tensor_file_slice_into(tensor=文件切片, destination=pin(CPU hostbuf), destination2=VRAM)`：
  - 通过 `memory_management.py:44-52` 的一系列校验
  - `memory_management.py:65`：`hostbuf = destination.untyped_storage()._comfy_hostbuf` → 命中（`pinned_memory.py:104` 已设）
  - **落到 `memory_management.py:66-75`：`hostbuf.read_file_slice(..., device_ptr=<VRAM>, device=<VRAM index>)`**
- **结论：落到「文件 → hostbuf(RAM) → 显存」这一支（真正的三层路径）。**

**情形 ③：pin 不存在 且 不允许创建（快盘 + 已有签名 + 非 high_ram）**
- `ops.py:232` 条件为假 → 不 `pin_memory`；`pin` 仍为 None
- `ops.py:235`：`cast_maybe_lowvram_patch(source=xfer_source(文件切片), pin=None, offload_stream, xfer_dest2=xfer_dest(VRAM))` → `cast_to_gathered(source, r=None, r2=VRAM)`
- 进 `read_tensor_file_slice_into(tensor=文件切片, destination=None, destination2=VRAM)`：
  - **落到 `memory_management.py:57-63`：`comfy_aimdo.host_buffer.read_file_to_device(...)` = 文件 → 显存（直通 DMA，不经 RAM）**
- **结论：落到「文件 → 显存」这一支（用户观察到的现象）。**

### 2.3 `destination` / `destination2` 是否为 None 由谁决定？

**由 `ops.py` 的 `handle_pin` / `cast_maybe_lowvram_patch` 决定**，更上游是「pin 是否存在」：

- `cast_to_gathered(tensors, r, ..., r2)` 的 `r`/`r2`：
  - `r`（→ `destination`）= **pin（CPU hostbuf 张量）或 None**（`ops.py:226/230/235` 传入）
  - `r2`（→ `destination2`）= **VRAM 目标**（`ops.py:235` 的 `xfer_dest2=dest`）
- 若「只有一个 VRAM 目标」被当作 `r` 传入，则 `memory_management.py:40-42` 会在函数内部把 `destination` 归一化为 `None`、把 VRAM 目标挪到 `destination2`，从而进入 `read_file_to_device` 支。**即：`destination=None` 的最终判定在 `memory_management.py:40-42` 与 `:57`。**

一句话：**`destination` 是否为 None ⇔ 该权重当前是否存在 RAM hostbuf pin**（且该 pin 是否被选为搬运源）。

---

## 3. (b) RAM 缓存层是否存在？替换策略？三个管理 API 的触发条件

### 3.1 存在，且是真实活跃的缓存层

- **载体**：`comfy_aimdo.host_buffer.HostBuffer`。6 个子池见 `model_patcher.py:1783-1789`（初始化）与 `:1883-1888`（真实分配，含 `hostbuf_size = pinned_hostbuf_size(model_size())`）：
  `weights / patches / weights-loaded / patches-loaded / weights-fast / patches-fast`
- **RAM 与 hostbuf 的绑定**：`pinned_memory.py:98-104`：`offset = hostbuf.size; hostbuf.extend(size); pin = hostbuf_to_tensor(hostbuf)[offset:offset+size]; pin.untyped_storage()._comfy_hostbuf = hostbuf`
- **规模统计**：`model_patcher.py:2072 loaded_ram_size()`、`:2076 pinned_memory_size()`
- **上限**：`model_management.py:1631-1644` `MAX_PINNED_MEMORY`（Windows 约 40% 物理内存；集成 GPU 直接 `DISABLE_PINNED_MEMORY`，见 `:1627-1634`）

### 3.2 替换策略：**不是 LRU**

替换策略由两部分组成，均**不按「访问时间」**：

**(1) 同尺寸桶 + 优先级抢占（`pinned_memory.py`）**
- `:10-15 _add_to_bucket`：按 **size** 分桶，桶内按 `-priority` 用 `bisect.insort` 排序
- `:17-46 _steal_pin`：容量不足时，从同 size 桶尾（最低优先级）抢占一个受害 pin，把它的 `pin/registered/stack_index` 让给请求者
- `:88-91`：`priority = comfy.utils.bit_reverse_range(counter[0], 16)`，即按**分配序号做 bit-reverse 打散**（`utils.py:1532`）——一种「均匀错峰」而非 LRU 的淘汰权重

**(2) 分层（tiered）驱逐（`model_management.py`）**
- `:693-709 pin_eviction_tiers`：按子池与状态分层的顺序驱逐：
  `FAST(非当前prompt) → 常规(非当前prompt) → LOADED(非当前prompt) → FAST(当前prompt) → LOADED(当前prompt) → 常规(当前prompt)...`
- `:711-726 registration_eviction_tiers`：unregister 时的类似分层
- `:674-691 free_model_pins`：跨所有 dynamic 模型执行 `unregister_inactive_pins`（仅解除注册，`model_patcher.py:2080`）或 `partially_unload_ram`（真正销毁，`model_patcher.py:2107`）

**「最近性」信号**：只有 `stack_split`（`model_patcher.py:2085`、`pinned_memory.py:64/122`）这个栈式游标，用于 `unregister_inactive_pins` 的反向遍历；**没有按 tensor 命中时间维护的 LRU 链表**。所以严格来说：**RAM 层替换 = 「尺寸桶 + bit-reverse 优先级 + 子池分层」驱逐，不是 LRU。**

### 3.3 `extra_ram_release` / `RAM_CACHE_HEADROOM` / `partially_unload_ram` 触发条件

**`set_ram_cache_release_state`（`memory_management.py:178`）的调用者与参数：**
- `execution.py:748`：**每个 prompt 执行开始时**设：
  `ram_headroom = int(cache_args["ram"] * 1024**3)`；`ram_release_callback = self.caches.outputs.ram_release if cache_type == CacheType.RAM_PRESSURE else None`
  → `set_ram_cache_release_state(ram_release_callback, ram_headroom)`
- `execution.py:841`：**finally 里复位** `set_ram_cache_release_state(None, 0)`
- **含义**：只有当缓存策略是 `RestoreStrategy/CacheType.RAM_PRESSURE` 时，才注册「释放输出缓存腾 RAM」的回调；否则 `extra_ram_release_callback` 恒为 None → `extra_ram_release` 恒返回 0（`memory_management.py:185-187`）。

**`extra_ram_release(target, free_active=False)`（`memory_management.py:184`）触发点（共 2 处）：**
- `pinned_memory.py:93`：**每次要给模块建立 RAM pin 之前** → `extra_ram_release(RAM_CACHE_HEADROOM)`
- `model_management.py:1684`：**每次要对单张量做 host 注册之前**（`pin_memory(tensor)`）→ `extra_ram_release(RAM_CACHE_HEADROOM)`
- 语义：向**输出/中间结果缓存**施压，让它把 RAM 降到 `headroom` 水线以下（`RAM_CACHE_HEADROOM`），为权重 pin 腾地方。

**`RAM_CACHE_HEADROOM`（`memory_management.py:176`）使用点：**
- `memory_management.py:182`：`set_ram_cache_release_state` 里赋值 `max(0, int(headroom))`
- `model_management.py:750`：`ensure_pin_budget` 里
  `shortfall = size + max(RAM_CACHE_HEADROOM/2, 2048MiB) - virtual_memory_available()`（RAM 预算判定）
- `pinned_memory.py:93`、`model_management.py:1684`：作为 `extra_ram_release` 的目标水位

**`partially_unload_ram(ram_to_unload, subsets=...)`（`model_patcher.py:2107`）触发点：**
- `model_management.py:682`：`free_model_pins(...)` 里（**非 registration 的 RAM 驱逐**，由 `free_pins`/`ensure_pin_budget` 触发）
- `model_management.py:1485`：卸载模型时，硬性释放 `patches / patches-loaded / patches-fast`
- `model_patcher.py:1828`：`unpin_all_weights()`（`__del__` 时）→ `partially_unload_ram(1e32)`
- `model_patcher.py:2144`：`unpatch_model(unpatch_weights=True)` → `partially_unload_ram(1e32)`
- 动作（`model_patcher.py:2112-2129`）：从栈顶 pop，`hostbuf.truncate(offset, do_unregister=registered)` 真正回收 RAM，直到释放量达标

**整体触发链（RAM 压力 → 驱逐）**：`execution.py:799-811`（RAM_PRESSURE 分支）→ `should_free_pins_for_ram_pressure(ram_shortfall)`（`model_management.py:734`）→ `free_pins(...)`（`:728`）→ `free_model_pins`（`:674`）→ `partially_unload_ram` / `unregister_inactive_pins`。

---

## 4. (c) 磁盘 vs RAM 的取舍策略在哪一行？`fast_disk` 改变了什么？

### 4.1 取舍发生在 `comfy/ops.py:232`

```python
# comfy/ops.py:228-237
def handle_pin(m, pin, source, dest, subset="weights", size=None):
    if pin is not None:                                   # ← pin 已存在：RAM→VRAM
        cast_maybe_lowvram_patch([pin], dest, offload_stream)
        return
    if signature is None or not fast_disk or args.high_ram:   # ★ 取舍点 ops.py:232
        comfy.pinned_memory.pin_memory(m, subset=subset, size=size)   # 建 RAM pin
        pin = comfy.pinned_memory.get_pin(m, subset=subset)
    cast_maybe_lowvram_patch(source, pin, offload_stream, xfer_dest2=dest)  # 文件→(RAM)→VRAM
```

**判定表**：

| signature | fast_disk | high_ram | 是否建 RAM pin | 实际路径 |
|---|---|---|---|---|
| None | 任意 | 任意 | **是** | 文件 → RAM(hostbuf) → VRAM |
| 非空 | **False** | 任意 | **是** | 文件 → RAM → VRAM |
| 非空 | **True** | False | **否** | **文件 → VRAM（直通）** |
| 非空 | True | **True** | **是** | 文件 → RAM → VRAM |

即：**只有「签名已存在（该区域曾 stage 过）+ fast_disk=True + 非 high_ram」三者同时成立，才旁路 RAM 层。**

### 4.2 `fast_disk` 具体改变了什么行为

`fast_disk` 是 `ModelPatcherDynamic` 的构造参数（`model_patcher.py:1754/1760`），在 `model_patcher.py:1790` 写入 `pin_state["fast_disk"]`。其影响面：

1. **子池选择**：`ops.py:189` `subset = "weights-fast" if fast_disk else "weights"`；`ops.py:247` 同理 `patches-fast`。即快盘模型把 RAM pin 放进「fast」子池，而 fast 子池在驱逐分层里**最先被驱逐**（`model_management.py:695/698/705` 等）。
2. **是否旁路 RAM**：`ops.py:232`（如上）。
3. **跳过注册预算检查**：`model_prefetch.py:115` `if not (comfy_modules and comfy_modules[0]._pin_state["fast_disk"]): ensure_pin_registerable(...)` → 快盘时不再为 pin 申请 host 注册预算。
4. **loaded pin 回退被禁用**：`ops.py:191`/`249` 的 `if pin is None and not fast_disk:` 回退分支在快盘时不生效。

**`fast_disk` 从哪来**：`comfy/storage.py:91-111`
- `state_dict_fast_disk(state_dict)` 收集源文件路径（依赖 `storage.py:83-88 annotate_state_dict` 写入的 `_comfy_source_path`），再调 `model_fast_disk(paths)`
- `storage.py:104-111 model_fast_disk`：若命令行有 `--fast-disk/--disable-fast-disk` 用它们；否则**探测磁盘速度**（`fast_storage`，Windows 走 `comfy_aimdo.storage.fast_disk(path)`，Linux 走 `_linux_fast_storage`/`_fast_nvme` 的 NVMe 链路判断）
- **语义**：磁盘够快（NVMe）→ 认为「从盘直接 DMA 到显存」比「先拷进 RAM 再拷进显存」更划算，于是**主动放弃 RAM 缓存**，把磁盘当作 backing store。

---

## 5. (d) `_comfy_hostbuf` 取不到时的走向；VRAM 还能拿到数据的情形

### 5.1 该分支的门槛

`memory_management.py:65` 的 hostbuf 分支要成立，必须：
- `destination is not None`（否则更早在 `:57` 走 read_file_to_device）
- `destination.device.type == "cpu"`（`:47` 已强制）
- 且 `destination.untyped_storage()._comfy_hostbuf` 存在

取不到时，代码继续到 `:77-97`：`file_obj.seek(info.offset)` + `readinto(view)`，**只把数据写进 `destination`（CPU）**，返回 True（`memory_management.py:95`）。

### 5.2 「显存仍能拿到数据」的三种真实情形

**关键事实：seek/readinto 分支本身永远不会填 VRAM。** 它只写 CPU（`memory_management.py:80-95`），且返回 True，导致 `model_management.py:1564 if ...: continue` 直接跳过后续 `dest2_view.copy_`。所以要显存拿到数据，只能是**这一次调用根本没走 seek 分支**，而是：

1. **该调用就没有 CPU destination**：`ops.py:191-197` 在拿到 pin 时把源换成 pin；但若 `destination` 是纯 VRAM（被 `:40-42` 归一化成 `destination2`），则走 `:57 read_file_to_device`（文件→显存）。这是快盘直通路径。
2. **CPU destination 是 hostbuf pin**（真实路径必然如此）：pin 的 storage 一定带 `_comfy_hostbuf`（`pinned_memory.py:104`），于是走 `:66-75`，**一次调用同时写 CPU hostbuf 与 VRAM（`device_ptr`）**。也就是这次调用里，显存和内存是一起被填的。
3. **调用返回 False 时的兜底拷贝**：若因 `:44-52` 任一校验不通过而返回 False，`model_management.py:1565-1570` 会执行 `dest_view.copy_(tensor)` 与 `dest2_view.copy_(tensor if dest_view is None else dest_view, ...)`——此时才通过普通 device copy 把数据送到 VRAM（且先经 `mark_mmap_dirty`）。

**因此对 (d) 的结论**：若某个 CPU destination 真的缺失 `_comfy_hostbuf` **且同时带着 VRAM destination2**，那么 `read_tensor_file_slice_into` 会「只填 CPU、返回 True」，**VRAM 会保持陈旧数据**——这是一个**潜在隐患**。但经逐行核对，**上游所有进入该函数的 CPU destination 都只可能是 hostbuf pin（必带 `_comfy_hostbuf`）或纯 CPU 目标（destination2 为 None）**，所以该隐患在树内不可达。**换句话说：RAM 层是「文件→RAM→VRAM」路径的硬前提；一旦 RAM 层被旁路，上游改用 destination=None 的直通 DMA 来保证显存拿到数据，而不是靠 seek 分支。**

> 备注：`host_buffer.read_file_to_device` / `HostBuffer.read_file_slice` 的内部实现属 `comfy_aimdo` 二进制，本次未查证；上述均为上游调用契约层面的结论。

---

## 6. (e) 结论：三层是真实存在、还是部分休眠？

### 6.1 权威后端判定（谁是真正的 backing store）

| 层 | 是否真实存在 | 是否活跃 | 权威性 |
|---|---|---|---|
| **磁盘 DISK** | ✅ 存在 | ✅ **始终活跃** | ★**权威 backing store**。mmap 只读视图常驻（`utils.py:105-153`），任何被驱逐的权重都能从这里复原 |
| **RAM HOSTBUF** | ✅ 存在 | ⚠️ **条件活跃**（`fast_disk` 决定） | 缓存层。快盘时被旁路（`ops.py:232`）；慢盘时活跃（`ops.py:233` → `memory_management.py:66-75`）。**不是**权威存储，被 `partially_unload_ram` 可随时销毁（`model_patcher.py:2121`） |
| **VRAM** | ✅ 存在 | ✅ 活跃 | 工作集。VBAR（`model_patcher.py:1811/1993`）+ cast buffer（`ops.py:162-165`）；由 `vbar.free_memory` 驱逐（`model_patcher.py:2050`） |

**最终 backing store = 磁盘。** RAM 与 VRAM 都是可驱逐的缓存/工作集；磁盘（mmap safetensors 文件）不可驱逐。

### 6.2 用户观察是否成立？

**「显存不足直接取硬盘、没有内存这一步」——在代码上部分成立，但需要限定条件：**

- ✅ **成立的条件**：模型 **`fast_disk=True`**（快盘自动探测 / `--fast-disk`），且该 VBAR 区域**已有签名**（`ops.py:232` 三条件同上表），系统非 `--high-ram`。此时上游**刻意跳过 RAM pin**，直落 `memory_management.py:57-63 read_file_to_device` = **文件 → 显存**。用户看到的正是这条路径。
- ❌ **不成立的条件**：`fast_disk=False`（慢盘）或 `--high-ram` 或签名尚不存在（首次 stage）。此时 RAM 层**是活跃的**：`pinned_memory.py:69` 建 pin，`memory_management.py:66-75` 走 **文件 → hostbuf(RAM) → 显存**。用户会看到「内存这一步」。

**因此准确表述应为**：当前代码**有**三层，只是**快盘策略把 RAM 层设为「直通旁路」**；用户描述的「没有内存这一步」是 `fast_disk` 策略的表现，而非「RAM 层缺失/从未实现」。**风险点在于：一旦快盘旁路生效，就不存在「显存不足时先退到 RAM」这一中间档——直接从 VRAM 退回磁盘。** 这正是团队此前（任务 #26「F8 全量代码审核」）观测到的「三层卸载机制缺失」的真实边界：**缺的不是 RAM 层的代码，而是「快盘场景下的 RAM 中间卸载档」。**

### 6.3 缺口边界（已实现 / 休眠 / 缺失）

| 能力 | 状态 | 证据 |
|---|---|---|
| 磁盘 backing store | **已实现、始终活跃** | `utils.py:105-153` |
| 文件 → 显存 直通 DMA | **已实现、活跃**（快盘默认） | `memory_management.py:57-63` |
| 文件 → RAM → 显存 三层搬运 | **已实现** | `memory_management.py:66-75`、`ops.py:233-235` |
| RAM hostbuf 缓存池 | **已实现** | `model_patcher.py:1883-1888`、`pinned_memory.py:98-104` |
| RAM 驱逐/替换 | **已实现（非 LRU；尺寸桶+优先级+子池分层）** | `pinned_memory.py:17-46`、`model_management.py:693-732` |
| RAM 水线/预算 | **已实现** | `model_management.py:747-755`、`memory_management.py:178-187` |
| RAM 缓存释放回调（输出缓存腾挪） | **部分休眠**：仅当 `CacheType.RAM_PRESSURE` 才注册；默认 `callback=None` → `extra_ram_release` 恒 0 | `execution.py:747-748`、`memory_management.py:184-187` |
| **「显存不足 → 先退 RAM」的中间卸载档（快盘场景）** | ⚠️ **实质缺失/被旁路** | 取舍点 `ops.py:232`；快盘时永不建 pin |
| VRAM VBAR 工作集 + 驱逐 | **已实现** | `model_patcher.py:1811/1993/2050` |
| `partially_unload_ram`（显式退 RAM） | **已实现**，但主动调用方主要是 RAM 压力与卸载钩子，**非**「显存不足」 | `model_patcher.py:2107`、`model_management.py:682/1485` |

---

## 7. 供复核的证据索引（文件:行 一览）

- 搬运原语与三支分派：`comfy/memory_management.py:18`、`:40-42`、`:44-52`、`:57-63`、`:65-75`、`:77-97`
- 唯一调用者：`comfy/model_management.py:1563`
- `cast_to_gathered`：`comfy/model_management.py:1548-1570`；兜底拷贝 `:1565-1570`
- ops 取权重入口：`comfy/ops.py:340`、`:375`、`:380`、`:383`
- 决策点 `handle_pin`：`comfy/ops.py:228-237`（**取舍行 `:232`**）
- 预取与签名：`comfy/ops.py:128`、`:168-169`、`:177-179`、`:182`、`:187-197`
- pin 建立：`comfy/pinned_memory.py:48`、`:69-126`；`_comfy_hostbuf` 绑定 `:104`；优先级 `:88-91`；抢占 `:17-46`
- 磁盘文件切片：`comfy/utils.py:105-107`、`:150-153`
- RAM 池初始化：`comfy/model_patcher.py:1783-1789`、`:1883-1888`
- RAM 管理 API：`comfy/model_patcher.py:2072`、`:2076`、`:2080`、`:2107`
- 分层驱逐：`comfy/model_management.py:660-662`、`:674-691`、`:693-709`、`:711-726`、`:728-732`、`:734-745`、`:747-755`、`:757-769`
- RAM 水线：`comfy/memory_management.py:173-187`；调用者 `comfy/execution.py:745-748`、`:799-811`、`:841`
- 显存 VBAR：`comfy/model_patcher.py:1803-1817`、`:1992-1994`、`:2045-2070`
- 磁盘/内存取舍策略来源：`comfy/storage.py:83-111`；`fast_disk` 落库 `comfy/model_patcher.py:1790`

---

## 8. 未查证 / 待实机验证项（诚实标注）

1. `comfy_aimdo.host_buffer.read_file_to_device` / `HostBuffer.read_file_slice` / `ModelVBAR.vbar_fault` / `vbar_signature_compare` / `free_memory` / `HostBuffer.truncate` / `extend` 的**二进制内部行为**（是否真实做 DMA、签名语义、是否零拷贝）——本次为纯静态上游审计，**未查证**。
2. `vbar_fault` 返回 `signature` 的确切含义（「曾写入」还是「当前驻留」）——影响 `ops.py:232` 三条件判定的直觉解释，README/源码不可得，**建议实机用日志验证 `fully_faulted` / `resident` 取值**。
3. 本报告未在 XPU 上验证；上述结论与设备无关（CPU/CUDA/XPU 共用同一套 `memory_management`/`pinned_memory` 逻辑），但 **XPU 上 `torch.cuda.cudart().cudaHostRegister` 类调用是否可用**（`pinned_memory.py:59/105/108`、`model_patcher.py:2095`）需结合 Phase1-A 结论评估——这正是「H2D 桩」相关风险所在。

---

*报告完。本次为只读审计，未对任何源文件做出修改。*
