# 交付说明：XPU 三层存储（VRAM → RAM → 磁盘）

> 2026-10-09 · 专家团「ComfyUI Intel 显卡适配团」交付
> 目标机：Intel Arc B580（驱动 32.0.101.9034）· torch 2.14.0+xpu · ComfyUI v0.39.0
> 基线：comfy_aimdo 0.5.6.dev28 (`gc2cf98fb9`)

---

## 0. 结论

用户指出的缺陷 —— **「只有显存不足直接取硬盘，没有内存这一步」** —— **在代码上成立，现已修复并在真机验收通过（含 1 项判据不适用，见 §6）**。

修复后 XPU 上的存储层级为真正的三级：

```
取回：RAM 命中 → RAM → VRAM ┊ RAM 未命中 → 磁盘 → VRAM（并按预算填充 RAM）
回收：VRAM 驱逐 ┊ RAM 预算耗尽 → 既有 steal/bucket 机制驱逐（数据可从磁盘重建）
```

---

## 1. 改动说明：改了什么、为什么、影响面

### 1.1 根因（不是"缺三层代码"，是 RAM 中间层被两处开关联手关掉）
| 环节 | 位置 | 事实 |
|---|---|---|
| ① 无预算 | `comfy/model_management.py:1636-1644` | `if is_nvidia() or is_amd():` 内才赋 `MAX_PINNED_MEMORY` → **XPU 恒为 `-1`** |
| ② 直接判否 | `model_management.py:757-759` | `free_registrations(): if MAX_PINNED_MEMORY <= 0: return False` |
| ③ RAM pin 建不起来 | `comfy/pinned_memory.py:94-96` | `ensure_pin_registerable()` 恒 False → `pin_memory()` 短路 |
| ④ 退化为直通 | `comfy/ops.py:228-237` | pin 为 None → 「文件 → 显存」跳过 RAM |
| ⑤ C 侧也刻意关 | fork `src-xpu/dispatch.cpp:606-610` | `xpu_host_register()` 为 no-op（注释 "XPU phase 1 keeps ComfyUI host pinning disabled"） |

另：真机实测 `torch.cuda.cudart()` 在 XPU 构建下抛 `AssertionError: 'Torch not compiled with CUDA enabled'`；④ 之所以没炸，只因 ②③ 短路得更早（**属"静默失效掩盖了真雷"**）。

### 1.2 核心设计原则
> **「注册不可用」≠「缓存不可用」。**
把两件事解耦：**缓存预算**（字节，参与驱逐决策）与 **注册状态**（能否被驱动固定，只影响拷贝性能，不影响正确性）。

### 1.3 改动清单（4 文件 11 处，全部 XPU 门控）
| 文件 | 改动 |
|---|---|
| `comfy/model_management.py` | ① 新增 `HOST_PIN_REGISTRATION_SUPPORTED = is_nvidia() or is_amd()`；② 新增独立计数器 `TOTAL_PIN_CACHE_MEMORY`（"已缓存未注册"字节）；③ `ensure_pin_registerable` 三分支（无注册能力时退化为**纯缓存预算**，经 `free_pins` 真正销毁 hostbuf）；④ 新增 XPU 预算分支（env `AIMDO_XPU_RAM_CACHE_GB`）；⑤ `host_register_pin`/`host_unregister_pin` 守卫 helper；⑥ `xpu_ram_cache_enabled()` 谓词；⑦⑧ `pin_memory`/`unpin_memory` 守卫化与记账分流 |
| `comfy/pinned_memory.py` | ⑨ `get_pin` 早退条件追加 `not HOST_PIN_REGISTRATION_SUPPORTED`；⑩ 注册段整体门控（XPU 跳过注册与两次重试、不走 `_steal_pin` 失败回退）；⑪ 记账分流（`registered` 忠实保持 `False`） |
| `comfy/model_patcher.py` | ⑫ `unregister_inactive_pins` 的 `cudaHostUnregister` 走 helper；⑬ `partially_unload_ram` 新增 `elif not HOST_..._SUPPORTED` 扣减 `TOTAL_PIN_CACHE_MEMORY` |
| `comfy/ops.py` | ⑭ `handle_pin:232` 追加 `or comfy.model_management.xpu_ram_cache_enabled()` |

> ⑬ 是**必须**的：铁律要求 `registered` 忠实表示"驱动已注册"，XPU 上恒 `False`，故既有"以 `registered` 为键"的扣账逻辑全部不执行 —— **漏掉 ⑬ 会让预算只增不减、RAM 层重新变死代码且不报错**。

### 1.4 影响面
- **XPU**：RAM 中间层启用（新行为）。
- **CUDA / ROCm / CPU / MPS**：**零变化**。新增判断在非 XPU 上恒 False，helper 在原路径逐字等价。
- **DirectML**：`xpu_ram_cache_enabled()` 以 `get_torch_device().type` 判定，DirectML 下正确返回 False（详见 §7 的 X2）。

---

## 2. 修改后的完整文件

