# Phase 5 真机验收探针工装（三层卸载 / RAM 中间缓存）

> 产出：xpu-compat-reviewer（兼容性审查官）· 2026-10-09 · 对应任务 #31「Phase5-prep」
> 依据：`REVIEW_3tier_ram_cache.md`（R1/R6/回滚判据与 H1/H2 harness 规格）、`PHASE2_3tier_design.md §5`
> **状态：已预备、未执行。** 本工装仅被 author/落盘；真机执行由 `runtime-verifier` 负责（遵循“不运行 ComfyUI”的审查约束）。
> 目标机：Intel Arc B580 / torch 2.14.0+xpu / ComfyUI v0.39.0；期望 run 版本 dev28（`gc2cf98fb9`）。

---

## ⚠️ 后续事实：本工装中三个探针已被真机实测证伪（请勿直接复用）

> 追加于 2026-10-09（Round-2 之后）。**仅追加，不改动下文既有观测/数值/行号。**
> 本节的三个缺陷均由 Phase 5 真机验收**实测证伪**（根因非推断）。**工具本身也要被审查**——此处如实登记，避免误导后来者。

| 探针 | 根因 | 后果 |
|---|---|---|
| `probe_h1_hostbuf_fill.py` | `comfy_aimdo/host_buffer.py:6` 在 **import 期**执行 `lib = control.lib`；而独立脚本/验收 harness 里 `control.init()`（及随后的 `control.init_devices()`，见 `main.py:285`）**从未被调用** → `control.lib` 恒为 `None`（初值见 `control.py:12`，直到 `control.py:251` 的 `ctypes.CDLL` 才被赋值）→ `host_buffer.py` 里 `if lib is not None:` 包住的 argtypes 绑定**从不执行** → `HostBuffer()` 访问 `None.hostbuf_allocate` 抛 `AttributeError`。**真机补充实测：仅 `control.init()` 仍不够，还需要 `control.init_devices()`，否则 `hostbuf_*` 原生调用会 access violation。** | 探针**恒打印 `HOSTBUF_API_FAIL`**，**永远拿不到真值**——把“探针自身缺陷”伪装成“被测功能失败” |
| `probe_h2_hostbuf_h2d.py` | 同上根因（import 期 `lib=None`）；且其“非 XPU 才 skip”的分支会被该崩溃**误判成 `H2D_API_FAIL`** | 同上：假失败 |
| `probe_s3b_launch.py` | ① 只采样、**不提交 prompt**（仓库内没有“只提交”的脚本；`run_acceptance.py` 会另起实例，喂不进本探针实例）；② **对 `LoadedModel` 调 `loaded_ram_size()`** —— 该方法实际在 `ModelPatcher` 上（`ModelPatcherDynamic.loaded_ram_size`，`model_patcher.py:2072`；调用范例见 `model_management.py:1057 model.loaded_ram_size()`），`LoadedModel` 并无此方法 → `AttributeError` 被 `except` 吞掉 | **`loaded_ram_mb` 列恒为 0 的伪证据**（恒定假数据比缺数据更危险） |

**结论与用法变更：**
- **请勿直接复用上述三个脚本**（`probe_h1_hostbuf_fill.py` / `probe_h2_hostbuf_h2d.py` / `probe_s3b_launch.py`）。
- 替换件（验收方自写，**已跑出可信结果**）：`yan_h1_ram_fill.py` / `yan_h2_ram_h2d.py` / `yan_s3b_run.py`。
  真机结果：**H1/H2 各 32 MiB 逐字节一致**；**S3b 缓存 0→8178 MiB 触顶后回落（峰后 drop 270 MiB）**——即 R1/R6 判据在真机**成立**，失败的是本文件的三个脚本，而非被测功能。
- 本文档 §3/§4 中对应的“用法”段仍保留作历史记录，但**执行时以 `yan_*.py` 为准**。
- 其余脚本（`probe_s3b_analyze.py` 的分析逻辑、`probe_s4_hash.py`、`probe_rollback_check.py`）未被证伪；其中 `probe_s3b_analyze.py` 依赖 CSV 输入，请确认 CSV 由可信的 `yan_s3b_run.py` 产出后再用。

---

## 0. 工装清单

