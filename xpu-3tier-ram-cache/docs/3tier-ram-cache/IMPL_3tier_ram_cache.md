# IMPL：XPU 三层存储（打开 RAM 中间缓存层）

> 实现者：comfyui-python-engineer-3 · 2026-10-09
> 依据方案：`aimdo-xpu/PHASE2_3tier_design.md`（主理人产出）
> 前置审计：`aimdo-xpu/RESEARCH_3tier_architecture_audit.md`（本实现的基础）
> 源码根：`E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/ComfyUI/`
> 备份时间戳：`20261009-032052`（备份文件名 `<file>.bak.20261009-032052`）
> 补丁：`aimdo-xpu/comfyui-3tier-ram-cache.patch`
> 本次**未运行** ComfyUI、**未改** `site-packages`、**未改** `comfy-aimdo-src/`。

---

## 0. 一句话说明

把 XPU 上被"两处开关"（`MAX_PINNED_MEMORY` 恒为 -1 + `cudaHostRegister` 在 XPU 上抛异常）联手关掉的 **RAM 中间缓存层**打开，形成真正的三级：取回 `RAM 命中→RAM→VRAM` / `RAM 未命中→磁盘→RAM→VRAM`；回收时 VRAM 驱逐后仍可从 RAM 恢复，RAM 预算耗尽才落磁盘。

**核心解耦（方案 §3.1）**：把"宿主**缓存**预算"与"驱动**注册**能力"彻底分开——注册能力只影响拷贝性能，不影响缓存可用性。

---

## 1. §6 待复核点的复核结果（先给结论）

### 1.1 `ensure_pin_budget()` 完整原文（复核"是否第一道门"）

`comfy/model_management.py:747-755`（改前原文，逐字）：

```python
def ensure_pin_budget(size, evict_active=False, loaded=False):
    if args.high_ram:
        return True
    shortfall = size + max(comfy.memory_management.RAM_CACHE_HEADROOM / 2, 2048 * 1024 ** 2) - comfy.system_memory.virtual_memory_available()
    if shortfall <= 0:
        return True

    to_free = shortfall + PIN_PRESSURE_HYSTERESIS
    return free_pins(to_free, evict_active=evict_active, loaded=loaded) >= shortfall
```

配套常量：`comfy/model_management.py:639`（改前）`PIN_PRESSURE_HYSTERESIS = 256 * 1024 * 1024`。

**复核结论（重要，回答方案 §6 的第 1 问）**：

- `ensure_pin_budget` 的判据是**系统可用内存**（`comfy.system_memory.virtual_memory_available()`），**与 `MAX_PINNED_MEMORY` 无关**。它回答的是"系统 RAM 够不够"，不是"pin 预算够不够"。
- 在 `pinned_memory.py:98-100` 的短路顺序里它虽是**第一个被求值**的，但在 XPU 上**内存充足时恒返回 True**（`shortfall <= 0`），**因此它不是 XPU 上的阻断门**。
- XPU 上真正的阻断门是 `ensure_pin_registerable` → `free_registrations` 的 `if MAX_PINNED_MEMORY <= 0: return False`（`model_management.py:766-767` 改前）。
- **对实现的影响**：无需改动 `ensure_pin_budget`（保持原样）。改动点 #3 的目标正确，落在 `free_registrations` / `ensure_pin_registerable` 上。✔ 与方案一致，无出入。

### 1.2 `comfy_aimdo/torch.py::hostbuf_to_tensor` 语义

路径：`python_embeded/Lib/site-packages/comfy_aimdo/torch.py:43-45`（只读，未改）：

```python
def hostbuf_to_tensor(hostbuf):
    byte_view = (ctypes.c_uint8 * hostbuf.size).from_address(hostbuf.get_raw_address())
    return torch.frombuffer(byte_view, dtype=torch.uint8)
```

**语义**：把 `HostBuffer` 的**原始宿主地址**包成一个 CPU `uint8` 张量视图（`torch.frombuffer`，零拷贝）。
`pinned_memory.py:107` 再切片 `[offset:offset+size]` 得到本次 pin 张量，并在 `:108` 把 `_comfy_hostbuf` 挂到它的 storage 上——这正是 `memory_management.py:65` 能识别"文件→hostbuf→显存"分支的凭据。

**结论**：pin 是**纯 CPU 张量**（`device.type == "cpu"`），满足 `read_tensor_file_slice_into` 对 `destination` 的设备约束（`memory_management.py:47`）。与"是否向驱动注册"完全无关 → 支持本次"注册不可用≠缓存不可用"的解耦。✔

### 1.3 `get_pin()` 的 `registered` 语义：明确选择「XPU 上保持 False」

**选择**：XPU（无注册能力后端）上 `module_pin["registered"]` **忠实保持 `False`**，**不新增**"已缓存"状态位挂在 `module_pin` 上，而是把"已缓存但未注册"的**字节数**记到模块级独立计数器 `TOTAL_PIN_CACHE_MEMORY`。

**理由**：
1. `registered` 在代码里是**多处的行为开关**，不只是标志位——`model_patcher.py:2091`（`unregister_inactive_pins` 以它决定是否真正解注册）、`:2120-2125`（`partially_unload_ram` 以它决定 `do_unregister` 与计数扣减）。若在 XPU 上把它置 True，会让 `unregister_inactive_pins` 去调 `cudaHostUnregister`（XPU 上抛异常）并造成计数错位。
2. 铁律 #1：`registered` 必须忠实表示"驱动已注册"。XPU 上驱动未注册，故为 False。
3. "预算"是**字节数**语义，需要独立累加器；把它塞进 `module_pin` 的布尔位会引入"同一字段两种含义"。故新增 `TOTAL_PIN_CACHE_MEMORY`。✔

### 1.4 `model_patcher.py:2072-2130` 在"未注册"语义下的记账一致性

逐函数核对（改后）：

| 函数 | 与 `registered`/计数相关的行为 | XPU（registered 恒 False）下的表现 | 一致性判定 |
|---|---|---|---|
| `loaded_ram_size()` (:2072) | `pin_state[subset][0].size` 之和（HostBuffer.size） | 计入所有已缓存字节（含未注册） | ✔ 正确：RAM 缓存真实占用 |
| `pinned_memory_size()` (:2076) | `pin_state[subset][3][0]` 之和（per-subset `pinned_size`） | 因 `registered=False`，递增被跳过 → 恒 0 | ✔ 忠实表示"已注册字节=0"（诊断用，不影响正确性） |
| `unregister_inactive_pins()` (:2080) | `:2091 if not registered: continue` | 恒 `continue` → 本函数在 XPU 上是**安全空操作** | ✔ 语义正确：没有注册可解；数据必须保留（本函数不销毁数据） |
| `partially_unload_ram()` (:2107) | `:2121 hostbuf.truncate(..., do_unregister=registered)`；`:2123 if registered: 扣 TOTAL_PINNED_MEMORY/pinned_size` | `do_unregister=False`（无需解注册）→ 真正释放 hostbuf；**新增 `:2126-2129 elif` 扣减 `TOTAL_PIN_CACHE_MEMORY`** | ✔ 修正：原始实现会漏扣（见下） |

