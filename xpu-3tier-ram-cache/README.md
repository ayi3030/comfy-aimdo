# comfy-aimdo × Intel Arc（XPU）：三层存储卸载 + 免 flag 自动启用

> **这是一份可被自动化执行（含 AI 智能体）的安装说明。** 每一步都有明确命令与**判据**；遇到不符合判据的情况，按「排错」一节分支处理，不要跳过。
>
> 目标机：**Intel Arc 独显（已在 B580 上验证）** · Windows x64 · ComfyUI + `torch+xpu`

> ## ⚠️ v2 变更（请先读这三条，再决定怎么装）
>
> 1. **策略层已对齐上游（NVIDIA）**。v1 会在「磁盘判定为快盘」时也强制建 RAM pin；v2 **移除了这处覆盖**，`comfy/ops.py` 现已与 ComfyUI 原厂**逐字节相同**。
> 2. **因此默认配置下 RAM 中间层会休眠**（实测峰值 0 MiB），这与 NVIDIA 用户在快盘上的行为一致 —— **这是预期结果，不是没生效**。想启用 RAM 层见 §「启用 RAM 中间层（可选，有风险）」。
> 3. **启用 RAM 层存在已观测的设备级风险**：MiniMax H3 在 RAM 缓存启用时，8 次运行中出现 2 次 `UR_RESULT_ERROR_OUT_OF_RESOURCES` → `DEVICE_LOST`。根因未定位，失败率无法估计（样本太少）。**默认配置与 `AIMDO_XPU_RAM_CACHE_GB=0` 档位零失败记录。**
>
> 完整结论见 `docs/3tier-ram-cache/DELIVERY_v2_policy_align.md`，原始证据见 `docs/3tier-ram-cache/VERIFY_policy_align.md`。

---

## 0. 最短路径（TL;DR）

在 **ComfyUI 根目录**（即含 `main.py` 与 `python_embeded\` 的那一层）依次执行：

```bat
:: ① 安装 XPU 版 aimdo 后端
.\python_embeded\python.exe -m pip install --no-deps --force-reinstall "<本包>\wheel\comfy_aimdo-0.5.6.dev28-cp39-abi3-win_amd64.whl"

:: ② 打 ComfyUI 三层卸载补丁
.\python_embeded\python.exe -c "import comfy_aimdo,sys;print(comfy_aimdo.__version__, comfy_aimdo.__commit_id__)"
git apply -p0 --directory=. "<本包>\patch\comfyui-3tier-ram-cache-v2.patch"