| 文件 | 作用 | 退出码 |
|---|---|---|
| `PHASE5_EXPECTED_MD5.txt` | 文件闸：改后 4 文件 + 补丁的预期 md5 | — |
| `probe_s3b_launch.py` | **S3b**：非侵入式拉起 ComfyUI + 后台采样 pin 计数 → CSV | ComfyUI 的 |
| `probe_s3b_analyze.py` | **S3b**：读 CSV，判定 R1「升到顶后回落」 | 0 PASS / 1 FAIL / 3 WARN / 4 INCONCLUSIVE |
| `probe_h1_hostbuf_fill.py` | **S4-H1**：证明「文件→hostbuf(RAM)」写入正确字节（零 ComfyUI 依赖） | 0 OK / 1 FAIL / 2 API_FAIL |
| `probe_h2_hostbuf_h2d.py` | **S4-H2**：证明「hostbuf(RAM)→XPU 显存」写入正确字节 | 0 OK / 1 FAIL / 2 API_FAIL / 3 skip(非XPU) |
| `probe_s4_hash.py` | **S4**：输出产物 sha256 快照 + 对照（RAM 开 vs 关） | 0 一致 / 1 不一致 |
| `probe_rollback_check.py` | **回滚**：校验 env=0 退回磁盘直通且门禁不退化 | 0 PASS / 1 FAIL |

沿用既有门禁（**不新造**）：`run_acceptance.py`（三正/两负标记 + SD1.5 出图 + MiniMax H3 出片）。

---

## 1. 前置：文件闸（P0，必过）

应用补丁后逐一比对 md5（唯一“部署到位”判据）：
```
cd <ComfyUI根>
git apply -p0 --check aimdo-xpu/comfyui-3tier-ram-cache.patch && git apply -p0 aimdo-xpu/comfyui-3tier-ram-cache.patch
md5sum comfy/model_management.py comfy/pinned_memory.py comfy/model_patcher.py comfy/ops.py
# 期望（见 PHASE5_EXPECTED_MD5.txt）：
#   0a9040f28c8c747b52b92009d18bf82f  model_management.py
#   c629da9c2089c7c75d3b53386f49fdc9  pinned_memory.py
#   d523b099933dad858e3a9e73a567c730  model_patcher.py
#   96090a01b13e1b81907152324b6430a0  ops.py
```
打补丁**只能**用 `git apply -p0` 或 `patch -p0 --binary`；无 `--binary` 的 GNU patch 会因 CRLF 全失败；**禁止把文件转 LF**。

---

## 2. 门禁复用（P1，必过）

```
python_embeded\python.exe aimdo-xpu\run_acceptance.py \
    --expect-version 0.5.6.dev28 --expect-commit gc2cf98fb9
```
- **三正标记**（`POS_MARKERS`，全须出现）：
  1. `comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend`
  2. `comfy-aimdo XPU backend ready (mode=native_hook)`
  3. `DynamicVRAM support detected and enabled`
- **两负标记**（`NEG_MARKERS`，须缺席）：
  1. `XPU backend not requested`
  2. `No working comfy-aimdo install detected`
- 启动命令（`run_acceptance.py` 内部）：`python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --disable-auto-launch --port 8196`，cwd=portable 根，且 `AIMDO_XPU_ENABLED` 被剥离、不传 `--enable-dynamic-vram`。
- 退出码：0 全绿 | 2 门禁 | 3 小模型 | 4 H3 | 5 超时 | 6 版本不符 | 7 主配置失败但回退成功（=未验证主配置）。

> P1 通过后，新的 RAM 缓存探针才具备意义（否则先修门禁）。

---

## 3. S3b —— R1「计数收敛」实证（P2）

**目的**：拿“`TOTAL_PIN_CACHE_MEMORY` 升到预算顶后**回落**”作为 `free_pins→partially_unload_ram` 真的在扣账的直接证据。若只增不减且到顶后恒 False，则 R1 失败（RAM 层重新变死代码）。

**步骤**（两条：RAM 开 / 回滚关）：
```
:: (a) RAM 缓存开（默认预算）
python_embeded\python.exe aimdo-xpu\probe_s3b_launch.py --out aimdo-xpu\logs\s3b_on.csv -- ^
    --windows-standalone-build --disable-auto-launch --port 8196
::   在 ComfyUI 就绪后，另开终端用 run_acceptance.py 的同一端点提交 PROMPT_small + PROMPT_h3
::   （或直接复用 run_acceptance.py，但它不采样；建议：先跑本包装器起服务，再用其 HTTP 提交）

:: (b) 完成后分析
python_embeded\python.exe aimdo-xpu\probe_s3b_analyze.py aimdo-xpu\logs\s3b_on.csv
```
**CSV 列**：`t_epoch, total_pin_cache_mb, total_pinned_mb, loaded_ram_mb, max_pinned_mb, n_models`

**判定（由 analyze 输出）**：
| analyze 输出 | 含义 | 动作 |
|---|---|---|
| `PASS`（max_cache>0 且出现 ≥64MiB 回落） | 计数收敛，R1 成立 | 记录 CSV 路径为证据 |
| `FAIL`（max_cache=0） | RAM 层未被填充 | 查 `Enabled XPU RAM cache` 行 / `DISABLE_PINNED_MEMORY` / `is_intel_xpu()` |
| `FAIL`（贴近预算却从不回落） | 疑似 R1 失败（未扣账） | 查 `free_pins`→`partially_unload_ram`；核对 R5 的 `*-fast` 子池与 `current_loaded_models` |
| `WARN`（有占用但未达预算且单调） | 负载未触顶，**不能判通过** | 换更大模型/降 `AIMDO_XPU_RAM_CACHE_GB` 或延长运行后重测 |

