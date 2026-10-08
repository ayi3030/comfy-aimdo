# Phase 5 真机全量验收报告：XPU 三层卸载（RAM 中间缓存）

> 执行人：砚验证（comfyui-workflow-validator）· 2026-10-09 · 任务 #33
> 目标机：Intel Arc B580（驱动 32.0.101.9034）/ torch 2.14.0+xpu / ComfyUI 0.39.0 / comfy-aimdo 0.5.6.dev28
> 启动硬约束（全程遵守）：`cwd=portable 根`，`python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --disable-auto-launch --port 8196`；
> 未用 `run_intel_gpu.bat`；**未设** `AIMDO_XPU_ENABLED`；**未带** `--enable-dynamic-vram`；子进程显式剥离 `AIMDO_XPU_ENABLED`。
> 所有证据路径相对 `C:\Users\HE\WorkBuddy\2026-10-08-23-50-54\aimdo-xpu\`。

---

## 0. 结论速览

| 编号 | 项 | 判定 | 一句话证据 |
|---|---|---|---|
| P0 | 文件闸 | **PASS** | 4 文件 md5 与冻结值逐一相符（见 §1） |
| P1 | 免 flag 门禁 | **PASS** | 三正齐、两负缺；新增行 `Enabled XPU RAM cache 8178`（§2） |
| P2 | S3b 计数收敛（R1） | **PASS** | 缓存升到预算顶 8178 MiB 后回落（峰值后 drop=270 MiB），XPU 上 `TOTAL_PINNED_MEMORY` 恒 0（§3） |
| P3-H1 | 文件→hostbuf(RAM) | **PASS** | 32 MiB 逐字节一致（§4.1） |
| P3-H2 | hostbuf(RAM)→XPU 显存 | **PASS** | 32 MiB 逐字节一致（§4.2） |
| P3-hash | 同 seed 开/关字节对照 | **INCONCLUSIVE**（判据在本机不可成立） | 同配置两次出图 sha256 即不同；on×off 差异量级 == 基线（§4.3） |
| P4 | 回滚 | **PASS** | `AIMDO_XPU_RAM_CACHE_GB=0` → `XPU RAM cache disabled … disk passthrough mode`，门禁不退化（§5） |
| P5 | 出图/出片 | **PASS** | SD1.5 PNG 375,001 B；MiniMax H3 MP4 1,120,626 B = 124 帧/24fps/5.1667s（§6） |

**对“RAM 中间层被关掉”这一缺陷的裁决：修复成立。** RAM 层确实建立（P1 新日志行 + P2 计数上升）、确实被填充到预算顶、且在触顶后确实发生驱逐回落（P2），H2D 拷贝逐字节正确（P3-H2）。
**唯一未能实机证实的是“字节级数值不变”这一条**——但原因已查明是**本机管线本身不可复现**，**不是**本补丁引入（§4.3）。该条**不判 PASS 也不判 FAIL，如实标 INCONCLUSIVE**。

---

## 1. P0 文件闸（唯一权威值）

```
cd <ComfyUI>/comfy && md5sum model_management.py pinned_memory.py model_patcher.py ops.py
0f7c056c0d45645f81b4256f3d56703a *model_management.py       ✓
c629da9c2089c7c75d3b53386f49fdc9 *pinned_memory.py          ✓
d523b099933dad858e3a9e73a567c730 *model_patcher.py          ✓
96090a01b13e1b81907152324b6430a0 *ops.py                    ✓
a517cfa1fc6f2be64fc0a47546494c54  comfyui-3tier-ram-cache.patch (306 行) ✓
```
与 `PHASE5_EXPECTED_MD5.txt` 现行值一致。`PHASE5_EXPECTED_MD5.txt` 中记为“历史（已作废）”的 `0a9040f2…` **未采用**。
验收全程结束后**再次复核**，4 文件 md5 未变（未被验收过程污染）。

---

## 2. P1 免 flag 门禁

证据日志：`logs/run_on.log`（Run ON）、`logs/run_on_final.log`、`logs/run_off.log`（回滚）。
检查器：`yan_logcheck.py`（自写，只读日志）。

```
== RAM cache lines ==
   [INFO] Enabled XPU RAM cache 8178 (default = min(ram*0.25, 24GiB))