**必须修的点（本实现新增，方案未显式列出但属铁律 #1 要求）**：
改前 `partially_unload_ram` 只在 `if registered:` 分支扣计数。XPU 上 `registered=False`，于是 `TOTAL_PINNED_MEMORY`（=0）无需扣，但**新引入的 `TOTAL_PIN_CACHE_MEMORY` 必须扣**，否则预算只增不减、缓存退化为无界。故加 `elif not HOST_PIN_REGISTRATION_SUPPORTED:` 分支。
**CUDA 上该 `elif` 为假**（`not True`→False），语句被完全跳过 → CUDA 路径零变化。✔

**`_steal_pin` 复核**：`pinned_memory.py:17-46` 是**同尺寸**桶内的 pin 交换（`buckets` 以 size 为键），字节数净变化为 0，故**无需**触碰任何计数器。XPU 上 `victim_pin["registered"]`（False）被复制给 `module_pin`（`:35`），语义自洽。✔

---

## 2. 逐处改动（文件:行 + 原文 + 新文 + 理由）

> 行号为**改后**文件行号；"原文"取自备份 `.bak.20261009-032052`。
> 共 **4 文件 / 11 处**（方案 7 处 + 复核后新增 4 处配套）。

### 文件 1：`comfy/model_management.py`

#### 改动 ①（方案 #2）模块级能力开关 + 独立缓存计数器
位置：改后 `:1627-1640`（改前 `:1606-1608`）

原文：
```python
PINNED_MEMORY = {}
TOTAL_PINNED_MEMORY = 0
MAX_PINNED_MEMORY = -1
```

新文：
```python
PINNED_MEMORY = {}
TOTAL_PINNED_MEMORY = 0
# XPU 三层存储：对"无驱动 host 注册能力"的后端（如 Intel XPU）单独维护"已缓存但未注册"
# 的宿主字节数。为什么不复用 TOTAL_PINNED_MEMORY：那个计数器表示"已向驱动成功注册"的字节，
# 注册不可用的后端上它恒为 0，若拿它当缓存预算记账，预算只会永远是 0、缓存将无界增长；
# 反之 CUDA/ROCm 上本计数器恒为 0，保证行为与改前逐字节等价。
TOTAL_PIN_CACHE_MEMORY = 0
MAX_PINNED_MEMORY = -1

# 驱动级 host 注册（cudaHostRegister）能力开关。
# CUDA/ROCm 可用；Intel XPU 构建下 torch.cuda.cudart() 会抛
# AssertionError('Torch not compiled with CUDA enabled')（真机实测），故为 False。
# 关键语义：注册能力只影响拷贝性能，不代表宿主缓存不可用——"注册不可用 ≠ 缓存不可用"。
HOST_PIN_REGISTRATION_SUPPORTED = is_nvidia() or is_amd()
```

理由：XPU 上 `cudaHostRegister` 必抛异常（方案 §0.2 真机实测），需要一个统一开关把"注册"与"缓存"解耦；预算计数需独立字段以免与"已注册字节"混淆（铁律 #1）。`is_nvidia()/is_amd()` 在 `:426/:433` 已定义，此处可用。

---

#### 改动 ②（方案 #3）`free_registrations` / `ensure_pin_registerable` 语义修正
位置：改后 `:757-790`（改前 `:749-769`）

原文：
```python
def ensure_pin_registerable(size, evict_active=True):
    return free_registrations(TOTAL_PINNED_MEMORY + size - MAX_PINNED_MEMORY, evict_active=evict_active)
```

新文：
```python
def total_pin_memory_used():
    """宿主 pin 的预算口径 = 已注册字节 + 已缓存未注册字节。..."""
    return TOTAL_PINNED_MEMORY + TOTAL_PIN_CACHE_MEMORY

def free_registrations(shortfall, evict_active=True):
    if MAX_PINNED_MEMORY <= 0:
        return False
    if shortfall <= 0:
        return True

    shortfall += REGISTERABLE_PIN_HYSTERESIS
    for subsets, current_prompt, active, registrations in registration_eviction_tiers(evict_active):
        shortfall -= free_model_pins(shortfall, subsets, current_prompt, active, registrations=registrations)
    return shortfall <= REGISTERABLE_PIN_HYSTERESIS

def ensure_pin_registerable(size, evict_active=True):
    if MAX_PINNED_MEMORY <= 0:
        # 无预算（含 XPU 缓存被显式关闭）＝不可建立 pin，与原行为一致。
        return False
    if HOST_PIN_REGISTRATION_SUPPORTED:
        # 原语义不变：仅按"已注册"字节做预算，并借 free_registrations 触发解注册驱逐。
        return free_registrations(TOTAL_PINNED_MEMORY + size - MAX_PINNED_MEMORY, evict_active=evict_active)

    # 注册不可用（如 Intel XPU）：MAX_PINNED_MEMORY 退化为"纯宿主缓存预算"。
    # 此时 unregister 路径对从未注册的 pin 是空操作（model_patcher.py:2091 以 registered 为键），
    # 所以超预算必须真正销毁 hostbuf 缓存，走 free_pins（→ partially_unload_ram）。
    shortfall = total_pin_memory_used() + size - MAX_PINNED_MEMORY
    if shortfall <= 0:
        return True
    return free_pins(shortfall + PIN_PRESSURE_HYSTERESIS, evict_active=evict_active) >= shortfall
```

`free_registrations` **函数体未改**（保留其驱逐调用链，满足方案"不要丢掉驱逐链"）；改动集中在 `ensure_pin_registerable`：
- **CUDA 分支**：逐字等价于改前（`free_registrations(TOTAL_PINNED_MEMORY + size - MAX)`）。新增的顶层 `if MAX_PINNED_MEMORY <= 0: return False` 与原路径结果一致（原路径此情况下 `free_registrations` 也返回 False）。
- **XPU 分支**：预算口径换成 `total_pin_memory_used()`，且**必须走 `free_pins`（真正销毁）而非 `free_registrations`（只解注册）**——因为 XPU 上"解注册"是空操作，无法回收缓存。复用既有 `free_pins`→`partially_unload_ram` 机制（铁律 #3）。

---

