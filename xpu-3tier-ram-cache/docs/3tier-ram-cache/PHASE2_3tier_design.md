# Phase 2 设计方案：XPU 真三层存储（VRAM → RAM → 磁盘）

> 主理人产出 · 2026-10-09 · 目标机型 Intel Arc B580 / torch 2.14.0+xpu / ComfyUI v0.39.0
> 部署基线：comfy_aimdo 0.5.6.dev28 (`gc2cf98fb9`)，fork `ayi3030/comfy-aimdo` main `c2cf98f`

---

## 0. 结论先行（已由主理人逐条读盘/实测复核）

**问题不是"缺三层代码"，而是三层里的 RAM 中间层在 XPU 上被两处开关联手关掉了。**

当前实际数据链路是：

```
磁盘(safetensors mmap) ──64MiB staging ring, 同步 H2D──> 显存(VBAR)
                          ↑ RAM 层完全被旁路
```

### 0.1 两处关闭点（证据）

| # | 位置 | 代码 | 后果 |
|---|---|---|---|
| A | `comfy/model_management.py:1636-1644` | `if is_nvidia() or is_amd():` 内才赋 `MAX_PINNED_MEMORY` | XPU 下 `MAX_PINNED_MEMORY` 恒为 `-1`（:1608 初值） |
| B | `comfy/model_management.py:757-759` | `free_registrations(): if MAX_PINNED_MEMORY <= 0: return False` | `ensure_pin_registerable()` 恒 False |
| C | `comfy/pinned_memory.py:94-96` | `if (not ensure_pin_budget(...) or not ensure_pin_registerable(...)): return _steal_pin(...)` | `pin_memory()` 在此短路 → RAM pin 永不建立 |
| D | `comfy/ops.py:228-237` | `handle_pin()`：pin 为 None 时 `cast_maybe_lowvram_patch(source, None, ..., xfer_dest2=dest)` | 退化为「文件→显存」直通 |
| E | `comfy-aimdo-src` fork `src-xpu/dispatch.cpp:606-610` | `xpu_host_register() { return CUDA_SUCCESS; }`，注释 "XPU phase 1 keeps ComfyUI host pinning disabled" | C 侧也是刻意 no-op |
| F | fork `src-xpu/dispatch.cpp:593-597` | `xpu_host_alloc()` 用 pageable `std::malloc` | 宿主内存非 pinned（曾试 `sycl::malloc_host`，导致 DEVICE_LOST 而回退） |

### 0.2 真机实测（主理人执行，原文）

```
torch 2.14.0+xpu
hasattr cuda True
cuda.is_available() = False
cudart() EXC: AssertionError 'Torch not compiled with CUDA enabled'
xpu.is_available() = True
```

⇒ `pinned_memory.py:59/105/108`、`model_management.py:1692/1722`、`model_patcher.py:2095` 里对
`torch.cuda.cudart().cudaHostRegister/cudaHostUnregister` 的**直接调用在 XPU 上会抛 AssertionError**。
目前之所以没炸，只是因为 C 那条路在更早的 `ensure_pin_registerable` 就短路了 —— **属于"静默失效"，不是"安全"**。

### 0.3 与用户观察的一致性

用户判断「显存不足直接取硬盘、没有内存这一步」——**在代码上完全成立**，且这是 fork 有意的
"XPU phase 1" 简化，不是 bug。

---

## 1. 目标

把 RAM 层打开为**有界、可驱逐的中间缓存**，形成真正的三级取回/回收顺序：

| 方向 | 顺序 |
|---|---|
| 取回（fault 时） | ① RAM 命中 → `RAM → VRAM`；② RAM 未命中 → `磁盘 → VRAM`，并按预算填充 RAM |
| 回收（压力时） | ① VRAM 页驱逐（VBAR unmap）；② RAM 副本仍在则可从 RAM 恢复；③ RAM 预算耗尽 → 既有 steal/bucket 机制驱逐（数据可从磁盘重建） |

---

## 2. 改动清单（「文件 → 位置 → 现状 → 改动 → 风险」）

| # | 文件 | 位置 | 现状 | 改动 | 风险 |
|---|---|---|---|---|---|
| 1 | `comfy/model_management.py` | :1636-1644 | `if is_nvidia() or is_amd():` 赋 `MAX_PINNED_MEMORY` | 追加 XPU 分支，给一个有界 RAM 缓存预算 | 预算过大→宿主内存压力/换页 |
| 2 | `comfy/model_management.py` | 模块级（:1606 附近） | 无"注册能力"概念 | 新增 `HOST_PIN_REGISTRATION_SUPPORTED`（CUDA/ROCm=True，XPU=False） | 遗漏分支→XPU 仍会调 cudaHostRegister→AssertionError |
| 3 | `comfy/model_management.py` | :757-769 (`free_registrations` / `ensure_pin_registerable`) | 预算只服务"注册"语义 | 注册不可用时，把 `MAX_PINNED_MEMORY` 当**纯缓存预算**；函数语义改为"是否在预算内"，不再等同"可注册" | 语义混淆→驱逐阈值错位 |
| 4 | `comfy/model_management.py` | :1692, :1722 (`pin_memory` / `unpin_memory`) | 直接调 `cudaHostRegister/Unregister` | 走守卫 helper；注册不可用时跳过驱动调用、仅记账 | 记账与实际不一致→泄漏 |
| 5 | `comfy/pinned_memory.py` | :59, :105, :108 | 直接调 `torch.cuda.cudart().cudaHostRegister` | 走守卫 helper；XPU 上标记"已缓存但未注册" | 把未注册当已注册→unregister 时 assert |
| 6 | `comfy/model_patcher.py` | :2095 (`unregister_inactive_pins`) | 直接调 `cudaHostUnregister` | 走守卫 helper | 同上 |
| 7 | `comfy/ops.py` | :232 (`handle_pin`) | `if signature is None or not fast_disk or args.high_ram:` | 追加 XPU RAM 缓存开关条件 | 快盘上引入 RAM 占用（预期内） |