> 注意：`TOTAL_PINNED_MEMORY` 在 XPU 上应恒为 0（无注册）；它非 0 才是异常信号。

---

## 4. S4 —— R6「H2D 真写正确字节」实证（P3）

三条互补证据，**缺一不可**：

**H1（文件→RAM，可离线跑）**
```
python_embeded\python.exe aimdo-xpu\probe_h1_hostbuf_fill.py --size-mib 4
# 期望：HOSTBUF_FILLED_OK
```

**H2（RAM→XPU 显存，需 B580）**
```
python_embeded\python.exe aimdo-xpu\probe_h2_hostbuf_h2d.py --size-mib 4
# 期望：H2D_OK      （H2D_FAIL=首个不一致字节；H2D_API_FAIL=异常）
```
> H2 是 R6 最硬前提的直接证伪：若 C 侧 `hostbuf_read_file_slice(device_ptr=...)` 在 XPU 上“返回 True 却没真写显存”，快路径会 `return True` 跳过 torch 回退 → **静默错误**。H2 逐字节比对能抓到它。

**同 seed 出图哈希对照（端到端）**
```
:: 每次运行前清空输出目录 out\ ；固定 seed（PROMPT_*.json 内）
:: (1) RAM 开：正常启动 -> 出图 -> 快照
python_embeded\python.exe aimdo-xpu\probe_s4_hash.py snapshot <ComfyUI输出目录> aimdo-xpu\logs\s4_on.txt
:: (2) RAM 关：设 AIMDO_XPU_RAM_CACHE_GB=0 启动、同 seed 出图 -> 快照
set AIMDO_XPU_RAM_CACHE_GB=0
python_embeded\python.exe aimdo-xpu\probe_s4_hash.py snapshot <ComfyUI输出目录> aimdo-xpu\logs\s4_off.txt
:: (3) 对照
python_embeded\python.exe aimdo-xpu\probe_s4_hash.py compare aimdo-xpu\logs\s4_on.txt aimdo-xpu\logs\s4_off.txt
# 期望：IDENTICAL  —— RAM 层只改变“字节来源”，不得改变数值/图像
```
> 若 `MISMATCH`：优先怀疑 H2D 快路径写错（跑 H2 复核），或 dtype/量化路径被牵连；按 `REVIEW_3tier_ram_cache.md` R6 修复建议强制走回退分支。

---

## 5. 回滚探针（P4）

```
set AIMDO_XPU_RAM_CACHE_GB=0
python_embeded\python.exe aimdo-xpu\run_acceptance.py --small-only     :: 产出 acc_*.log
python_embeded\python.exe aimdo-xpu\probe_rollback_check.py <acc_*.log>
# 期望：
#   disabled-line present : True
#   enabled-line  present : False
#   三正 PASS / 两负 PASS
#   ROLLBACK_OK
```
**含义**：env=0 → `MAX_PINNED_MEMORY` 保持 -1 → 退回“磁盘直通”，且门禁不退化（可安全回退）。

---

## 6. 判定汇总（Phase 5 通过条件）

| 编号 | 判据 | 通过标准 | 对应 |
|---|---|---|---|
| P0 | 文件闸 | 4 文件 md5 = `PHASE5_EXPECTED_MD5.txt` | 部署到位 |
| P1 | 门禁 | 三正齐、两负缺（exit 0） | 不退化 |
| P2 | S3b | `probe_s3b_analyze` = PASS | **R1 收敛** |
| P3 | S4 | H1=OK, H2=OK, hash=IDENTICAL | **R6 真写** |
| P4 | 回滚 | `probe_rollback_check` = ROLLBACK_OK | 可回滚 |
| P5 | 出片 | 免 flag 下 SD1.5 出图 + MiniMax H3 出片（run_acceptance exit 0） | 端到端 |

**任一 P2/P3 判 FAIL → 三层卸载修复不成立，需返工**（P2 FAIL 指向 R1；P3 FAIL 指向 R6/H2D）。

---

## 7. 明确的边界与不变量

- 本工装**不修改** ComfyUI 任何源码；`probe_s3b_launch.py` 只读取 `comfy.model_management` 的模块属性，不 monkeypatch。
- 所有探针**不 import `comfy.*`**（除 S3b 包装器在目标进程内被动读取），可在非 XPU 机器上安全跑 H1/hash/rollback。
- H2/S3b 需 B580 真机与 `python_embeded`；在非 XPU 机器上 H2 会明确 `H2D_API_FAIL: ... skip`，**不静默通过**。
- 所有“不确定”一律输出非 0 退出码或 FAIL/WARN，**不默认通过**（延续本审查官的硬标准）。