#### 改动 ③（方案 #1）XPU 有界 RAM 缓存预算
位置：改后 `:1668-1708`（改前 `:1636-1644`）

原文：
```python
if not DISABLE_PINNED_MEMORY:
    if is_nvidia() or is_amd():
        ram = get_total_memory(torch.device("cpu"))
        if WINDOWS:
            MAX_PINNED_MEMORY = ram * 0.40  # Windows limit is apparently 50%
        else:
            swap = 0 if comfy.system_memory.cgroup_memory_limit() is not None else get_disk_swap_total()
            MAX_PINNED_MEMORY = max(ram * 0.40, min(ram * 0.90, ram - 4 * 1024 ** 3, ram + swap - 16 * 1024 ** 3))
        logging.info("Enabled pinned memory {}".format(MAX_PINNED_MEMORY // (1024 * 1024)))
```

新文：新增前置 env 解析函数 + `elif is_intel_xpu():` 分支
```python
def _xpu_ram_cache_gb_from_env():
    """读取环境变量 AIMDO_XPU_RAM_CACHE_GB。..."""
    raw = os.environ.get("AIMDO_XPU_RAM_CACHE_GB", None)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        logging.warning("Invalid AIMDO_XPU_RAM_CACHE_GB=%r; using default XPU RAM cache budget", raw)
        return None

if not DISABLE_PINNED_MEMORY:
    if is_nvidia() or is_amd():
        ... (原文不动) ...
    elif is_intel_xpu():
        _xpu_cache_gb = _xpu_ram_cache_gb_from_env()
        if _xpu_cache_gb is None:
            ram = get_total_memory(torch.device("cpu"))
            _xpu_cache_bytes = int(min(ram * 0.25, 24 * 1024 ** 3))
            logging.info("Enabled XPU RAM cache {} (default = min(ram*0.25, 24GiB))".format(_xpu_cache_bytes // (1024 * 1024)))
        elif _xpu_cache_gb > 0:
            _xpu_cache_bytes = int(_xpu_cache_gb * 1024 ** 3)
            logging.info("Enabled XPU RAM cache {} (AIMDO_XPU_RAM_CACHE_GB={})".format(_xpu_cache_bytes // (1024 * 1024), _xpu_cache_gb))
        else:
            _xpu_cache_bytes = 0
            logging.info("XPU RAM cache disabled (AIMDO_XPU_RAM_CACHE_GB=0); disk passthrough mode")
        if _xpu_cache_bytes > 0:
            MAX_PINNED_MEMORY = _xpu_cache_bytes
```

理由：XPU 原本落到 `else`（无任何赋值）→ `MAX_PINNED_MEMORY` 恒为 -1（关闭点 A）。给一个有界预算并打启动日志（便于验收核证）。取值规则严格按方案：env 优先 / 默认 `min(ram*0.25, 24GiB)` / 显式 `0`=关闭（保持 -1，可回滚）。

---

#### 改动 ④（配套）守卫式注册 helper + XPU 缓存开关谓词
位置：改后 `:1727-1757`（新增，位于 `discard_cuda_async_error` 与 `pin_memory` 之间；行号含 X2 修正后的 docstring）

新文：
```python
def host_register_pin(ptr, size):
    """守卫式 host 注册：把宿主内存固定给驱动（仅供拷贝加速，不影响正确性）。..."""
    if not HOST_PIN_REGISTRATION_SUPPORTED:
        return False
    return torch.cuda.cudart().cudaHostRegister(ptr, size, 1) == 0

def host_unregister_pin(ptr):
    """守卫式 host 解注册：与 host_register_pin 对称。返回 True=已成功解除注册。"""
    if not HOST_PIN_REGISTRATION_SUPPORTED:
        return False
    return torch.cuda.cudart().cudaHostUnregister(ptr) == 0

def xpu_ram_cache_enabled():
    """XPU 三层存储：RAM 中间缓存是否启用（当前计算设备是 XPU 且预算 > 0）。

    供 ops.py 的 handle_pin 使用——让"快盘 fast_disk"场景也能建立 RAM pin。

    必须判"当前选中的计算设备"而非"XPU 是否可用"：
    is_intel_xpu() 只反映 XPU 可用性；而 get_torch_device() 里 directml 分支
    （model_management.py:198-200）是它的最前置短路——在 --directml 下即使 XPU 可用，
    实际计算设备也不是 XPU（:206 的 is_intel_xpu() 分支根本轮不到）。
    若用 is_intel_xpu()，就会在 DirectML 上因 MAX_PINNED_MEMORY>0 而误触发 ops.py:232 的 or，
    去建 RAM pin，破坏"CUDA/DirectML 零变化"铁律。
    故此处一律以 get_torch_device().type 为准。请勿"简化"回 is_intel_xpu()。
    """
    return get_torch_device().type == "xpu" and MAX_PINNED_MEMORY > 0
```

理由：把 `cudart()` 调用收敛到唯一入口，`HOST_PIN_REGISTRATION_SUPPORTED` 为常量 True 时路径与改前逐字节等价（CUDA 零变化）；XPU 上直接短路返回 False，**永不触碰 `cudart()`**。

> **Round-2 修正（X2）**：初版此处为 `return is_intel_xpu() and MAX_PINNED_MEMORY > 0`，被 Phase 4 审查判定**判据过松**——`is_intel_xpu()` 是"设备可用性"而非"当前计算设备"。已改为以 `get_torch_device().type == "xpu"`（= 当前选中设备）判断。`get_torch_device()`（`model_management.py:195`）所有返回分支都是 `torch.device`，故 `.type` 可用；CUDA/CPU 模式下其 `.type` 分别为 `"cuda"`/`"cpu"`，条件恒 False。
>
> **Round-3 修正（X3，纯注释）**：X2 生效后审查官复核指出——初版 docstring 里写的场景「**混合机跑 CUDA** 时 `is_intel_xpu()` 仍可能为 True」**在真实代码里不可达**（GPU 路径上 `:206` 先判 `is_intel_xpu()`，为真即返回 `xpu`，轮不到 cuda 分支）。真正的分歧点是 **`--directml`**：`:198-200` 的 `if directml_enabled: return directml_device` 是**最前置短路**，此时 XPU 可能可用但实际设备是 DirectML。审查官脚本枚举 384 个设备状态：非 directml 状态新旧**完全一致（差异 0）**，唯一差异的 32 个状态**全部是 `--directml` 且 XPU 可用**。docstring 已据此改写（删除不成立的场景、换成 DirectML 短路理由）。详见 §10。

---

#### 改动 ⑤（方案 #4）`pin_memory` 守卫化
位置：改后 `:1752-1797`（改前 `:1663-1700`）