[PASS] enabled-line present  = True
== gate markers ==
  [PASS] required  'comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend'
  [PASS] required  'comfy-aimdo XPU backend ready (mode=native_hook)'
  [PASS] required  'DynamicVRAM support detected and enabled'
  [PASS] forbidden 'XPU backend not requested' present=False
  [PASS] forbidden 'No working comfy-aimdo install detected' present=False
GATE_PASS
```
- 机器 RAM 32,712 MB（`Total VRAM 11876 MB, total RAM 32712 MB`），默认预算 `min(ram*0.25,24GiB)=8178 MiB`（日志原文）。
- 新增启动行 `Enabled XPU RAM cache …` = **本补丁生效的第一眼证据**。

---

## 3. P2 S3b —— R1「计数收敛」（本次核心）

采样器：`yan_s3b_run.py`（进程内，只读 `comfy.model_management` 模块全局量，**不 monkeypatch、不改被测源码**）。
采样列：`TOTAL_PIN_CACHE_MEMORY` / `TOTAL_PINNED_MEMORY` / `MAX_PINNED_MEMORY` / `sum(model.loaded_ram_size())`。

**主证据（默认预算，Run ON final）**：`logs/s3b_on_final.csv`（181 样本）
```
python probe_s3b_analyze.py logs/s3b_on_final.csv
samples=181  max_cache=8177.8MiB@#47  tail_min=7907.8MiB  drop=270.0MiB  budget=8178.043
cache monotonic non-decreasing: False  plateau_reached(>= 0.5*budget): True
PASS: observed a significant cache drop after peak -> eviction decrements the budget.
```
（Run ON 首轮 `logs/s3b_on.csv` 独立复现：`drop=202.5MiB`，PASS。两次结论一致。）

**逐样本轨迹**（`logs/s3b_on.csv`，t 为相对秒）：
```
idx   t_rel   cache(MiB)  n_dyn
  0     0.0        0.0     0       (空载)
 10    10.2       72.4     1
 20    20.6     1483.8     1
 30    30.6     8066.8     2
 40    40.6     8176.3     2       (逼近预算顶 8178)
 45..152        8177.8     2       (贴顶)
