# comfy-aimdo × Intel Arc（XPU）：三层存储卸载 + 免 flag 自动启用

本目录是本机在 **Intel Arc B580** 上适配 comfy-aimdo（ComfyUI 的显存动态卸载器）的完整交付存档，包含补丁、可替换源码、设计与验收文档、自证脚本、原始日志与真实产出。

- **目标机**：Intel Arc B580（驱动 `32.0.101.9034`）· torch `2.14.0+xpu` · ComfyUI `v0.39.0` · RAM ≈ 32 GiB · 模型位于 NVMe
- **aimdo 基线**：`comfy_aimdo 0.5.6.dev28`（`__commit_id__ = gc2cf98fb9`）
- **本目录对应分支**：`xpu-3tier-ram-cache`（基于 `ayi3030/comfy-aimdo` main `c2cf98f`）

> ⚠️ 本目录内的日志与文档包含本机路径（`E:\aiwork\...`、`C:\Users\HE\...`）。已扫描确认**不含任何凭据**。

---

## 一、目录里的两部分工作

| 目录 | 主题 | 性质 |
|---|---|---|
| `docs/3tier-ram-cache/` + `patch/comfyui-3tier-ram-cache.patch` + `comfy/` | **三层存储卸载**（VRAM → RAM → 磁盘） | **ComfyUI 侧补丁**，本目录的主交付 |
| `docs/optin-gate/` + `patch/{comfyui-intel-gate, PATCH_xpu_opt_in_alignment, aimdo-xpu}.patch` | **免 flag 自动启用 DynamicVRAM** | 上一阶段（含 comfy-aimdo 侧改动） |

⚠️ **两部分性质不同**：三层卸载改的是 **ComfyUI 源码**（不是 aimdo 包），所以以**补丁**形式交付；免 flag 那部分同时含 aimdo 侧改动（已进入 fork 的 `main`）。

---

## 二、主交付：三层存储卸载

### 2.1 问题
Intel XPU 上 aimdo 的 **RAM 中间层被两处开关联手关掉**，权重只能「磁盘 → 显存」直通 —— 即用户观察到的 *"显存不足直接取硬盘，没有内存这一步"*。

| 环节 | 位置 | 事实 |
|---|---|---|
| ① 无预算 | `comfy/model_management.py:1636-1644` | `if is_nvidia() or is_amd():` 内才赋 `MAX_PINNED_MEMORY` → **XPU 恒为 `-1`** |
| ② 直接判否 | `model_management.py:757-759` | `free_registrations(): if MAX_PINNED_MEMORY <= 0: return False` |
| ③ RAM pin 建不起来 | `comfy/pinned_memory.py:94-96` | `pin_memory()` 在此短路 |
| ④ 退化为直通 | `comfy/ops.py:228-237` | pin 为 None → 「文件 → 显存」，跳过 RAM |
| ⑤ C 侧也刻意关 | fork `src-xpu/dispatch.cpp:606-610` | `xpu_host_register()` 是 no-op（"XPU phase 1" 简化） |

另有一个被掩盖的雷：`torch.cuda.cudart()` 在 XPU 构建下抛 `AssertionError`，只因 ②③ 短路得更早才没炸。

### 2.2 设计原则
> **「注册不可用」≠「缓存不可用」。**
把**缓存预算**（字节，参与驱逐决策）与**注册状态**（能否被驱动固定，只影响拷贝性能）解耦。

改 **4 文件 11 处**，全部 XPU 门控，**CUDA / ROCm / CPU / MPS 行为零变化**：

- `model_management.py`：新增 `HOST_PIN_REGISTRATION_SUPPORTED`、独立计数器 `TOTAL_PIN_CACHE_MEMORY`、守卫 helper `host_register_pin`/`host_unregister_pin`、谓词 `xpu_ram_cache_enabled()`；`ensure_pin_registerable` 三分支；新增 XPU 预算分支
- `pinned_memory.py`、`model_patcher.py`、`ops.py`：见 `docs/3tier-ram-cache/IMPL_3tier_ram_cache.md`

### 2.3 真机验收结论（**通过，含 1 项判据不适用**）

| 项 | 判定 | 实测 |
|---|---|---|
| 文件闸 | PASS | 4 个源码 md5 前后一致 |
| 免 flag 门禁 | PASS | 三正齐 / 两负缺；新增 `Enabled XPU RAM cache 8178` |
| **计数收敛** | PASS | RAM 缓存 `0 → 8177 MiB`（触顶）→ **触顶后回落**，峰后 drop **270 MiB**（门限 64）、`monotonic=False`；`TOTAL_PINNED_MEMORY` 恒 0（符合设计） |
| **H1 文件→hostbuf** | PASS | 32 MiB **逐字节一致** |
| **H2 hostbuf→XPU 显存** | PASS | 32 MiB 写入真机显存后**逐字节一致** → 直接证伪"返回 True 却没真写显存"的静默错误 |
| 回滚 | PASS | `AIMDO_XPU_RAM_CACHE_GB=0` → `disk passthrough mode`，门禁不退化 |
| 出图 / 出片 | PASS | PNG **375,001 B**；MP4 **1,120,626 B**（124 帧 / 24fps / 5.1667s，PyAV 校验） |
| 同 seed 字节对照 | **INCONCLUSIVE** | 同配置两次运行的 sha256 本身就不同 ⇒ 基线不可复现、判据本机不成立；`on×off` 与基线同量级，未发现补丁归因差异。**该条想防的静默错误已由 H1/H2 直接覆盖** |