原文（关键段）：
```python
    if torch.cuda.cudart().cudaHostRegister(ptr, size, 1) == 0:
        PINNED_MEMORY[ptr] = size
        TOTAL_PINNED_MEMORY += size
        return True
    else:
        logging.warning("Pin error.")
        discard_cuda_async_error()

    return False
```

新文：
```python
    if host_register_pin(ptr, size):
        PINNED_MEMORY[ptr] = size
        TOTAL_PINNED_MEMORY += size
        return True

    if HOST_PIN_REGISTRATION_SUPPORTED:
        # CUDA/ROCm：注册失败即放弃，与原行为一致（告警 + 吞掉异步错误）。
        logging.warning("Pin error.")
        discard_cuda_async_error()
        return False

    # 注册不可用（XPU）：不放弃缓存，仅按"已缓存未注册"记账（独立计数器）。
    PINNED_MEMORY[ptr] = size
    TOTAL_PIN_CACHE_MEMORY += size
    return True
```
另在函数头补 `global TOTAL_PIN_CACHE_MEMORY`。

理由：CUDA 路径逐字等价（原 `==0` 成功分支 → helper True 分支；原 else → `HOST_PIN_REGISTRATION_SUPPORTED` 分支）。XPU 上不再返回 False，而是纳入宿主缓存并单独记账。

---

#### 改动 ⑥（方案 #4）`unpin_memory` 守卫化
位置：改后 `:1799-1833`（改前 `:1702-1730`）

原文（关键段）：
```python
    if torch.cuda.cudart().cudaHostUnregister(ptr) == 0:
        size = PINNED_MEMORY.pop(ptr)
        TOTAL_PINNED_MEMORY -= size
        return True
    else:
        logging.warning("Unpin error.")
        discard_cuda_async_error()

    return False
```

新文：
```python
    if host_unregister_pin(ptr):
        size = PINNED_MEMORY.pop(ptr)
        TOTAL_PINNED_MEMORY -= size
        return True

    if HOST_PIN_REGISTRATION_SUPPORTED:
        logging.warning("Unpin error.")
        discard_cuda_async_error()
        return False

    # 注册不可用：没有驱动状态可撤销，仅清理缓存记账。
    size = PINNED_MEMORY.pop(ptr)
    TOTAL_PIN_CACHE_MEMORY = max(0, TOTAL_PIN_CACHE_MEMORY - size)
    return True
```
另在函数头补 `global TOTAL_PIN_CACHE_MEMORY`。

理由：与 `pin_memory` 对称，保证记账闭合（铁律 #1）。

---

### 文件 2：`comfy/pinned_memory.py`

#### 改动 ⑦（方案 #5）`get_pin` 早退条件扩展 + 注册守卫
位置：改后 `:52-71`（改前 `:52-67`）

原文：
```python
    if pin is None or module_pin["registered"] or comfy.model_management.DISABLE_PINNED_MEMORY:
        return pin
    ...
    if torch.cuda.cudart().cudaHostRegister(pin.data_ptr(), size, 1) != 0:
        comfy.model_management.discard_cuda_async_error()
        return pin
```

新文：
```python
    if (pin is None or module_pin["registered"] or comfy.model_management.DISABLE_PINNED_MEMORY
            or not comfy.model_management.HOST_PIN_REGISTRATION_SUPPORTED):
        # 注册不可用的后端（如 XPU）：pin 一旦建立即可直接使用，无需也无法做驱动注册。
        # 早退可避免走到下面的 cudaHostRegister（XPU 上会抛 AssertionError）；
        # 此类后端上 registered 恒为 False，故这里按"未注册"语义返回可用 pin。
        return pin
    ...
    if not comfy.model_management.host_register_pin(pin.data_ptr(), size):
        comfy.model_management.discard_cuda_async_error()
        return pin
```

理由：这是**关键早退点**——XPU 上若不放行早退，`get_pin` 会走到 `cudaHostRegister` 抛异常。放行后 pin 仍被正常返回给调用方使用（`registered` 保持 False）。CUDA 上 `not HOST_PIN_REGISTRATION_SUPPORTED` 恒 False，条件不变。

---

#### 改动 ⑧（方案 #5）`pin_memory` 跳过注册重试 + 记账分流
位置：改后 `:104-137`（改前 `:100-126`）

原文（关键段）：
```python
        pin.untyped_storage()._comfy_hostbuf = hostbuf
        if torch.cuda.cudart().cudaHostRegister(pin.data_ptr(), size, 1) != 0:
            comfy.model_management.discard_cuda_async_error()
            comfy.model_management.free_registrations(size, evict_active=not fast)
            if torch.cuda.cudart().cudaHostRegister(pin.data_ptr(), size, 1) != 0:
                comfy.model_management.discard_cuda_async_error()
                del pin
                hostbuf.truncate(offset, do_unregister=False)
                return _steal_pin(module, stack, buckets, size, priority, subset)
    except RuntimeError:
        ...
    module_pin["pin"] = pin
    stack.append((module, offset))
    module_pin["registered"] = True
    module_pin["stack_index"] = len(stack) - 1
    stack_split[0] = max(stack_split[0], module_pin["stack_index"])
    comfy.model_management.TOTAL_PINNED_MEMORY += size
    pinned_size[0] += size
    _add_to_bucket(module, module_pin, buckets, size, priority)
    return True
```

新文：
```python
        pin.untyped_storage()._comfy_hostbuf = hostbuf
        if comfy.model_management.HOST_PIN_REGISTRATION_SUPPORTED:
            # 仅在有驱动注册能力的后端（CUDA/ROCm）才尝试注册；
            # XPU 上注册不可用，跳过注册与两次重试，直接进入下面的记账分支。
            if not comfy.model_management.host_register_pin(pin.data_ptr(), size):
                comfy.model_management.discard_cuda_async_error()
                comfy.model_management.free_registrations(size, evict_active=not fast)
                if not comfy.model_management.host_register_pin(pin.data_ptr(), size):
                    comfy.model_management.discard_cuda_async_error()
                    del pin
                    hostbuf.truncate(offset, do_unregister=False)
                    return _steal_pin(module, stack, buckets, size, priority, subset)
    except RuntimeError:
        ...
    module_pin["pin"] = pin
    stack.append((module, offset))
    if comfy.model_management.HOST_PIN_REGISTRATION_SUPPORTED:
        module_pin["registered"] = True
        comfy.model_management.TOTAL_PINNED_MEMORY += size
    else:
        # 注册不可用：registered 忠实保持 False（XPU 恒未注册），
        # 预算占用记到独立的"已缓存未注册"计数器，避免与"已注册字节"语义混淆。
        module_pin["registered"] = False
        comfy.model_management.TOTAL_PIN_CACHE_MEMORY += size
    module_pin["stack_index"] = len(stack) - 1
    stack_split[0] = max(stack_split[0], module_pin["stack_index"])
    pinned_size[0] += size
    _add_to_bucket(module, module_pin, buckets, size, priority)
    return True
```