:: ③ 校验（必须逐字通过）
cd comfy && ..\python_embeded\python.exe -c "import hashlib;[print(hashlib.md5(open(f,'rb').read()).hexdigest(),f) for f in ['model_management.py','pinned_memory.py','model_patcher.py','ops.py']]"
```

期望输出（**v2 值**，必须逐字一致，否则**停下**，见「排错」）：
```
53c3fac684a22b8995ee0d5ba7b0becd model_management.py
c629da9c2089c7c75d3b53386f49fdc9 pinned_memory.py
d523b099933dad858e3a9e73a567c730 model_patcher.py
9e3f9620541118ee30479660a9191557 ops.py
```

> 说明：`ops.py` 的期望值 `9e3f9620…` **就是 ComfyUI v0.39.0 原厂文件的 md5** —— v2 已把该文件恢复原样。若你看到的是 v1 的 `96090a01…`，说明打的还是旧补丁。
> v1 补丁（`comfyui-3tier-ram-cache.patch`）仍在 `patch/` 下保留，仅供对照；**新装请用 v2**。

然后直启（**不要**加任何 dynamic-vram 相关开关）：
```bat
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build
```

启动日志里必须出现这 4 行：
```
[INFO] Enabled XPU RAM cache 8178 (default = min(ram*0.25, 24GiB))
[INFO] comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend
[INFO] comfy-aimdo XPU backend ready (mode=native_hook)
[INFO] DynamicVRAM support detected and enabled
```
且**不得**出现 `XPU backend not requested` 或 `No working comfy-aimdo install detected`。

---

## 1. 这个包解决什么问题

aimdo 是 ComfyUI 的显存动态卸载器，设计为三级存储：`显存(VRAM) ↔ 内存(RAM) ↔ 磁盘`。

在 Intel XPU 上，**RAM 中间层被两处开关联手关掉了**，权重只能「磁盘 → 显存」直通 —— 即"显存不足直接取硬盘、没有内存这一步"。本包把 RAM 中间层打开：

| 方向 | 修复后的行为 |
|---|---|
| 取回（fault） | RAM 命中 → `RAM→VRAM`；未命中 → `磁盘→VRAM` 并按预算填充 RAM |
| 回收（压力） | VRAM 驱逐后仍可从 RAM 恢复；RAM 预算耗尽才驱逐到磁盘 |

**核心设计原则**：*「注册不可用」≠「缓存不可用」* —— 把**缓存预算**（参与驱逐）与**驱动注册状态**（只影响拷贝性能）解耦。

### 包的两部分（**依赖关系不同，别混**）

| 部分 | 内容 | 性质 |
|---|---|---|
| **A. aimdo XPU 后端** | `wheel/comfy_aimdo-0.5.6.dev28-*.whl`（内含 `aimdo_xpu.dll`） | **pip 包**。上游 PyPI 的 comfy-aimdo **不含 XPU 支持**，必须用本 wheel |
| **B. ComfyUI 三层卸载补丁** | `patch/comfyui-3tier-ram-cache.patch` + `comfy/` | **ComfyUI 源码补丁**（4 文件 / 11 处），不是 pip 包 |

> **只装 A 不装 B**：XPU 后端可用，但仍是"磁盘直通"（没有 RAM 中间层）。
> **只装 B 不装 A**：补丁是空操作（XPU 后端没装，`HOST_PIN_REGISTRATION_SUPPORTED` 分支不参与）。

---

## 2. 兼容性检查（装之前先跑这个）

```bat
.\python_embeded\python.exe -c "import torch,sys;print('py',sys.version.split()[0]);print('torch',torch.__version__);print('xpu_available',hasattr(torch,'xpu') and torch.xpu.is_available())"
```

| 判据 | 期望 | 不符合时怎么办 |
|---|---|---|
| Python | **≥ 3.9**（wheel 是 `cp39-abi3`） | 低于 3.9 无法使用本 wheel |
| 平台 | **Windows x64** | 本 wheel **仅 Windows**；Linux 需用 fork 里的构建脚本自行编译 |
| torch | 含 **XPU 支持**（如 `2.14.0+xpu`；`torch.xpu` 存在） | 若 `torch` 是 CUDA 构建，需先换装 Intel 的 `torch+xpu` |
| `xpu.is_available()` | `True` | 若 `False`：检查是否为 Intel Arc 独显、驱动是否装了 Level Zero 运行时 |

> **不是 Intel GPU 的机器**：可以装 B（补丁在 CUDA/ROCm/CPU 上**行为零变化**，已验证），但**不要**装 A（无意义）。此时启动日志里不会有 `Enabled XPU RAM cache`，属正常。

---

## 3. 安装

### 3.1 定位 ComfyUI 根目录
根目录 = 同时包含 `main.py`（或 `ComfyUI\main.py`）与 `python_embeded\` 的那一层。
- Windows Portable：`<任意路径>\ComfyUI_windows_portable\`
- 其他安装方式：用你的 ComfyUI 所在 Python 环境的解释器替换下文的 `.\python_embeded\python.exe`

### 3.2 装 A —— aimdo XPU 后端
```bat
.\python_embeded\python.exe -m pip install --no-deps --force-reinstall "<本包>\wheel\comfy_aimdo-0.5.6.dev28-cp39-abi3-win_amd64.whl"
.\python_embeded\python.exe -c "import comfy_aimdo;print(comfy_aimdo.__version__, comfy_aimdo.__commit_id__)"
```
**判据**：输出 `0.5.6.dev28 gc2cf98fb9`。
若输出 `ModuleNotFoundError` 或版本号不是 dev28 → 见「排错」。

> 装前请**关掉正在运行的 ComfyUI**（Windows 上 `.dll` 被占用会导致替换失败）。
> 建议先备份：把 `python_embeded\Lib\site-packages\comfy_aimdo\` 整个目录拷到别处。

### 3.3 装 B —— ComfyUI 三层卸载补丁
```bat
cd <ComfyUI 根>
git apply -p0 "<本包>\patch\comfyui-3tier-ram-cache.patch"
```
若上一步报错（多半是 ComfyUI 版本不同），按顺序试：
```bat
git apply -p0 --3way "<本包>\patch\comfyui-3tier-ram-cache.patch"
:: 或
patch -p0 --binary -i "<本包>\patch\comfyui-3tier-ram-cache.patch"
```
**仍失败** → 说明你的 ComfyUI 与该补丁的基线版本不同，改为**手动落 11 处改动**，逐处清单见 `docs/3tier-ram-cache/IMPL_3tier_ram_cache.md`。宁可手动改，也不要硬套。

> ⚠️ **行尾约束**：这 4 个文件是 **CRLF**，补丁内容行也是 CRLF（仅 diff 元数据行为 LF）。
> **禁止**不带 `--binary` 的 `patch`；**禁止**把文件转成 LF。若不方便打补丁，可直接用 `comfy\` 里的 4 个文件覆盖（它们是同一份字节）。

**判据**：见 §0 第 ③ 步的 md5 输出。**必须逐字一致**，否则不要启动。

---

## 4. 启动与验收

```bat
cd <ComfyUI 根>
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build
```

> ⚠️ **不要**加 `--enable-dynamic-vram`；**不要**设环境变量 `AIMDO_XPU_ENABLED`（含 `=0`）；**不要**用任何"一键启动脚本"。
> 这三个都会**短路门禁、掩盖真实状态**（其中 `--enable-dynamic-vram` 是修复**之前**的旧用法）。

逐项验收：

| # | 判据 | 不符时 |
|---|---|---|
| 1 | 日志出现 `Enabled XPU RAM cache <N>` | 说明 B 未生效 → 回到 §3.3 校验 md5 |
| 2 | 出现 `published N SYCL queue(s)` / `backend ready (mode=native_hook)` / `DynamicVRAM support detected and enabled` | 出现 `XPU backend not requested` → A 未生效；出现 `No working comfy-aimdo install detected` → A 装错/装到了别的 Python |
| 3 | 跑一个模型工作流，产物落盘且体积合理（图 >100 KB） | 见「排错」 |
| 4 | v2 默认下 RAM 缓存**应保持在接近 0** —— 这不是没生效，是与 NVIDIA 快盘行为一致 | 若必须看到缓存被填满，先读「启用 RAM 中间层（可选，有风险）」 |

### RAM 缓存预算怎么调
| 环境变量 | 效果 |
|---|---|
| 不设 | 默认 `min(物理内存 × 0.25, 24GiB)` |
| `AIMDO_XPU_RAM_CACHE_GB=<N>` | 显式指定 GiB |
| `AIMDO_XPU_RAM_CACHE_GB=0` | **关闭**，退回"磁盘直通"（即改动前行为） |

> 预算吃的是**系统内存**。若机器内存紧张（例如同时跑别的任务），把它调小。

### 启用 RAM 中间层（可选，有风险）

v2 默认让 RAM 层按上游策略休眠。若你要显式启用它：

```bat
set AIMDO_XPU_RAM_CACHE_GB=4
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --high-ram
```

**在这么做之前，请接受以下已观测事实**（B580 真机）：

| 现象 | 数据 |
|---|---|
| RAM 缓存启用时 H3 崩溃 | 8 次运行中 **2 次** 失败：`UR_RESULT_ERROR_OUT_OF_RESOURCES` → `UR_RESULT_ERROR_DEVICE_LOST`，死在 `comfy/ldm/minimax/model.py:777 torch.lerp(...)` |
| RAM 缓存未启用时 | 2 次运行 0 失败 |
| 与宿主内存的关系 | **无关**：崩溃那次提交内存 77.65 %、可用剩 10,987 MB，反而**优于**通过的某次（81.69 % / 8,549 MB）。失败是**设备侧** OOR |
| 与预算大小的关系 | **不单调**：4 GiB 的样本崩了，8 GiB 的两次反而过了 |
| 失败率 | **无法估计**。样本太少（3 中 1 的 Wilson 95 % CI 约 [6 %, 66 %]），既不能说「约四分之一」也不能排除更高 |
| 根因 | **未定位** |

**不要用 `--disable-fast-disk`**：它会落 `weights-loaded` 子集并触发 `model_prefetch.py:116` 的 `ensure_pin_registerable()`，在 XPU 分支会**真销毁** hostbuf（NVIDIA 上只是解注册、数据保留），缓存抖动更明显，且同样有上述崩溃风险。

想零风险，就用默认配置，或 `AIMDO_XPU_RAM_CACHE_GB=0`。

---

## 5. 回滚

| 层级 | 动作 |
|---|---|
| **软回滚（首选）** | 启动前设 `AIMDO_XPU_RAM_CACHE_GB=0` → 退回"磁盘直通"，**无需改文件** |
| **B 回滚** | `git apply -R -p0 "<本包>\patch\comfyui-3tier-ram-cache-v2.patch"`，或还原你自己的原始 `comfy/*.py`（注意 v2 下 `ops.py` 已是原厂文件，无需还原） |
| **A 回滚** | `.\python_embeded\python.exe -m pip uninstall comfy-aimdo`，或还原 §3.2 备份的 `site-packages\comfy_aimdo\` |

---

## 6. 排错

| 症状 | 原因 | 处置 |
|---|---|---|
| `ModuleNotFoundError: comfy_aimdo` | 装到了别的 Python | 用 **ComfyUI 用的那个**解释器重装（portable 下是 `python_embeded\python.exe`） |
| 版本号不是 `0.5.6.dev28` | 旧版仍在 | 加 `--force-reinstall`，并确认无并存 dist-info |
| `pip` 报 dll 被占用 | ComfyUI 还在运行 | 关掉进程（`taskkill /F /IM python.exe`）后重试 |
| 日志出现 `XPU backend not requested` | A 未生效，或设了 `AIMDO_XPU_ENABLED=0` | 检查环境变量；重装 A |
| 日志出现 `No working comfy-aimdo install detected` | A 装错位置 / dll 缺失 | 确认 `site-packages\comfy_aimdo\aimdo_xpu.dll` 存在 |
| 没有 `Enabled XPU RAM cache` 这一行 | B 未生效 | 校验 `model_management.py` 的 md5 |
| `git apply` 失败 | ComfyUI 版本与补丁基线不同 | 见 §3.3 的 `--3way` / 手动落 11 处 |
| 大量权重反复读盘、速度慢 | 属正常（磁盘是权威 backing store） | 调大 `AIMDO_XPU_RAM_CACHE_GB` 可减少回读 |
| 报 `AssertionError: Torch not compiled with CUDA enabled` | 有代码直接调了 `torch.cuda.cudart()` | 说明 B 未完整应用（4 文件必须**全部**替换） |

---

## 7. 已知边界（请与结论同读）

- **同 seed 结果并非逐字节可复现**：本机 XPU 采样存在与 aimdo 无关的非确定性（已记录、未定位根因，超出本补丁范围）。因此不能用"两次出图哈希相同"作为验收判据。
- **H2D 是同步拷贝**：`queue->memcpy(...).wait_and_throw()`，磁盘读与显存拷贝无重叠（性能项，未做）。
- **宿主内存非 pinned**：aimdo 的 XPU 宿主分配用 pageable `malloc`（改 pinned 曾致 `DEVICE_LOST`）。因此 RAM 层的拷贝加速有限。
- **wheel 仅 Windows x64**（`cp39-abi3-win_amd64`）。Linux 需自行编译。
- **补丁基线为 ComfyUI v0.39.0**；其他版本可能需要手动适配。

---

## 8. 目录结构

```
xpu-3tier-ram-cache/
├── README.md              ← 本文件（安装说明）
├── SHA256SUMS.txt         ← 全包校验和（sha256sum -c 可验）
├── wheel/                 ← A：aimdo XPU 后端（pip 安装）
├── patch/                 ← B：补丁（4 个）
├── comfy/                 ← B：4 个可直接覆盖的 ComfyUI 文件
├── docs/3tier-ram-cache/  ← 本主题：设计 / 实现 / 独立审查 / 真机验收 / 调研 / 探针说明
├── docs/optin-gate/       ← 另一主题存档：免 flag 自动启用 DynamicVRAM
├── probes/                ← 自证脚本与验收工装
├── logs/                  ← 原始日志与采样 CSV（真实证据）
└── artifacts/             ← 真实产出（MiniMax H3 出片 + SD1.5 出图）
```

**校验全包**：
```bat
cd xpu-3tier-ram-cache
sha256sum -c SHA256SUMS.txt
```

关键件校验和：
```
8f2aba921cc24a420bf1225dde6594b6a4cd20fc71848dd77eb20717982ee3f7  wheel/comfy_aimdo-0.5.6.dev28-cp39-abi3-win_amd64.whl
21daad6f0b74fd73e5372009a712a754  patch/comfyui-3tier-ram-cache-v2.patch   (223 行)  ← 本次用这个
a517cfa1fc6f2be64fc0a47546494c54  patch/comfyui-3tier-ram-cache.patch      (306 行)  ← v1，仅留档
53c3fac684a22b8995ee0d5ba7b0becd  comfy/model_management.py
c629da9c2089c7c75d3b53386f49fdc9  comfy/pinned_memory.py
d523b099933dad858e3a9e73a567c730  comfy/model_patcher.py
9e3f9620541118ee30479660a9191557  comfy/ops.py   （= ComfyUI v0.39.0 原厂）
```

---

## 9. 真机验收结论（Intel Arc B580 / torch 2.14.0+xpu / ComfyUI v0.39.0）

### 9.1 v1（带策略覆盖）—— 留档，已被 v2 取代

| 项 | 判定 | 实测 |
|---|---|---|
| 免 flag 门禁 | PASS | 三正齐 / 两负缺；`Enabled XPU RAM cache 8178` |
| RAM 中间层启用 | PASS | 缓存 `0 → 8177 MiB` 触顶，**触顶后回落**（峰后 drop 270 MiB，`monotonic=False`）→ 证明**可回收** |
| 文件→hostbuf | PASS | 32 MiB **逐字节一致** |
| hostbuf→显存 | PASS | 32 MiB 写入真机显存后**逐字节一致**（排除"拷了却没真写"的静默错误） |
| 出图 / 出片 | PASS | PNG 375,001 B；MP4 1,120,626 B（h264 864×480，124 帧 @24fps） |
| 回滚 | PASS | `AIMDO_XPU_RAM_CACHE_GB=0` → 退回直通且门禁不退化 |

⚠️ v1 的「RAM 中间层启用 PASS」只有 **n=1** 样本，按 v2 的样本量看**不足以证明稳定**（见 9.2）。

### 9.2 v2（策略对齐上游）—— 当前版本

| 档 | 配置 | RAM 缓存峰值 | SD1.5 | MiniMax H3 | 判定 |
|---|---|---|---|---|---|
| A | **默认（无 flag）** | **0.0 MiB**（203 采样恒 0） | ✓ 375,208 B | ✓ 1,085,024 B | **PASS** |
| C | `AIMDO_XPU_RAM_CACHE_GB=0` + `--disable-fast-disk` | 恒 0 | ✓ 375,352 B | ✓ 1,126,374 B | **PASS** |
| E | `--high-ram`（8 GiB） | 8177.8 / 8177.8，回落 380.6 / 357.4 | ✓ | ✓（n=2 全过） | 见 §风险 |
| F | `--high-ram` + `GB=4` | 4095.98 ×2，回落 253.5 / 352.5 | ✓ | **2 过 1 挂**（n=3） | **FAIL** |
| B | `--disable-fast-disk` | 8109–8176，回落 379.8 / 444.9 | ✓ | **2 过 1 挂**（n=3） | **FAIL** |
| H1/H2 | 拷贝逐字节 | — | — | — | **PASS**（无回归） |

**核心对照**：默认档 RAM 缓存峰值 **8177.8 MiB → 0.0 MiB** —— 策略覆盖确已移除，与 NVIDIA 快盘行为一致。
**汇总**：RAM 缓存**启用**的 8 次 H3 运行中 **2 次**设备级失败；**未启用**的 2 次 0 失败（样本量小，两栏都不构成统计结论）。

原始日志与 CSV 在 `logs/`（文件名带 `A-default` / `B-nofastdisk` / `C-rollback` / `E-highram` / `F-gb4`），完整报告见 `docs/3tier-ram-cache/VERIFY_policy_align.md`。

细节见 `docs/3tier-ram-cache/VERIFY_3tier_ram_cache.md`、`docs/3tier-ram-cache/DELIVERY_3tier_ram_cache.md`。

---

## 10. 许可证与来源

- **ComfyUI**（`comfyanonymous/ComfyUI`）为 **GPL-3.0**。`comfy/` 下 4 个文件是其**修改副本**，此处仅为便于取用而附带；**规范交付物是 `patch/comfyui-3tier-ram-cache.patch`**。
- **comfy-aimdo** 为 **GPL-3.0**（作者 rattus）。`wheel/` 内的二进制 wheel 由本仓库（`src/` + `src-xpu/`）构建，**对应源码即本仓库**，满足 GPL-3.0 的分发要求。XPU 原生后端位于 `src-xpu/`（上游 comfy-aimdo 无此目录）。

---

## 附：两部分工作的详细文档索引

**A/B 三层存储卸载（本包主题）**
- `docs/3tier-ram-cache/PHASE2_3tier_design.md` —— 设计方案（改动清单 `文件→位置→现状→改动→风险`）
- `docs/3tier-ram-cache/IMPL_3tier_ram_cache.md` —— 逐处实现（含 11 处改动的前后原文、md5、行尾与部署约束）
- `docs/3tier-ram-cache/REVIEW_3tier_ram_cache.md` —— 独立审查（R1–R6 + 两轮复核）
- `docs/3tier-ram-cache/VERIFY_3tier_ram_cache.md` —— 真机验收（含各项证据与 1 项判据不适用的说明）
- `docs/3tier-ram-cache/RESEARCH_3tier_architecture_audit.md`、`RESEARCH_xpu_3tier_capability.md` —— 底层调研
- `docs/3tier-ram-cache/PHASE5_PROBE_KIT.md` —— 探针说明（**含已证伪的 3 个脚本，勿直接复用**）

**免 flag 自动启用 DynamicVRAM（另一主题存档）**
见 `docs/optin-gate/`，入口 `docs/optin-gate/交付说明.md`。