完整数据见 `docs/3tier-ram-cache/VERIFY_3tier_ram_cache.md` 与 `logs/`。

---

## 三、怎么用

### 3.1 部署（二选一）
```bash
# 方式 A（推荐）：打补丁。在 ComfyUI 根目录执行，-p0
cd <ComfyUI>
git apply -p0 <此目录>/patch/comfyui-3tier-ram-cache.patch
# 或： patch -p0 --binary -i <此目录>/patch/comfyui-3tier-ram-cache.patch

# 方式 B：直接覆盖
copy <此目录>\comfy\*.py  <ComfyUI>\comfy\
```
> ⚠️ 这 4 个文件是 **CRLF** 行尾，补丁内容行也是 CRLF（仅 diff 元数据行为 LF）。
> **禁止**不带 `--binary` 的 `patch`；**禁止**把文件转成 LF。

### 3.2 文件闸（唯一判据）
```bash
cd <ComfyUI>/comfy
md5sum model_management.py pinned_memory.py model_patcher.py ops.py
```
必须逐字等于：
```
0f7c056c0d45645f81b4256f3d56703a  model_management.py
c629da9c2089c7c75d3b53386f49fdc9  pinned_memory.py
d523b099933dad858e3a9e73a567c730  model_patcher.py
96090a01b13e1b81907152324b6430a0  ops.py
```
补丁自身：`a517cfa1fc6f2be64fc0a47546494c54`（306 行）

### 3.3 启动与期望日志
```bash
cd <portable 根>
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --disable-auto-launch --port 8196
```
> **不得**用 `run_intel_gpu.bat`；**不得**设 `AIMDO_XPU_ENABLED`（含 `=0`）；**不带** `--enable-dynamic-vram` —— 否则会短路门禁、掩盖真实状态。

应看到：
```
[INFO] Enabled XPU RAM cache 8178 (default = min(ram*0.25, 24GiB))
[INFO] comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend
[INFO] comfy-aimdo XPU backend ready (mode=native_hook)
[INFO] DynamicVRAM support detected and enabled
```

### 3.4 预算开关
| 设置 | 效果 |
|---|---|
| 不设 `AIMDO_XPU_RAM_CACHE_GB` | 默认 `min(ram*0.25, 24GiB)`（本机 ≈ 8178 MiB） |
| `AIMDO_XPU_RAM_CACHE_GB=<N>` | 显式指定 |
| `AIMDO_XPU_RAM_CACHE_GB=0` | **关闭**，退回"磁盘直通"（可回滚） |

### 3.5 回退
- **软回退**：`AIMDO_XPU_RAM_CACHE_GB=0`（无需改文件）
- **硬回退**：用 `patch/comfyui-3tier-ram-cache.patch` 反向应用，或还原你自己的原始 `comfy/*.py`

---

## 四、自证脚本（`probes/`）

| 脚本 | 用途 |
|---|---|
| `yan_h1_ram_fill.py` / `yan_h2_ram_h2d.py` | H1 文件→hostbuf、H2 hostbuf→XPU 显存，**逐字节比对** |
| `yan_s3b_run.py` / `s3b_*.csv` | 采样 RAM 缓存预算曲线（判"升顶后是否回落"） |
| `yan_logcheck.py` | 门禁日志断言 |
| `run_acceptance.py` / `selfcheck_prompts.py` / `PROMPT_*.json` | 端到端验收主门禁（SD1.5 出图 + MiniMax H3 出片） |

> ⚠️ **`probe_h1_hostbuf_fill.py` / `probe_h2_hostbuf_h2d.py` / `probe_s3b_launch.py` 已被真机实测证伪，请勿直接复用**（三个都是探针自身缺陷，非被测功能问题）。详见 `docs/3tier-ram-cache/PHASE5_PROBE_KIT.md` 顶部注记。
>
> 实测经验：`comfy_aimdo` 的 `hostbuf_*` 依赖 `control.init_devices()`（`main.py:285`）；只调 `control.init()` 就碰 hostbuf 会 access violation。独立 harness 必须走完整初始化路径。

---

## 五、已知边界（避免误读）

- **XPU 采样非确定性**：本机 XPU 采样存在与 aimdo 无关的非确定性，未定位根因，**超出本补丁范围**。
- **H2D 是同步的**：`dispatch.cpp:814` 用 `queue->memcpy(...).wait_and_throw()`，磁盘读与 H2D 无重叠。
- **宿主内存非 pinned**：`xpu_host_alloc` 用 pageable `std::malloc`（`dispatch.cpp:593-597`），因改 pinned 曾致 `DEVICE_LOST`。故 RAM 层是"pageable 宿主缓存"，拷贝加速有限。
- **H3 的 MP4 只含视频轨**，音频轨未入容器（muxer 层面，与本补丁无关）。

---

## 六、许可证与归属

`comfy/` 下的 4 个文件是 **ComfyUI**（`comfyanonymous/ComfyUI`，GPL-3.0）的**修改副本**，此处仅为便于取用而附带；权威来源以上游仓库为准，**规范交付物是 `patch/comfyui-3tier-ram-cache.patch`**。

XPU 原生后端实现位于 `ayi3030/comfy-aimdo` 的 `src-xpu/`（上游 comfy-aimdo 无此目录）。各文件沿用其原许可证。