理由：
- **注册段整体门控**：XPU 上**完全跳过**注册与两次重试，**不走 `_steal_pin` 失败回退**（方案明确要求），继续执行 `:125-139` 的记账与入桶。这正是让 `pin_memory` 在 XPU 上"建立缓存"的关键。
- **记账分流**：XPU 上 `registered=False` + `TOTAL_PIN_CACHE_MEMORY` 累加（而非 `TOTAL_PINNED_MEMORY`）。
- CUDA 上两个 `if HOST_PIN_REGISTRATION_SUPPORTED` 均恒真 → 与改前逐字等价。

---

### 文件 3：`comfy/model_patcher.py`

#### 改动 ⑨（方案 #6）`unregister_inactive_pins` 解注册守卫
位置：改后 `:2095-2097`（改前 `:2095-2097`）

原文：
```python
                if torch.cuda.cudart().cudaHostUnregister(pin.data_ptr()) != 0:
                    comfy.model_management.discard_cuda_async_error()
                    continue
```

新文：
```python
                if not comfy.model_management.host_unregister_pin(pin.data_ptr()):
                    comfy.model_management.discard_cuda_async_error()
                    continue
```

理由：`not (==0)` 等价于 `!= 0` → CUDA 逐字等价。XPU 上因 `:2091 if not registered: continue` 恒提前跳过，**根本不会到达此处**（即不会误调 `cudaHostUnregister`）。守卫是纵深防御。

---

#### 改动 ⑩（配套，铁律 #1）`partially_unload_ram` 缓存计数扣减
位置：改后 `:2120-2129`（改前 `:2120-2125`）

原文：
```python
                registered = module_pin["registered"]
                hostbuf.truncate(offset, do_unregister=registered)
                stack_split[0] = min(stack_split[0], len(stack) - 1)
                if registered:
                    comfy.model_management.TOTAL_PINNED_MEMORY = max(0, comfy.model_management.TOTAL_PINNED_MEMORY - size)
                    pinned_size[0] = max(0, pinned_size[0] - size)
```

新文：追加一个 `elif`
```python
                registered = module_pin["registered"]
                hostbuf.truncate(offset, do_unregister=registered)
                stack_split[0] = min(stack_split[0], len(stack) - 1)
                if registered:
                    comfy.model_management.TOTAL_PINNED_MEMORY = max(0, comfy.model_management.TOTAL_PINNED_MEMORY - size)
                    pinned_size[0] = max(0, pinned_size[0] - size)
                elif not comfy.model_management.HOST_PIN_REGISTRATION_SUPPORTED:
                    # 注册不可用（XPU）：hostbuf 已释放，需同步扣减"已缓存未注册"预算计数，
                    # 否则 RAM 预算只增不减、缓存层会退化为无界。
                    comfy.model_management.TOTAL_PIN_CACHE_MEMORY = max(0, comfy.model_management.TOTAL_PIN_CACHE_MEMORY - size)
```

理由：见 §1.4。`TOTAL_PIN_CACHE_MEMORY` 是本次引入的预算累加器，其**减项**必须与 `hostbuf.truncate` 同点发生，否则预算泄漏。CUDA 上 `elif` 为假、语句被跳过（零变化）。

---

### 文件 4：`comfy/ops.py`

#### 改动 ⑪（方案 #7）`handle_pin` 放开 XPU RAM 缓存
位置：改后 `:232-239`（改前 `:232-234`）

原文：
```python
            if signature is None or not fast_disk or args.high_ram:
                comfy.pinned_memory.pin_memory(m, subset=subset, size=size)
                pin = comfy.pinned_memory.get_pin(m, subset=subset)
```

新文：
```python
            if (signature is None or not fast_disk or args.high_ram
                    or comfy.model_management.xpu_ram_cache_enabled()):
                # XPU 三层存储：即使判定为快盘（fast_disk）也要建立 RAM pin，
                # 从而形成 VRAM 驱逐后仍可从 RAM 恢复的中间层。
                # 该条件在 CUDA/ROCm/CPU 上恒为 False（xpu_ram_cache_enabled 内部门控），
                # 故不改变其既有的"快盘直通"行为。
                comfy.pinned_memory.pin_memory(m, subset=subset, size=size)
                pin = comfy.pinned_memory.get_pin(m, subset=subset)
```

理由：这是"让快盘场景也走 RAM 缓存"的**唯一入口**（方案 §3.2）。**未改 `fast_disk` 本身**（其牵涉 `ops.py:189/247` 子池、`model_prefetch.py:115`、`ops.py:191/249` 四处行为，按方案保持不动）。XPU 上 pin 落在 `weights-fast`/`patches-fast` 子池（因 fast_disk 仍为真），这是既有分层驱逐机制内的一等公民，复用即可（铁律 #3）。

---

## 3. 数据流（改动后的实际路径）

```
[磁盘] safetensors mmap ──(_comfy_tensor_file_slice)──┐
                                                      │  read_tensor_file_slice_into
      ┌── RAM 未命中：memory_management.py:66-75 hostbuf.read_file_slice ──┐
      │     文件 → hostbuf(RAM) → 显存                                     │
      └── RAM 命中：pin 无 file_slice → 返回 False                          │
             → model_management.py:1568 dest.copy_() = RAM → VRAM          │
                                                      ▼
                                            [VRAM] VBAR / cast buffer
```

- **取回（fault）**：`ops.py:190 get_pin` 命中→`RAM→VRAM`；未命中→`ops.py:232` 建 pin→`文件→RAM→VRAM`（一次调用同时填两层）。
- **回收（压力）**：`vbar.free_memory` 驱逐 VRAM；RAM 副本仍在→下次 fault 从 RAM 恢复；RAM 超预算→`ensure_pin_registerable`→`free_pins`→`partially_unload_ram` 销毁 hostbuf（数据可从磁盘重建）。

---

## 4. 自查三件事（我的角色硬标准）