**共 4 个文件、7 处改动。全部 XPU 条件门控，CUDA/ROCm/CPU 行为零变化。**

---

## 3. 关键设计点

### 3.1 「注册不可用」≠「缓存不可用」（本方案的核心）
XPU 上 `cudaHostRegister` 不可用（C 侧本就是 no-op），但 `HostBuffer` 依然是**有效的宿主内存缓存**。
必须把两件事解耦：

- **缓存预算**（字节数）→ 参与驱逐决策 ← 保留并新增 XPU 预算
- **注册状态**（能否被驱动固定）→ 只影响拷贝性能，不影响正确性 ← XPU 恒为"未注册"

违反这一点的后果：要么把 XPU 一起关掉（现状），要么调用不存在的 CUDA API 而崩。

### 3.2 为什么不动 `fast_disk`
`fast_disk` 影响面很广，至少四处连带行为：
- 子池选择：`ops.py:189`、`:247`（`weights-fast` / `patches-fast`）
- 跳过注册预算：`model_prefetch.py:115`
- 禁用 loaded 回退：`ops.py:191`、`:249`

改它＝同时改变四类行为。**改为在 `ops.py:232` 新增一个窄条件**，只解锁"快盘也要 RAM 缓存"这一件事。

### 3.3 必须复用的既有机制（禁止另起一套）
- 同尺寸桶 + `bit_reverse_range` 优先级抢占：`pinned_memory.py:17-46`、`:88-91`
- 子池分层驱逐：`model_management.py:693-709`
- `extra_ram_release` / `RAM_CACHE_HEADROOM`：`memory_management.py:173-188`
- 每 prompt 生命周期钩子：`execution.py:748`（设）、`:841`（复位）

### 3.4 预算默认值与逃生舱
- 新增 CLI/环境变量控制 XPU RAM 缓存上限（默认取保守值，建议 `min(ram * 0.25, …)`，具体由实现者按实测给）；
- 支持设 `0` = 关闭（回退到当前"磁盘直通"行为），保证可回滚。

---

## 4. 明确不做（P1 / 需另立任务）

| 项 | 原因 |
|---|---|
| L0 命令表实现真异步 H2D | 当前 SYCL `queue->memcpy().wait_and_throw()` 是同步的；异步化是独立性能议题 |
| 多 queue 并行 / 磁盘读与 H2D 重叠 | 同上 |
| 真 pinned host 内存（`sycl::malloc_host`） | 已在 fork 上试过并回退（DEVICE_LOST），需先查清驱动行为 |
| 修复 upstream `comfy-aimdo-src/src-xpu/dispatch.c` 的 H2D 桩 | 该树**未被编译**（CI overlay 用的是 fork 的 `src-xpu/`），不影响真机 |
| 依赖 L0 on-demand paging 做自动回迁 | L0 无 CUDA VMM page-fault 的用户级等价物；AIMDO 的 `vbar_fault` 是软件 fault，三层必须自己实现 |

---

## 5. 验证判据（Phase 5 用）

1. **门禁不退化**：免 flag 直启仍出现三正标志、两负标志缺席。
2. **RAM 层确已建立**：日志/pin 统计中出现非零的 RAM 缓存占用（`TOTAL_PINNED_MEMORY` 非零，或新增的诊断计数）。
3. **三档命中可区分**：能证明「RAM 命中 → RAM→VRAM」与「RAM 未命中 → 磁盘→VRAM」两条路都被走到（需诊断计数或 trace）。
4. **CUDA/ROCm 零回归**：`HOST_PIN_REGISTRATION_SUPPORTED=True` 路径行为与改动前逐字节一致（代码走查 + 若可能，用 NVIDIA 机复核）。
5. **可回滚**：把 XPU RAM 缓存预算设为 0 后，行为退回当前"磁盘直通"。
6. **超大模型工作流仍出片**：MiniMax H3（20.97GB 扩散 + 14.95GB 文本编码器 vs 11.6GB 显存）跑通并落盘。

---

## 6. 待实现者复核的点（不得跳过）

- `ensure_pin_budget()`（`model_management.py:~740-755`）的完整函数体——确认它在 XPU 上的返回值，以及它是否才是第一道门（若是，改动点 #3 需相应调整）。
- `comfy_aimdo/torch.py::hostbuf_to_tensor` 的语义（pin 张量如何绑定 hostbuf）。
- `pinned_memory.py:52` `get_pin()` 的 `module_pin["registered"]` 语义：改造后 XPU 上应保持 `False` 还是新增独立状态位，需给出明确选择并说明理由。
- `model_patcher.py:2072-2130` 的 `loaded_ram_size` / `pinned_memory_size` / `unregister_inactive_pins` / `partially_unload_ram` 在"未注册"语义下的记账一致性。