153   153.8     7993.13    3   ←  贴顶后回落 184.7 MiB（新动态模型 n=2→3 触发驱逐）
160   160.8     7975.27    4   ←  再次回落 190.9 MiB（n=3→4）
163   163.8     8142.64    4       (重新填充)
```
**判定依据**：
1. 缓存**升到预算顶**（8177.8 ≈ budget 8178.043 MiB）→ RAM 中间层确实被填充到有界预算；
2. 触顶后**发生回落**（idx153、idx160 等，随新模型载入触发驱逐），峰值后最大回落 **270 MiB（≥64 MiB 门限）**；**非单调**（`monotonic=False`）→ `free_pins → partially_unload_ram → TOTAL_PIN_CACHE_MEMORY` 的扣账链**真的在执行**；
3. 上升期也有多次显著回落（最大单步 **1968 MiB**），进一步佐证驱逐。

**对照不变量**：`TOTAL_PINNED_MEMORY` 全程 **恒为 0**（`distinct values = ['0.0']`）——符合“XPU 无驱动注册能力、缓存字节记入独立计数器”的设计；`loaded_ram_size()` 与 `TOTAL_PIN_CACHE_MEMORY` 数值一致（RAM 层真实占用 == 已缓存 pin 字节）。

> **未发现“只升不落”**。故 P2 = PASS。（若只升不落，将按硬标准判 FAIL，本节未出现该情形。）

---

## 4. P3 S4 —— H2D 真写 + 数值正确性

### 4.1 H1：文件 → hostbuf(RAM)（`logs/h1_20261009-034729.log`）
```
H1_ENV: init_devices([0]) -> True
H1_PAYLOAD: 33554432B sha256=f7c98a9302904b06d67845ffa2313faab909f164d13b38cdd4cab257ffe0215c
H1_OK (size=33554432B, hostbuf.size=33554432B) -> file->hostbuf(RAM) writes exact bytes
```
32 MiB `os.urandom` 随机载荷逐字节一致（另 4 MiB 亦 OK）。**PASS。**

### 4.2 H2：hostbuf(RAM) → XPU 显存（`logs/h2_20261009-034729.log`）
```
H2_ENV: init_devices([0]) -> True
H2_ENV: torch=2.14.0+xpu xpu_avail=True device=Intel(R) Arc(TM) B580 Graphics
H2_PAYLOAD: 33554432B sha256=68bf90fe972e595e0721b1c20a386275a4c0e90c1e085f03776104057aedbdaf
H2D_OK (size=33554432B, device=xpu:0) -> hostbuf(RAM)->XPU 显存 writes exact bytes
```
把 hostbuf 写入一块 **真实 XPU 显存**（`device_ptr=dest.data_ptr()`），`synchronize()` 后 `dest.cpu()` 与源文件逐字节比对**完全一致**（32 MiB；另 4 MiB 亦 OK）。
→ **直接证伪**了“C 侧 H2D 返回 True 却没真写显存”的静默错误。**PASS。**
（非 XPU 机器上本探针输出 `H2D_SKIP` 退出 3，**不静默通过**——本机确实跑了真 H2D。）

### 4.3 同 seed 开/关字节对照 —— **INCONCLUSIVE（判据在本机不成立）**

**实测数据**（统一 seed=42，PROMPT_small.json；快照 `logs/s4_*.txt`，原件 `artifacts/*.png`）：

| 运行 | 配置 | 产物字节 | sha256(前 16) |
|---|---|---|---|
| off_run1 | `AIMDO_XPU_RAM_CACHE_GB=0` | 375,068 | `121c63db643885531199…` |
| off_run2 | `AIMDO_XPU_RAM_CACHE_GB=0`（**同配置对照**） | 375,177 | `40b56cb1e83989c2…` |
| on_run | 默认预算（缓存开） | 375,294 | `36d5582ef748ec49…` |

**关键对照实验**：**同配置**的 off_run1 与 off_run2 **sha256 就不一致**（`probe_s4_hash.py compare` → MISMATCH）。
像素级量化（`numpy`+`PIL`，512×512×3）：

| 对照 | max 通道差 | 平均差 | 像素差异占比 |
|---|---|---|---|
| **off1 vs off2（基线）** | **26** | 0.1710 | 33.14% |
| on vs off1 | 27 | 0.1775 | 33.83% |
| on vs off2 | 34 | 0.1662 | 32.37% |

**解读**：
1. 「sha256 完全一致」这一判据在本机**本质上不可成立**——**同配置两次运行即不同**，与是否开 RAM 缓存无关。故它不是有效的补丁测试，**不能据此判 FAIL（也不判 PASS）**。
2. on×off 的差异量级（max 27/34、占比 ~32%）与 **off×off 基线（max 26、占比 33%）同量级** → **未发现补丁额外引入的误差**。
3. 该非确定性在 **ON 与 OFF 两次运行中都存在**（两者都运行 aimdo `native_hook` 后端），故属于**后端/管线性质**，而非 RAM 缓存补丁。彻底归因（例如临时禁用 aimdo 后端做对照）超出本次验收范围，**未做，如实标注**。

**该条欲防的“静默错误”已由 §4.1/§4.2 的逐字节比对覆盖**：拷贝腿正确 → 不会因 RAM 路径产生错误字节。故本项**不影响“修复成立”的裁决**，但**“字节级数值不变”本身在本机无证据，标 INCONCLUSIVE**。

---

## 5. P4 回滚（`AIMDO_XPU_RAM_CACHE_GB=0`）

`logs/run_off.log`，双重检查器（既有 `probe_rollback_check.py` + 自写 `yan_logcheck.py`）：
```
disabled-line present : True
enabled-line  present : False
[PASS] required  'comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend'
[PASS] required  'comfy-aimdo XPU backend ready (mode=native_hook)'
[PASS] required  'DynamicVRAM support detected and enabled'
[PASS] forbidden 'XPU backend not requested' present=False
[PASS] forbidden 'No working comfy-aimdo install detected' present=False
ROLLBACK_OK
```
日志原文：`[INFO] XPU RAM cache disabled (AIMDO_XPU_RAM_CACHE_GB=0); disk passthrough mode`。
→ 回滚后退回“磁盘直通”，且**免 flag 门禁三正仍全中、两负仍缺**，**未退化**。**PASS。**
（回滚 run 还成功出图：`p3_small_00001_.png` 375,068 B，status=success。）

---

## 6. P5 出图 / 出片（Run ON final，`logs/s3b_on_final_result.json`）

| 任务 | 产物 | 字节 | 结果 |
|---|---|---|---|
| small（SD1.5） | `ComfyUI/output/p3_small/p3_small_00001_.png` | **375,001** | success（>100 KB ✓） |
| h3（MiniMax H3） | `ComfyUI/output/p3_h3/p3_h3_00001_.mp4` | **1,120,626** | success（>1 MB ✓） |

H3 视频经 **PyAV 解码校验**（`av` 19.0.0）：
```
codec h264  size 864 x 480
frames 124  duration_s 5.166666666666667  fps 24.0
decoded_frames 124
```
→ **124 帧 @ 24fps / 5.1667s**，与预期完全一致；可完整解码，**非截断/损坏文件**。产物已固化到 `artifacts/onfinal_p3_h3_00001_.mp4`。

---

## 7. 工装审计（我采用/替换了哪些，为什么）

**复用（判据成立）**：
- `run_acceptance.py`：以库方式复用于提交/轮询/产物校验；其启动约束、`AIMDO_XPU_ENABLED` 剥离、三正两负、产物体积判定均正确。
- `probe_s4_hash.py`：按内容 sha256 快照/对照，逻辑正确（用于 §4.3）。
- `probe_rollback_check.py`：只读日志断言，判据成立（用于 §5）。
- `probe_s3b_analyze.py`：会话/触顶/回落判定逻辑正确，用于交叉验证 §3。

**替换（判据不成立，会静默误判）**：
1. `probe_h1_hostbuf_fill.py` —— **实测恒 FAIL**，但根因是探针缺陷：`host_buffer.py:6` 在**导入期**执行 `lib = control.lib`，而探针从未 `control.init()` → `lib` 恒 None → `if lib is not None:` 里的 argtypes 绑定被跳过 → `HostBuffer()` 抛 `AttributeError` → 打印 `HOSTBUF_API_FAIL`。**该探针永远拿不到真值。** 替换为 `yan_h1_ram_fill.py`。
2. `probe_h2_hostbuf_h2d.py` —— 同根因（缺 `control.init()`），且“非 XPU 才 skip”的分支会被上述崩溃误判为 API_FAIL。替换为 `yan_h2_ram_h2d.py`。
3. `probe_s3b_launch.py` —— 两处缺陷：(a) 只采样不提交 prompt，仓库内无“只提交”脚本配套（`run_acceptance.py` 会另起实例，无法给探针实例喂 prompt）；(b) `probe_s3b_launch.py:63` 对 `LoadedModel` 调 `loaded_ram_size()`，而该方法定义在 `ModelPatcher` 上（`model_management.py:1057`），`LoadedModel` 无此方法 → 被 `except` 吞掉 → `loaded_ram_mb` 列**恒为 0（伪证据）**。替换为 `yan_s3b_run.py`（进程内采样 + 同进程提交）。

**验收中新发现的运行事实（供后续参考）**：`hostbuf_*` 依赖 `control.init_devices()`——ComfyUI 在 `main.py:285` 调用；裸进程若只 `control.init()` 就用 hostbuf，会 **access violation**（真机实测）。故 H1/H2 探针必须先 `init_devices([0])`。

**自写脚本清单**（均落在 `aimdo-xpu/`，**未修改被测 4 文件**）：
`yan_h1_ram_fill.py`、`yan_h2_ram_h2d.py`、`yan_s3b_run.py`、`yan_logcheck.py`。

---

## 8. 证据索引

| 类别 | 路径 |
|---|---|
| 门禁/回滚日志 | `logs/run_on.log`、`logs/run_on_final.log`、`logs/run_on_small.log`、`logs/run_off.log`、`logs/run_off2.log` |
| S3b 采样 CSV | `logs/s3b_on.csv`(181)、`logs/s3b_on_final.csv`(181)、`logs/s3b_off.csv`、`logs/s3b_off2.csv` |
| 提交结果 JSON | `logs/s3b_on_result.json`、`logs/s3b_on_final_result.json`、`logs/s3b_off_result.json`、`logs/s3b_off2_result.json`、`logs/s3b_on_small_result.json` |
| history | `logs/s3b_*_hist.json` |
| H1/H2 | `logs/h1_20261009-034729.log`、`logs/h2_20261009-034729.log` |
| 哈希快照 | `logs/s4_on.txt`、`logs/s4_off.txt`、`logs/s4_off2.txt`、`logs/s4_onfinal_small.txt`、`logs/s4_onfinal_h3.txt` |
| 固化的图像/视频 | `artifacts/off_run1_p3_small_00001_.png`、`artifacts/off_run2_p3_small_00001_.png`、`artifacts/on_run_p3_small_00001_.png`、`artifacts/onfinal_p3_small_00001_.png`、`artifacts/onfinal_p3_h3_00001_.mp4` |

---

## 9. 收尾状态

- `taskkill` 已收树：`tasklist` 无 `python.exe`；`netstat` 确认 **8196 未 LISTENING**。
- 全程 B580 独占，无第二个 ComfyUI 实例。
- 文件闸 4 文件 md5 与冻结值一致（验收前后各核一次）。

## 10. 未实机验证 / 边界（如实声明）

1. **§4.3「同 seed 字节级一致」未获实机证据**：本机管线不可位复现（off×off 即不同），判据不可成立。已用 off×off 基线 + on×off 量级对照给出**“无额外误差”**的间接证据，并明确这不是 FAIL。**该项标 INCONCLUSIVE，未默认通过。**
2. 非确定性的**精确根因未定位**（可能为 torch.xpu 非确定性算子，或 aimdo 后端异步拷贝）；未做“禁用 aimdo 后端”的对照，因超出本次验收范围。
3. H3 于默认预算下**未单独做最大压力（OOM）边界测试**；本验收覆盖的是常规 124 帧出片路径。
4. **（裁决后补记）** 本机 XPU 采样存在**与 aimdo 无关的非确定性**；本轮**未定位其根因，超出本补丁范围**。任何人后续看到 §4.3 的 sha256/像素差异时，**不得**解读为「修复未验证」或「我们没查」——它是采样管线的固有性质，与本补丁改动（ComfyUI 侧 pin/缓存策略）无归因关系。

---

## 11. team-lead 裁决（2026-10-09）

> 原文裁决由 team-lead 下达，本节照录其依据与结论，**不改写 §1–§10 已记录的观测与数值**。

### (a) P3-hash 的 INCONCLUSIVE —— **接受**
依据：
1. **判据在本机不可成立**：同配置 `off1` vs `off2` 的 sha256 就不一致 ⇒ 基线本身不可复现 ⇒ 任何「on vs off 字节相同」的判据在此机器上**先天无法判定**。这是判据的适用性问题，不是补丁的问题。
2. **未发现补丁额外引入误差**：像素差 `off1×off2`（纯基线）`max=26 / 33.14%`，而 `on×off1` `max=27 / 33.83%`、`on×off2` `max=34 / 32.37%` —— **on×off 与基线同一量级**，无系统性放大。
3. **非确定性在 ON/OFF 两次都出现**（且都走 aimdo `native_hook` 后端）⇒ 归于后端/采样管线性质。
4. **该条本想防的静默错误已被更直接的证据覆盖**：H1 证明「文件→hostbuf」32 MiB 逐字节一致，H2 证明「hostbuf→真机 B580 显存」32 MiB 逐字节一致 —— **「返回 True 却没真写显存」已被直接证伪**，不依赖哈希对照。

team-lead 明确：**不因「多数项 PASS」而把该项粉饰成 PASS**；**INCONCLUSIVE 保持 INCONCLUSIVE**。

### (b) 额外「禁用 aimdo 后端」对照轮 —— **不做**
依据：「禁用 aimdo 后端」的确定性对照**回答的不是本补丁的问题**。本补丁改的是 ComfyUI 侧的 pin/缓存策略，不碰后端；而 aimdo 后端是本次要**启用**的东西，把它关掉测出的非确定性无法归因到本补丁。真正相关的对照（ON vs OFF）已做，且显示无补丁归因差异。该项**升级为「已记录未决问题」**（见 §10.4），不再烧一轮去「消灭」一个 INCONCLUSIVE。

### 最终判定
**Phase 5 验收：通过（含 1 项 INCONCLUSIVE）。** 措辞精确、不得写成「全项通过」。

---

## 12. 复现用运行事实：`hostbuf_*` 依赖 `control.init_devices()`

**结论**：`comfy_aimdo.host_buffer` 的 `hostbuf_read_file_slice` 等导出**必须在 `control.init()` 之后、且进一步调用 `control.init_devices()` 之后**才可用。ComfyUI 走的是完整启动路径：`main.py:72` `import comfy_aimdo.control` → `main.py:77/84` `control.init(...)` → `main.py:285` `control.init_devices(...)`。**独立 harness 若绕过完整启动路径，就会踩到下面两个坑。**

**坑 1（静默）**：`host_buffer.py:6` 在**导入期**执行 `lib = control.lib`。若在 `control.init()` **之前**就 `import comfy_aimdo.host_buffer`，则 `lib` 恒为 `None`，`if lib is not None:` 的 argtypes 绑定被跳过 → 之后 `HostBuffer()` 抛 `AttributeError: 'NoneType' object has no attribute 'hostbuf_allocate'`。（这正是旧 `probe_h1_hostbuf_fill.py` 的失效原因。）

**坑 2（崩溃）**：即使 `init()` 已调用、`host_buffer.lib` 已就绪，**若未调 `init_devices()`** 就调用 `read_file_slice`，会 **access violation**：
```
OSError: exception: access violation writing 0x0000000000000008
（随后 __del__ 里 hostbuf_free 二次异常：access violation writing 0x0）
```
真机（B580）实测复现。

**最短复现步骤**（`python_embeded\python.exe`，Windows）：
```python
# 失败版（只 init()，不 init_devices()）：
import comfy_aimdo.control as control
control.init("xpu")
from comfy_aimdo import host_buffer as hb          # 必须在 init() 之后导入！
b = hb.HostBuffer(0, 8*1024*1024, 16*1024*1024)
b.extend(4*1024*1024, register=False)
b.read_file_slice(open(path, "rb"), 0, 4*1024*1024, offset=0, stream=0,
                  device_ptr=0, device=None)       # <<< OSError access violation 0x8

# 通过版（补上 init_devices，与 ComfyUI main.py:285 一致）：
import comfy_aimdo.control as control
control.init("xpu")
# >>> 关键一步 <<<
control.init_devices([0])                          # = main.py:285 所做之事
from comfy_aimdo import host_buffer as hb
b = hb.HostBuffer(0, 8*1024*1024, 16*1024*1024)
b.extend(4*1024*1024, register=False)
b.read_file_slice(open(path, "rb"), 0, 4*1024*1024, offset=0, stream=0,
                  device_ptr=0, device=None)       # OK：正常填充 hostbuf
```
**要点小结**：① `host_buffer` 必须在 `control.init()` **之后**导入（argtypes 才绑定）；② `hostbuf_*` 调用前必须 `control.init_devices()`（否则 crash）。本报告 §4.1/§4.2 的 `yan_h1_ram_fill.py` / `yan_h2_ram_h2d.py` 正是按此两条实现（`H1_ENV: init_devices([0]) -> True`）。

---

## 13. 收尾确认（最终）

- 无残留进程：`tasklist /FI "IMAGENAME eq python.exe"` → 无匹配。
- 端口：`netstat` 确认 **8196 未 LISTENING**。
- 文件闸：4 源码 md5 与冻结值一致（验收前、验收后各核一次，均未变）。
- 全程 B580 独占，无第二个 ComfyUI 实例。
- 裁决后**未再启动 ComfyUI**。