| 检查 | 结论 | 证据 |
|---|---|---|
| **无 Intel 环境是否仍可导入** | ✔ 是 | 新增判断全部走 `HOST_PIN_REGISTRATION_SUPPORTED`（CUDA/ROCm/无 GPU 上为常量/在 XPU 上才是 False）与 `is_intel_xpu()`；XPU 专用分支只在 `else`/`elif` 内。所有四个文件 `py_compile` 通过 |
| **原 CUDA 路径是否行为不变** | ✔ 是 | ① `host_register_pin`/`host_unregister_pin` 在 `HOST_PIN_REGISTRATION_SUPPORTED=True` 时逐字返回 `cudart()==0`，与改前 `==0`/`!=0` 判定等价；② `ensure_pin_registerable` 的 CUDA 分支逐字等价；③ 所有新增 `if HOST_PIN_REGISTRATION_SUPPORTED` 在 CUDA 上恒真 → 走原分支；④ `model_patcher.py` 新增 `elif not HOST_..._SUPPORTED` 在 CUDA 上恒假 → 语句被跳过；⑤ `ops.py` 新增 `or xpu_ram_cache_enabled()` 在 CUDA 上恒 False |
| **dtype 回退链是否闭合** | ✔ 与本改动无关 | 本次不触碰 dtype/量化逻辑；`QuantizedTensor` 分支（`memory_management.py:20-34`）未改 |

---

## 5. 与方案有出入之处及原因

1. **新增了方案未列出的 2 处配套改动**（§2 改动 ④ 的 `xpu_ram_cache_enabled()` 谓词、改动 ⑩ 的 `partially_unload_ram` 计数扣减），另加 §2 改动 ② 的 `total_pin_memory_used()` helper。
   - 原因：方案给出的是"目标与约束"，未穷举实现细节。改动 ⑩ 是**铁律 #1 的必然推论**——既然引入独立预算计数器，就必须在唯一的 hostbuf 销毁点同步扣减，否则 RAM 缓存退化为无界（方案 §6 第 4 项也是要求核对这一点）。改动 ④ 的谓词是为让 `ops.py` 保持单行可读、且把"是否**当前计算设备**为 XPU + 预算>0"的判据集中在 model_management 内（避免 ops.py 里散落设备判断细节；该判据已按 Round-2 X2 修正为 `get_torch_device().type == "xpu"`，见 §9.1）。
2. **`ensure_pin_budget` 未改动**：方案 §6 第 1 问担心"它才是第一道门"。复核结论是它判据为**系统可用内存**、在内存充足时恒 True，**不是 XPU 上的阻断门**，故不动（改它会偏离原语义、影响 CUDA）。
3. **`free_registrations` 函数体保持不变**（方案 #3 标题含该函数）。只改 `ensure_pin_registerable` 的调用语义——因为 `free_registrations` 的"按预算驱逐并触发解注册"逻辑在 CUDA 上仍是正确的，且方案明确"不要丢掉驱逐调用链"。XPU 的驱逐改由 `free_pins`（真正销毁）承担。
4. **XPU 上 pin 仍进 `*-fast` 子池**（因 `fast_disk` 未改）：方案 §3.2 明确禁止动 `fast_disk`。副作用是 XPU 的 RAM pin 属"最先被驱逐"层，属预期。
5. **`registered` 状态选择**：按 §1.3，选择在 XPU 上**不新增 `module_pin` 状态位**，仅新增模块级字节计数器。与方案"需给出明确选择并说明理由"的要求对应。

---

## 6. 自证材料

### 6.1 `py_compile` 结果

命令：
```
<python_embeded>/python.exe -m py_compile \
  comfy/model_management.py comfy/pinned_memory.py comfy/model_patcher.py comfy/ops.py
```
结果：**PY_COMPILE_OK**（4/4 通过，无输出）。

### 6.2 改前 / 改后 md5（每个改动文件）

| 文件 | 改前 md5 | 改后 md5（最终，含 X2+X3 修正） |
|---|---|---|
| `comfy/model_management.py` | `162840e344dcca56cda909d9a021c78f` | `0f7c056c0d45645f81b4256f3d56703a` |
| `comfy/pinned_memory.py` | `3f831a750fcfa3e419d3377cb79e4910` | `c629da9c2089c7c75d3b53386f49fdc9` |
| `comfy/model_patcher.py` | `51f66e21cc08f90408afe03090a4c886` | `d523b099933dad858e3a9e73a567c730` |
| `comfy/ops.py` | `9e3f9620541118ee30479660a9191557` | `96090a01b13e1b81907152324b6430a0` |

> X2（判据收紧）与 X3（docstring 场景修正）都只触及 `model_management.py` 一处（`xpu_ram_cache_enabled`），
> 故其余 3 个文件 md5 不变。**`0f7c056c…` 即当前工作区 `model_management.py` 的最终值。**

### 6.3 守卫覆盖静态核验

- 全仓 `torch.cuda.cudart()` 直接调用现在**仅存在于** `model_management.py:1736`（`host_register_pin`）与 `:1742`（`host_unregister_pin`）两个守卫 helper 内（其余为注释/docstring）。
- 4 个改动文件中已无裸 `cudaHostRegister`/`cudaHostUnregister` 调用。

### 6.4 补丁可重放性 + 行尾（**Round-2 更正**）

`aimdo-xpu/comfyui-3tier-ram-cache.patch`（306 行，unified diff）。

#### 6.4.1 行尾事实（字节级实测，已更正）

> ⚠️ **本文档初版曾错误地写"4 个文件为纯 LF"，现更正如下。** 初版结论源于 MSYS 下
> `sed`/`cat -A` 的**文本模式**会自动吞掉 `\r`，导致观察失真；改用 Python 以二进制读取才得到真相。

**字节级统计（`open(path,'rb')`，改后工作区）**：

| 文件 | CRLF | bare-LF | bare-CR | 判定 |
|---|---|---|---|---|
| `comfy/model_management.py` | 2318 | 0 | 0 | **PURE_CRLF** |
| `comfy/pinned_memory.py` | 139 | 0 | 0 | **PURE_CRLF** |
| `comfy/model_patcher.py` | 2190 | 0 | 0 | **PURE_CRLF** |
| `comfy/ops.py` | 1825 | 0 | 0 | **PURE_CRLF** |

（改前的 `.bak.20261009-032052` 同样全部为 PURE_CRLF。）

**补丁行尾**：**MIXED** —— `CRLF=284`（全部内容行）+ `bare-LF=22`（**恰好是 diff 元数据行**：`---` / `+++` / `@@` 及 hunk 头）。已逐行核对：22 个 bare-LF 行号全部落在元数据行上，内容行（context / `-` / `+`）**无一例外是 CRLF**。

#### 6.4.2 部署方式（强制）

**已实测**：在临时目录用**改前备份**还原为 `comfy/*.py` 后：
- `git apply -p0 --check` → **通过**；
- `patch -p0 --binary -i <patch>` → 干净应用，结果与当前工作区 **4/4 字节级 IDENTICAL**；
- `patch -p0`（**不加 `--binary`**）→ hunk 全部 FAIL（因内容行 CRLF 与 patch 的文本模式处理冲突）——这是**预期**，见下。