可直接替换的副本在 **`aimdo-xpu/deliver/comfy/`**（4 个文件，已核 md5 与行尾）：

```
0f7c056c0d45645f81b4256f3d56703a  model_management.py
c629da9c2089c7c75d3b53386f49fdc9  pinned_memory.py
d523b099933dad858e3a9e73a567c730  model_patcher.py
96090a01b13e1b81907152324b6430a0  ops.py
```
全部 **PURE_CRLF**（bareLF=0）。

等价的**可重放补丁**：`aimdo-xpu/comfyui-3tier-ram-cache.patch`
（md5 `a517cfa1fc6f2be64fc0a47546494c54`，306 行；已在干净副本上验证重放后 **4/4 逐字节 IDENTICAL**）

---

## 3. 部署验证步骤

### 3.1 部署（二选一）
```bash
# 方式 A（推荐）：打补丁。必须在 <ComfyUI> 根目录下、-p0
cd E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable\ComfyUI
git apply -p0 C:\Users\HE\WorkBuddy\2026-10-08-23-50-54\aimdo-xpu\comfyui-3tier-ram-cache.patch
# 或：patch -p0 --binary -i <patch>

# 方式 B：直接覆盖
copy <本包>\deliver\comfy\*.py  <ComfyUI>\comfy\
```
> ⚠️ **行尾约束**：这 4 个文件是 **CRLF**，补丁内容行也是 CRLF（仅 diff 元数据行为 LF）。
> **禁止**不带 `--binary` 的 `patch`；**禁止**把文件转成 LF —— 转 LF 会破坏逐字节可复现性。

### 3.2 文件闸（唯一判据）
```bash
cd <ComfyUI>\comfy
md5sum model_management.py pinned_memory.py model_patcher.py ops.py
```
必须逐字等于 §2 的四个值。**任一不符即停手，不要启动。**

### 3.3 启动与期望输出
```bash
cd E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --disable-auto-launch --port 8196
```
> **不得**用 `run_intel_gpu.bat`；**不得**设 `AIMDO_XPU_ENABLED`（含 `=0`）；**不带** `--enable-dynamic-vram`。否则会掩盖被测路径。

期望在启动日志中看到（本轮实测行号取自 `logs/run_on_final.log`）：
```
[INFO] Enabled XPU RAM cache 8178 (default = min(ram*0.25, 24GiB))     ← 新增
[INFO] comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend
[INFO] comfy-aimdo XPU backend ready (mode=native_hook)
[INFO] DynamicVRAM support detected and enabled
```
且**不得**出现 `XPU backend not requested` / `No working comfy-aimdo install detected`。

### 3.4 RAM 缓存预算如何调
| 设置 | 效果 |
|---|---|
| 不设 `AIMDO_XPU_RAM_CACHE_GB` | 默认 `min(ram*0.25, 24GiB)`（本机 ~32 GiB RAM ⇒ **8178 MiB**，由日志值反推） |
| `AIMDO_XPU_RAM_CACHE_GB=16` | 显式设 16 GiB |
| `AIMDO_XPU_RAM_CACHE_GB=0` | **关闭**，退回"磁盘直通"（可回滚）；日志出现 `XPU RAM cache disabled ... disk passthrough mode` |
| 非法值 | 忽略并告警，回落默认（不让拼错的变量静默改变行为） |

---

## 4. 失败回退方案

| 层级 | 动作 | 说明 |
|---|---|---|
| **软回退（首选）** | 设 `AIMDO_XPU_RAM_CACHE_GB=0` 启动 | 行为退回改动前的"磁盘直通"，**无需改文件**。本轮已实测：disabled 行出现、enabled 行缺席、门禁三正两负仍全中（不退化） |
| **硬回退** | 用备份覆盖：`ComfyUI\comfy\*.py.bak.20261009-032052` | 4 个备份均就位 |
| **若门禁本身失败** | 先查是否误设了 `AIMDO_XPU_ENABLED` / `--enable-dynamic-vram`（会短路门禁、掩盖问题） | 再用 §3.2 文件闸确认版本 |

---

## 5. 验证方法（本轮实际执行）
`B580 独占`；文件闸 → 免 flag 门禁 → S3b 计数收敛 → SD1.5 出图 / MiniMax H3 出片 → H1/H2 逐字节 → 回滚 → 同 seed 对照。
执行者：`comfyui-workflow-validator`；原始日志 `aimdo-xpu/logs/`；报告 `VERIFY_3tier_ram_cache.md`。

---

## 6. 验证结果（逐项，含实测数据）