**规定命令**（在 ComfyUI 根目录执行）：

```
git apply -p0 <patch>              # 首选
patch -p0 --binary -i <patch>      # 等价
```

**禁止**：
- ❌ 禁止使用**不带 `--binary`** 的 `patch`（会 hunk 全 FAIL，且可能产生半应用状态）；
- ❌ **禁止对源文件或补丁做 LF/CRLF 行尾转换**。4 个源文件是 CRLF，若被转成 LF，虽然 `py_compile` 仍通过，但会：
  ① 与仓库其余文件的行尾约定不一致；② 使本补丁的上下文行不再匹配（补丁按 CRLF 内容行生成）；③ 破坏"逐字节 IDENTICAL"验收。
  若必须转行尾，应视为**独立变更**并重新生成补丁，不可在本补丁流程中顺手做。

#### 6.4.3 唯一验收判据

应用补丁后，**4 个文件的 md5 必须逐一等于**：

```
0f7c056c0d45645f81b4256f3d56703a  comfy/model_management.py
c629da9c2089c7c75d3b53386f49fdc9  comfy/pinned_memory.py
d523b099933dad858e3a9e73a567c730  comfy/model_patcher.py
96090a01b13e1b81907152324b6430a0  comfy/ops.py
```

（校验命令：`cd <ComfyUI>/comfy && md5sum model_management.py pinned_memory.py model_patcher.py ops.py`。）

#### 6.4.4 正向连带改动说明（预期耦合，非 bug）

改动 ③ 使 `MAX_PINNED_MEMORY` 在 XPU 上由 `-1` 变为正数，于是
`pinned_hostbuf_size(size) = max(0, int(min(size, MAX_PINNED_MEMORY) * 2))`（`model_management.py:1712-1715`）
在 XPU 上由恒 `0` 变为 `min(model_size, 预算) * 2`。
该值经 `model_patcher.py:1882` 传给 `HostBuffer(..., hostbuf_size)`（即 `HostBuffer.max_mmap_size`），
**从 0 变为正数**——这是 RAM 层**可增长的必要前提**（max_mmap_size=0 时 hostbuf 无法 extend）。
属**预期耦合**：它是"XPU 预算分支"生效的直接结果，不是副作用 bug。Phase 5 若观测到 `HostBuffer`
容量非零，属正常。

---

## 7. 需实机验证的项（交 Phase 5）

1. `HostBuffer.read_file_slice(..., device_ptr=..., device=...)` 在 XPU 上是否真正完成 H2D（C 侧 pageable malloc + 同步 memcpy）；这是"文件→RAM→VRAM"能否落地的**最硬前提**。
2. `TOTAL_PIN_CACHE_MEMORY` / `loaded_ram_size()` 是否出现非零（判据 #2）；两档命中是否可区分（判据 #3）。
3. `AIMDO_XPU_RAM_CACHE_GB=0` 回滚后，行为是否退回"磁盘直通"（判据 #5）。
4. CUDA/ROCm 机器上 `HOST_PIN_REGISTRATION_SUPPORTED=True` 路径与改前逐字节一致（判据 #4），建议走查 + 可能的话上 NVIDIA 机复核。
5. 超大模型（MiniMax H3）工作流仍出片（判据 #6）。

---

## 8. 变更文件清单

| # | 文件 | 改动点 | 备份 |
|---|---|---|---|
| 1 | `comfy/model_management.py` | ① 能力开关+计数器 ② 预算语义修正 ③ XPU 预算分支 ④ 守卫 helper+谓词 ⑤ `pin_memory` 守卫 ⑥ `unpin_memory` 守卫 | `model_management.py.bak.20261009-032052` |
| 2 | `comfy/pinned_memory.py` | ⑦ `get_pin` 早退+守卫 ⑧ `pin_memory` 跳过注册+记账分流 | `pinned_memory.py.bak.20261009-032052` |
| 3 | `comfy/model_patcher.py` | ⑨ 解注册守卫 ⑩ `partially_unload_ram` 计数扣减 | `model_patcher.py.bak.20261009-032052` |
| 4 | `comfy/ops.py` | ⑪ `handle_pin` 放开 XPU RAM 缓存 | `ops.py.bak.20261009-032052` |

> 说明：本实现**未**修改 `comfy-aimdo-src/`（未编译的上游树）与 `site-packages`（含 `comfy_aimdo/torch.py`，仅只读查阅）。

---

## 9. Round-2 修正记录（Phase 4 审查返工）

Phase 4 审查总体判定「可进入真机验证」，另要求 2 项修正（1 项代码 + 1 项文档），已全部完成。

### 9.1 必修 1（代码 / X2）：`xpu_ram_cache_enabled()` 判据收紧

- **位置**：`comfy/model_management.py:1744`（函数体）。
- **问题**：初版 `return is_intel_xpu() and MAX_PINNED_MEMORY > 0`。`is_intel_xpu()` 是「XPU 是否可用」，**不是**「当前选中的计算设备是不是 XPU」。混合机（Intel 核显 + NVIDIA/AMD 独显）上即使 ComfyUI 跑 CUDA 模式，`cpu_state == GPU and xpu_available` 仍可能为 True；又因 `is_nvidia()/is_amd()` 成立使 `MAX_PINNED_MEMORY > 0` ⇒ 本函数返回 True ⇒ `ops.py:232` 的 `or` 在 CUDA 上被触发 ⇒ 快盘场景在 CUDA 上也去建 RAM pin ⇒ **违反"CUDA 零变化"铁律**。
- **修改**（采纳审查官方案）：

```python
# 改前
    return is_intel_xpu() and MAX_PINNED_MEMORY > 0
# 改后
    return get_torch_device().type == "xpu" and MAX_PINNED_MEMORY > 0
```

- **前置确认（已做到）**：`get_torch_device()`（`model_management.py:195`）所有返回分支均返回 `torch.device`（`"cpu"`/`"mps"`/`"xpu"`/`"npu"`/`"mlu"`/`cuda`），故 `.type` 恒可用、不会 None。
- **注释**：函数 docstring 已写明「必须判"当前计算设备"而非"XPU 是否可用"」+「请勿简化回 `is_intel_xpu()`」，防止后人改回。**（该 docstring 的场景描述在 Round-3 被进一步更正，见 §10。）**
- **CUDA/CPU 侧效果**：CUDA 模式 `get_torch_device()` 返回 `torch.device("cuda", idx)` → `.type == "cuda"` ≠ `"xpu"` → 条件恒 False；CPU/MPS 同理。XPU 模式返回 `torch.device("xpu", idx)` → 条件由预算决定。**零回归保证比初版更强。**

### 9.2 必修 2（文档）：§6.4 行尾结论更正

初版 §6.4 误称「4 个文件为纯 LF」（源于 MSYS 下 `sed`/`cat -A` 文本模式吞 `\r`）。已按**字节级实测**更正为：文件与补丁**内容行均为 CRLF**，补丁仅元数据行为 LF；并写明部署命令与"禁止转 LF"。详见 §6.4。**代码/补丁本身无需改动**（重放一直是通的，只是文档理由错了）。

### 9.3 重新生成与复验结果（本回合实测）

- 补丁已重新生成：`aimdo-xpu/comfyui-3tier-ram-cache.patch`，**306 行**，MIXED（CRLF=284 内容行 + bare-LF=22 元数据行）。
- `py_compile` 4/4 → **PY_COMPILE_OK**。
- 行尾复验：4 源文件仍 **PURE_CRLF**（X2 编辑未引入 bare-LF）。
- 重放复验：临时目录以改前备份还原后，
  `git apply -p0 --check` → **OK**；`patch -p0 --binary` → **OK** 且 4/4**字节级 IDENTICAL**；
  `patch -p0`（无 `--binary`）→ **如期失败**（已在文档中禁止）。
- 新 md5（X2 后中间值，已由 §10 的 X3 结果取代）：`model_management 0a9040f2…`、`pinned_memory c629da9c…`、`model_patcher d523b099…`、`ops 96090a01…`。

### 9.4 部署清单（交 intel-xpu-adapter / Phase 5）

1. 备份现有 4 文件（`<file>.bak.<新时间戳>`）。
2. `cd <ComfyUI>` → `git apply -p0 <patch>`（或 `patch -p0 --binary -i <patch>`）。
   **禁止**无 `--binary` 的 `patch`；**禁止**转行尾。
3. 校验（唯一判据）：`cd comfy && md5sum model_management.py pinned_memory.py model_patcher.py ops.py`
   必须等于 §6.4.3 的 4 个值。
4. **不要**手工编辑 `xpu_ram_cache_enabled()` 的判据（须保持 `get_torch_device().type == "xpu"`）。
5. 回滚：还原 §1 步的 `.bak`，或用 `git apply -R -p0 <patch>`。

---

## 10. Round-3 修正记录（X3：docstring 事实澄清，纯注释）

审查官判定 **`X2 已闭合`**，但复核带出一条**事实澄清**：X2 后 docstring 里的场景描述不准确，team-lead 要求落到注释里。**这是本轮最后一处改动。**

### 10.1 要澄清的事实

初版（X2 后）docstring 写的场景是：「**混合机（Intel 核显 + NVIDIA/AMD 独显）跑 CUDA 模式**时 `is_intel_xpu()` 仍可能为 True」。

**该场景在真实代码里不可达。** 逐行读 `get_torch_device()`（`model_management.py:195-213`）：

```python
201    if cpu_state == CPUState.MPS:  return torch.device("mps")
203    if cpu_state == CPUState.CPU:  return torch.device("cpu")
205    else:
206        if is_intel_xpu():                       # ← GPU 路径上先判 XPU
207            return torch.device("xpu", torch.xpu.current_device())
208        elif is_ascend_npu():  ...
210        elif is_mlu():         ...
212        else:
213            return torch.device(torch.cuda.current_device())
```

GPU 路径上 `is_intel_xpu()` 是**第一个**判断；它为真就返回 `xpu`，**根本轮不到 cuda 分支**。故在非 DirectML 的 GPU 路径上，`get_torch_device().type == "xpu"` ⟺ `is_intel_xpu()`，旧写法并不会出错。

**真正的分歧点是 `--directml`**：`:198-200` 的 `if directml_enabled: return directml_device` 是**最前置短路**。此时 `is_intel_xpu()` 可能为 True，而当前计算设备是 DirectML ⇒ 旧写法会在 DirectML 上因 `MAX_PINNED_MEMORY>0` 误触发 `ops.py:232` 的 `or`，去建 RAM pin。

审查官脚本枚举 **384 个设备状态**：非 directml 状态**新旧完全一致（差异 = 0）**；唯一差异的 **32 个状态全部是 `--directml` 且 XPU 可用**。

### 10.2 改动（仅 docstring）

`comfy/model_management.py:1745-1756`，`xpu_ram_cache_enabled()` 的 docstring：

- **删除**「混合机跑 CUDA」这一不成立场景；
- **换成**准确理由：`is_intel_xpu()` 只反映「XPU 是否**可用**」；`get_torch_device()` 里 `directml_enabled` 是最前置短路（`:198-200`），故 `--directml` 下即使 XPU 可用、实际计算设备也不是 XPU；
- 破坏的表述由「"CUDA 零变化"铁律」改为「"CUDA/DirectML 零变化"铁律」；
- **保留**结论句：「一律以 `get_torch_device().type` 为准」+「**请勿"简化"回 `is_intel_xpu()`**」。

**函数体 `return` 语句未动**（仍是 `get_torch_device().type == "xpu" and MAX_PINNED_MEMORY > 0`）——本轮是纯注释改动，**行为零变化**。

### 10.3 复验（本回合实测）

- `py_compile` 4/4 → **PY_COMPILE_OK**。
- 行尾：4 源文件仍 **PURE_CRLF**（model_management CRLF=2318 / bareLF=0；其余三文件 CRLF=139/2190/1825，bareLF 均 0）。行数未变（docstring 行数不变），故 CRLF 计数与 X2 后一致。
- **仅 `model_management.py` 变化**；`pinned_memory.py` / `model_patcher.py` / `ops.py` md5 **未变**（`c629da9c…` / `d523b099…` / `96090a01…`），证明**没有改多**。
- 补丁已重出：**306 行**，仍 MIXED（CRLF=284 内容 + bareLF=22 元数据）；临时目录以改前备份还原后 `git apply -p0 --check` ✅、`patch -p0 --binary` ✅ 且 4/4 **字节级 IDENTICAL**。
- **最终 md5（冻结值，同 §6.2 / §6.4.3）**：

```
0f7c056c0d45645f81b4256f3d56703a  comfy/model_management.py   ← 本轮由 0a9040f2… 更新
c629da9c2089c7c75d3b53386f49fdc9  comfy/pinned_memory.py      ← 未变
d523b099933dad858e3a9e73a567c730  comfy/model_patcher.py      ← 未变
96090a01b13e1b81907152324b6430a0  comfy/ops.py                ← 未变
```

> 产物自本记录起**冻结**，不再改动。部署/回滚方式同 §9.4；唯一验收判据为上述 4 个 md5。