| 项 | 判定 | 实测 |
|---|---|---|
| 文件闸 | **PASS** | 5/5，验收前后各核一次未变 |
| 免 flag 门禁 | **PASS** | 三正齐（`run_on_final.log:13/15/47`）、两负缺；新行 `Enabled XPU RAM cache 8178`（`run_on_final.log:42`） |
| **S3b 计数收敛** | **PASS** | 缓存 `0 → 8177 MiB`（触顶，idx47）→ **触顶后回落**，峰后 drop **270 MiB**（门限 64 MiB）、`monotonic=False`；随新模型载入（n=2→3→4）触发驱逐；上升期最大单步 -1968 MiB。`TOTAL_PINNED_MEMORY` 全程 **恒 0**（XPU 无注册，符合设计） |
| H1 文件→hostbuf | **PASS** | 32 MiB `os.urandom` **逐字节一致**（`H1_OK`） |
| **H2 hostbuf→XPU 显存** | **PASS** | 32 MiB 写入真机显存后**逐字节一致**（`H2D_OK`, `device=xpu:0`）—— **直接证伪「返回 True 却没真写显存」的静默错误** |
| 同 seed 字节对照 | **INCONCLUSIVE** | 见下 |
| 回滚 | **PASS** | disabled 行出现、enabled 行缺席、门禁不退化（`ROLLBACK_OK`） |
| 出图 / 出片 | **PASS** | SD1.5 PNG **375,001 B**；MiniMax H3 MP4 **1,120,626 B**，PyAV 解码校验 h264 864×480 **124 帧 / 24fps / 5.1667 s**，非损坏 |

### 关于 INCONCLUSIVE（如实记录，不粉饰）
同 seed 的"开/关两次产物 sha256 相同"**判据在本机不成立**：同配置两次运行（off1 vs off2）的 sha256 本身就不同 ⇒ 基线不可复现。像素差显示 `on×off` 与 `off1×off2` **同一量级**（max 27/34 vs 26），**未发现补丁额外引入误差**；非确定性在 ON/OFF 两次运行**都出现**（均走 aimdo `native_hook` 后端）⇒ 属后端/采样管线性质。

**该条本想防的静默错误，已由 H1/H2 的更直接证据覆盖**，故接受为 INCONCLUSIVE 而非 PASS。

---

## 7. 已知边界与未决项

1. **XPU 采样非确定性**（**未决，超出本补丁范围**）：本机 XPU 采样存在与 aimdo 无关的非确定性，本轮未定位根因。它使"同 seed 字节对照"在此机器上不可判定。
2. **产物形态**：SD1.5/H3 出图出片正常；H3 的 MP4 仍只含视频轨（音频轨未入容器，属 muxer 层面，与本补丁无关）。
3. **C 侧 host 内存仍为 pageable**：`xpu_host_alloc` 用 `std::malloc`（`dispatch.cpp:593-597`），**非** pinned —— 因为改 pinned 曾导致 `DEVICE_LOST`。故本方案的 RAM 层是"pageable 宿主缓存"，**拷贝加速有限**；真正的 pinned 收益属 P1。
4. **H2D 是同步的**：`dispatch.cpp:814` 用 `queue->memcpy(...).wait_and_throw()`，磁盘读与 H2D 无重叠。异步化属 P1。
5. **`hostbuf_*` 的启动依赖**（本轮新发现）：依赖 `control.init_devices()`（`main.py:285`）；**只调 `control.init()` 就碰 hostbuf 会 access violation**。故任何脱离完整启动流程的独立 harness 必须走完整初始化。
6. **X2 的精确表述**：`xpu_ram_cache_enabled()` 原用 `is_intel_xpu()`（只反映"XPU 是否可用"）判据过松。**真实分歧不是"混合机跑 CUDA"**（`get_torch_device()` 在 GPU 路径先判 `is_intel_xpu()`，该场景不可达），而是 **`--directml`**（`model_management.py:198-200` 是最前置短路）。枚举 384 个设备状态：非 directml 差异 0，有差异的 32 个全是 `--directml`+xpu 可用。已修为 `get_torch_device().type == "xpu"`。

---

## 8. 交付物索引（`C:\Users\HE\WorkBuddy\2026-10-08-23-50-54\aimdo-xpu\`）

| 类别 | 文件 |
|---|---|
| **交付件** | `deliver/comfy/{model_management,pinned_memory,model_patcher,ops}.py`、`comfyui-3tier-ram-cache.patch`、本文件 |
| 设计 | `PHASE2_3tier_design.md` |
| 实现 | `IMPL_3tier_ram_cache.md`（含 §6.4 行尾与部署约束、逐处改动前后的 md5） |
| 审查 | `REVIEW_3tier_ram_cache.md`（R1–R6 + Round-2/Round-2b + 本文档 §7-6 的 X2 澄清） |
| 调研 | `RESEARCH_3tier_architecture_audit.md`、`RESEARCH_xpu_3tier_capability.md` |
| 验收 | `VERIFY_3tier_ram_cache.md`；日志 `logs/{run_on,run_on_final,run_off,run_off2}.log`、`logs/s3b_on*.csv`、`logs/h1_*.log`、`logs/h2_*.log`；产物 `artifacts/onfinal_p3_h3_00001_.mp4`、`artifacts/onfinal_p3_small_00001_.png` |
| 回退 | `ComfyUI/comfy/*.py.bak.20261009-032052` |
