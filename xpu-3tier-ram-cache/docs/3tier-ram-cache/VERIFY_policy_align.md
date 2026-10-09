# VERIFY_policy_align —— 策略对齐上游（NVIDIA）后的真机验收

验证人：policy-align-verifier
日期：2026-10-09
被测机器：Intel Arc B580 单卡 / torch 2.14.0+xpu / RAM 32,712 MB
被测目录：`E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable/ComfyUI/comfy/`

> 硬标准：结论必须来自实际运行。本文每个 PASS/FAIL 后都给出可指到的日志行、CSV 采样点或产物字节。
> 无证据的项一律写「无证据」。

---

## 0. 一句话总结论

**策略层改动已真机验证生效**：默认（快盘）路径下 XPU 的 RAM 缓存峰值从上一版的 **8177.8 MiB 降到 0.0 MiB**，与 NVIDIA 快盘行为一致，功能未退化（SD1.5 + MiniMax H3 均出图出片成功）→ **档 A PASS、档 C PASS**。
**档 B FAIL**：`--disable-fast-disk` 下 RAM 缓存确实建立起「触顶后回落」的正确行为，但 H3 在 3 次运行中 **1 次以 `UR_RESULT_ERROR_OUT_OF_RESOURCES` → `DEVICE_LOST` 失败**（另 2 次通过）—— 属间歇性设备资源失败，非确定性回归，但构成真实稳定性风险。

---

## 1. 开工前文件闸（通过）

实测 md5（与指定值逐位一致）：

| 文件 | 实测 md5 | 期望 md5 | 判定 |
|---|---|---|---|
| `model_management.py` | `53c3fac684a22b8995ee0d5ba7b0becd` | 同 | PASS |
| `pinned_memory.py` | `c629da9c2089c7c75d3b53386f49fdc9` | 同 | PASS |
| `model_patcher.py` | `d523b099933dad858e3a9e73a567c730` | 同 | PASS |
| `ops.py` | `9e3f9620541118ee30479660a9191557` | 同 | PASS |

- `ops.py` 与 `ops.py.bak.20261009-032052` 经 `cmp` **二进制比对完全相等**（md5 同为 `9e3f9620…`）。
- `ops.py:232` 源码实读：`if signature is None or not fast_disk or args.high_ram:` —— 上游三判据，无 `or xpu_ram_cache_enabled()`。

**残留调用检查**：全仓（含 `custom_nodes/`）grep `xpu_ram_cache_enabled`
- 活体 `.py` 文件：**0 命中**（grep 退出码 1）。不停手。
- 仅 2 处命中，均在旧备份：`comfy/ops.py.bak.20261009-061137:233`、`comfy/model_management.py.bak.20261009-061137:1744`。
- `custom_nodes/` 内容：`ComfyUI-OmniXPU/`、`aimdo_diag.py`、`example_node.py.example`、`websocket_image_save.py` —— 无调用点。

---

## 2. H1 / H2 附加回归（`pinned_memory.py` 本轮未改动）

日志：`logs/h1h2_20261009-062217.log`

```
H1_ENV: init_devices([0]) -> True
H1_ENV: control.lib=set host_buffer.lib=set
H1_PAYLOAD: 33554432B sha256=d9f3d3f9…73a
H1_OK (size=33554432B, hostbuf.size=33554432B) -> file->hostbuf(RAM) writes exact bytes
H1_EXIT=0
H2_ENV: torch=2.14.0+xpu xpu_avail=True device=Intel(R) Arc(TM) B580 Graphics
H2_PAYLOAD: 33554432B sha256=574bf3d7…77a
H2D_OK (size=33554432B, device=xpu:0) -> hostbuf(RAM)->XPU 显存 writes exact bytes
H2_EXIT=0
```

**判定：PASS**（32 MiB 逐字节一致，两条腿均真值，非 `HOSTBUF_API_FAIL` 伪证据）。

---

## 3. 档 A —— 默认（不带任何 storage 相关 flag）

### 3.1 启动方式（两条独立证据）

1. **进程内采样跑**（`yan_s3b_run.py`，runpy 拉起，argv 与硬约束一致）：
   日志 `logs/A-default_20261009-062300.log`，采样 `logs/A-default_pin_20261009-062300.csv`
2. **字面命令跑**（`run_acceptance.py`，真实子进程、`cwd` = portable 根、剥离 `AIMDO_XPU_ENABLED`、不带 `--enable-dynamic-vram`）：
   日志 `logs/A-default_literal_20261009-064101.log` / `_runner.log` / `_artifacts.txt`

### 3.2 门禁三正两负

字面命令跑的 runner 日志原文：

```
== gate markers ==
  [PASS] required:  'comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend'
  [PASS] required:  'comfy-aimdo XPU backend ready (mode=native_hook)'
  [PASS] required:  'DynamicVRAM support detected and enabled'
  [PASS] forbidden: 'XPU backend not requested' present=False
  [PASS] forbidden: 'No working comfy-aimdo install detected' present=False
   gate markers: ALL GREEN
...
== ALL GREEN ==
== exit 0 ==
```

进程内跑（`A-default_20261009-062300.log`）同样：三正各 1 次命中、两负 0 次命中。

### 3.3 预算仍在 / fast_disk 判定

- `A-default_20261009-062300.log:42` → `[INFO] Enabled XPU RAM cache 8178 (default = min(ram*0.25, 24GiB))`
- `A-default_literal_20261009-064101.log:39` → 同上，8178
- `Model storage policy: fast_disk=True` ×7（`A-default_literal_20261009-064101.log`，`fast_disk=True` 计数 7、`fast_disk=False` 计数 0）

### 3.4 核心对照数字（本轮最重要证据）

| 项 | 上一版实测 | 本档实测 | 判定 |
|---|---|---|---|
| `TOTAL_PIN_CACHE_MEMORY` 峰值 | **8177.8 MiB** | **0.0 MiB** | 覆盖确已移除 |
| `TOTAL_PIN_CACHE_MEMORY` 取值个数 | 多值（升到顶后回落） | **distinct = 1（恒 0）** | — |
| `sum(loaded_ram_size())` 峰值 | 8177.8 MiB | **0.0 MiB** | — |
| `TOTAL_PINNED_MEMORY` | 恒 0 | **恒 0（distinct=1）** | 符合 XPU 无注册能力设计 |
| 预算 `MAX_PINNED_MEMORY` | 8178.043 MiB | **8178.043 MiB** | 预算仍在，只是策略不再强制用 |
| 采样点数 | 181 | **203** | — |

采样器健康性佐证：档 A 的 `n_dyn_models` 列取值 `{0,1,2,3,4}`（204 行 CSV，203 个采样点），说明采样器确实看到动态模型在载入/卸载；在此前提下 `loaded_ram_mb` 仍恒为 0 —— **不是「采样器没数据」，而是「模型真的没有 RAM 驻留」**。

> 说明：任务书预期「`signature is None` 的原生路径仍会零星建 pin，缓存不一定恒为 0」。本轮实测**恒为 0**，即 5 个模型在 `fast_disk=True` 下均未触发该分支。**局限**：采样间隔 1 s，1 s 内建了又释放的 pin 可能被漏采 —— 这一点无证据可排除。

### 3.5 产物

| 作业 | 文件 | 字节 | 门限 | 判定 |
|---|---|---|---|---|
| SD1.5（进程内跑） | `ComfyUI/output/p3_small/p3_small_00002_.png` | 375,208 | >100 KB | PASS |
| MiniMax H3（进程内跑） | `ComfyUI/output/p3_h3/p3_h3_00002_.mp4` | 1,085,024 | >1 MB | PASS |
| SD1.5（字面命令跑） | `ComfyUI/output/p3_small/p3_small_00007_.png` | 374,984 | >100 KB | PASS |
| MiniMax H3（字面命令跑） | `ComfyUI/output/p3_h3/p3_h3_00006_.mp4` | 1,077,877 | >1 MB | PASS |

H3 进程内跑原文：`[INFO] Prompt executed in 180.74 seconds`。

### 3.6 全量日志错误扫描

`grep -cE "AttributeError|NameError|ImportError|Traceback"` 于 `A-default_20261009-062300.log` → **0**。
字面命令跑：runner 无 error 段，exit 0。

### 3.7 档 A 判定：**PASS**

---

## 4. 档 B —— 加 `--disable-fast-disk`

共跑 **3 次**（同参数、同硬约束），因为第 1 次出现 FAIL。

### 4.1 配置生效确认

三次日志 `fast_disk=False` 计数均为 **7**、`fast_disk=True` 计数 **0**（`grep -o "fast_disk=[A-Za-z]*" | uniq -c`）。

### 4.2 RAM 缓存行为（三次均达标）

| 运行 | 日志 | 采样 CSV | 峰值 | 触顶率 | 峰值后最低 | 峰值后回落 | distinct | 单调 |
|---|---|---|---|---|---|---|---|---|
| run1 | `logs/B-nofastdisk_20261009-063000.log` | `B-nofastdisk_pin_20261009-063000.csv` | 8109.062 MiB | 99.2% | 7729.271 | **379.791 MiB** | 20 | False |
| run2 | `logs/B-nofastdisk_rerun_20261009-063500.log` | `B-nofastdisk_rerun_pin_20261009-063500.csv` | 8176.055 MiB | 99.98% | 7731.117 | **444.938 MiB** | 46 | False |
| run3 | `logs/B-nofastdisk_run3_20261009-064000.log` | `B-nofastdisk_run3_pin_20261009-064000.csv` | 8176.055 MiB | 99.98% | 7731.117 | **444.938 MiB** | 45 | False |

三次 `TOTAL_PINNED_MEMORY` 均 **distinct=1、恒 0**（XPU 无驱动注册能力，字节记入独立计数器）。
三次 `sum(loaded_ram_size())` 与 `TOTAL_PIN_CACHE_MEMORY` 数值完全一致。

**「必须看到升到预算顶后回落」→ 达标**：三次均为 0 → 触顶（≈8178 预算）→ 真实回落，**不是「只升不落」**。run3 的回落明细（采样点索引, 降幅 MiB）：`#152: 369.571`、`#140: 332.801`、`#170: 288.129`、`#164: 272.896`、`#122: 224.315` —— 多段回落，说明驱逐链在持续工作。

run1 缓存曲线（全部采样值，MiB）：
```
0 0 0 0 0 0 0 0 0 0 0 72.375 236.812 260.813 626.119 1528.188 1845.316 1875.927
1875.963 1969.088 0.0 1483.75 2434.875 3919.141 5440.898 7070.312 7876.289
8001.406 8109.062 8109.062 7874.918 7729.271 7729.271 7729.271 7729.271
```
（注意 `#20` 处有一次归 0 后重新爬升 —— 与任务书提示的「`model_prefetch.py:116` 触发 `ensure_pin_registerable()`，XPU 分支会真的销毁 hostbuf」现象一致。）

### 4.3 稳定性：**FAIL**

| 运行 | SD1.5 | MiniMax H3 | 结果 |
|---|---|---|---|
| run1 | PASS 375,384 B (`p3_small_00003_.png`) | **FAIL** —— 无产物 | FAIL |
| run2 | PASS 375,094 B (`p3_small_00005_.png`) | PASS 1,079,827 B (`p3_h3_00004_.mp4`) | PASS |
| run3 | PASS 375,476 B (`p3_small_00006_.png`) | PASS 1,115,395 B (`p3_h3_00005_.mp4`) | PASS |

run1 失败原文（`logs/B-nofastdisk_20261009-063000.log`）：

```
150:[ERROR] !!! Exception during processing !!! level_zero backend failed with error: 20 (UR_RESULT_ERROR_DEVICE_LOST)
...
246:RuntimeError: level_zero backend failed with error: 40 (UR_RESULT_ERROR_OUT_OF_RESOURCES)
      File "…\ComfyUI\comfy\ldm\minimax\model.py", line 777, in _forward
        t_emb = torch.lerp(table[i0], table[i0 + 1], (pos - i0).unsqueeze(1))
263:RuntimeError: level_zero backend failed with error: 20 (UR_RESULT_ERROR_DEVICE_LOST)
      File "…\comfy\model_management.py", line 2220, in synchronize  -> torch.xpu.synchronize()
279:RuntimeError: level_zero backend failed with error: 20 (UR_RESULT_ERROR_DEVICE_LOST)
      File "…\comfy\model_management.py", line 2233, in soft_empty_cache -> torch.xpu.synchronize()
[INFO] Prompt executed in 12.29 seconds
```

H3 在 12.29 s 处终止（成功时约 180 s），`B-nofastdisk_result.json` 中 `h3.status = "error"`、`ok=false`、`artifacts=[]`。

run2 / run3 对同一批日志做 `grep -cE "DEVICE_LOST|OUT_OF_RESOURCES|Traceback"` → **0 / 0**。

### 4.4 门禁

三次门禁三正各 1 次命中、两负 0 次命中（run3 逐条核对：`published 1 SYCL queue`=1、`backend ready (mode=native_hook)`=1、`DynamicVRAM support detected and enabled`=1、`XPU backend not requested`=0、`No working comfy-aimdo install detected`=0）。**门禁未退化。**

### 4.5 系统内存 / 换页迹象（run3，独立进程采样 198 s，覆盖 H3 全段）

`logs/B-nofastdisk_run3_sysmem_20261009-064000.csv`（94 行，窗口 `1791499062 → 1791499260`）

| 指标 | 实测 |
|---|---|
| 物理可用内存最低 | 6,394.7 MB |
| 内存占用百分比峰值 | 80.5 % |
| 提交内存峰值 | 38,680.9 MB / 上限 43,464.2 MB = **88.99 %** |
| 提交内存剩余最低 | 4,783.3 MB |
| ComfyUI 进程 RSS 峰值 | 14,508.6 MB |

**判读**：提交内存最高到 88.99% —— 有明显压力，但**未见提交耗尽**（始终 ≥4.78 GB 余量），未见提交失败/换页的直接证据。run1 的失败发生在**设备侧**（`UR_RESULT_ERROR_OUT_OF_RESOURCES` 是 level-zero 设备资源错误，非宿主内存分配失败），因此**不能**用「宿主 pageable 内存被换页」解释。
**无证据**支持「换页导致失败」这一归因，也不否定它（本轮未抓到 pagefile I/O 计数器）。

### 4.6 缓存命中率 / `weights-loaded` 驱逐优先级

**无证据**。本轮工装未接命中/未命中计数器，也未针对 `weights-loaded` vs `weights-fast` 分别采样；只能观测到聚合的 `TOTAL_PIN_CACHE_MEMORY` 曲线（多段 ~220–370 MiB 的回落），无法给出命中率数字。

### 4.7 档 B 判定：**FAIL**（缓存行为达标，H3 不稳定）

失败归因的**边界说明**（避免误读）：
按 `ops.py:232` `if signature is None or not fast_disk or args.high_ram:`，当 `fast_disk=False` 时 `not fast_disk` 已为真，被删掉的 `or xpu_ram_cache_enabled()` **在该路径下不改变判定结果**。因此 `--disable-fast-disk` 的行为在改前/改后**应无差异** —— 本轮未对改前版本跑 `--disable-fast-disk`，故「不是本次改动引入的回归」这一结论**基于代码推演，无直接真机对照证据**。
可确证的只有：默认路径（档 A）全绿；`--disable-fast-disk` 路径下 3 次中 1 次设备资源失败。

---

## 5. 档 C —— 回滚（`AIMDO_XPU_RAM_CACHE_GB=0` + `--disable-fast-disk`）

日志 `logs/C-rollback_20261009-063200.log`，采样 `logs/C-rollback_pin_20261009-063200.csv`

### 5.1 软回退生效

```
42:[INFO] XPU RAM cache disabled (AIMDO_XPU_RAM_CACHE_GB=0); disk passthrough mode
```
- `XPU RAM cache disabled` 计数 = **1**
- `Enabled XPU RAM cache` 计数 = **0**（缺席 ✓）

### 5.2 门禁（未退化）

`logs/C-rollback_probecheck.txt`（`probe_rollback_check.py` 输出）：

```
disabled-line present : True
enabled-line  present : False  (must be False)
[PASS] required  'comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend'
[PASS] required  'comfy-aimdo XPU backend ready (mode=native_hook)'
[PASS] required  'DynamicVRAM support detected and enabled'
[PASS] forbidden 'XPU backend not requested' present=False
[PASS] forbidden 'No working comfy-aimdo install detected' present=False
ROLLBACK_OK
```

### 5.3 RAM 缓存恒为 0

205 个采样点，`cache_mb` / `pinned_mb` / `loaded_ram_mb` 三项均为 **max=0.0、min=0.0、distinct=1**；预算列为空（`MAX_PINNED_MEMORY` 保持 `-1`，退回磁盘直通）。

### 5.4 产物

| 作业 | 文件 | 字节 | 判定 |
|---|---|---|---|
| SD1.5 | `ComfyUI/output/p3_small/p3_small_00004_.png` | 375,352 | PASS |
| MiniMax H3 | `ComfyUI/output/p3_h3/p3_h3_00003_.mp4` | 1,126,374 | PASS |

错误扫描：`grep -cE "AttributeError|NameError|ImportError|Traceback|DEVICE_LOST"` → **0**。

### 5.5 档 C 判定：**PASS**

---

## 6. 汇总

| 档位 | 配置 | 门禁 | RAM 缓存行为 | SD1.5 | H3 | 判定 |
|---|---|---|---|---|---|---|
| A | 默认 | 三正齐两负缺 | 峰值 **0.0 MiB**（上一版 8177.8） | PASS | PASS | **PASS** |
| B | `--disable-fast-disk` | 三正齐两负缺 | 触顶 8176→回落 445 MiB ✓ | PASS | **1/3 FAIL** | **FAIL** |
| C | `AIMDO_XPU_RAM_CACHE_GB=0` + `--disable-fast-disk` | 三正齐两负缺 | 恒 0 ✓ | PASS | PASS | **PASS** |
| H1/H2 | 独立 hostbuf 探针 | — | — | — | — | **PASS** |

**关键数字**：`8177.8 MiB → 0.0 MiB`（档 A，203 采样点，distinct=1）。

---

## 7. 产物清单

日志目录：`C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/logs/`

档 A：
- `A-default_20261009-062300.log`（进程内跑，ComfyUI 全量日志）
- `A-default_pin_20261009-062300.csv`（203 采样点）
- `A-default_result.json`（作业结果）
- `A-default_analyze.txt`（聚合分析）
- `A-default_literal_20261009-064101.log` / `_runner.log` / `_artifacts.txt`（字面命令跑）

档 B：
- `B-nofastdisk_20261009-063000.log` + `B-nofastdisk_pin_20261009-063000.csv` + `_result.json` + `_analyze.txt`（run1，H3 FAIL）
- `B-nofastdisk_rerun_20261009-063500.log` + `_pin_*.csv` + `_result.json` + `_analyze.txt`（run2，PASS）
- `B-nofastdisk_run3_20261009-064000.log` + `_pin_*.csv` + `_result.json` + `_analyze.txt`（run3，PASS）
- `B-nofastdisk_run3_sysmem_20261009-064000.csv`（系统内存/提交内存）
- `B-nofastdisk_sysmem_20261009-063000.csv`（run1 窗口，含进程退出后时段，仅供参考）

档 C：
- `C-rollback_20261009-063200.log` + `C-rollback_pin_20261009-063200.csv` + `C-rollback_result.json` + `C-rollback_analyze.txt` + `C-rollback_probecheck.txt`

其它：
- `h1h2_20261009-062217.log`（H1/H2 回归）
- 新增工装（不改被测源码）：`yan_sysmem.py`（系统内存采样器）、`yan_pin_analyze.py`（CSV 聚合分析器）

---

## 8. 收尾状态

- `netstat` 中 **8196 无 LISTENING**，`tasklist` 中 **无 python 进程** —— 无残留、无独占冲突。
- **未修改** 4 个被测源码，**未删除**任何 `.bak.*` 备份。
- 未使用 `run_intel_gpu.bat`；未设 `AIMDO_XPU_ENABLED`（含 `=0`）；未带 `--enable-dynamic-vram`；每档跑完均确认端口释放后再启动下一档。
- 未复用 `probe_h1_hostbuf_fill.py` / `probe_h2_hostbuf_h2d.py` / `probe_s3b_launch.py`（三者判据已被证伪）。

---
---

# 9. 档 E —— `--high-ram`（默认预算）+ 备选档 F（`AIMDO_XPU_RAM_CACHE_GB=4`）

验证人：highram-verifier
日期：2026-10-09（接续第 0–8 节，本节点开始在原文后追加，未改动任何既有观测与数字）
被测机器 / 目录 / 文件闸：同第 1 节（开工与收尾两次 md5 复核，见 9.9）

> 样本数声明：档 E **2 个样本**（run1 / run2），备选档 F **1 个样本**。
> 2 个样本**不足以支撑统计显著性**，本档结论只对这 2 次运行负责，不做「必然稳定」的推断。

---

## 9.1 启动方式与硬约束

`cwd` = `E:/aiwork/ComfyUI_windows_portable_intel/ComfyUI_windows_portable`
未使用 `run_intel_gpu.bat`；未设 `AIMDO_XPU_ENABLED`（子进程环境中**显式剥离**，模型中 `assert "AIMDO_XPU_ENABLED" not in env` 通过）；未带 `--enable-dynamic-vram`；档 E 未加 `--disable-fast-disk`。

实际执行的 ComfyUI argv（与分配的启动硬约束字面一致，仅前置了进程内采样器以便读取模块全局量）：

```
python_embeded\python.exe -s -u <TOOLKIT>\yan_s3b_run.py --port 8196 \
    --out-csv <...>_pin.csv --out-json <...>_result.json --interval 1.0 --hard-timeout 2100 -- \
    --windows-standalone-build --disable-auto-launch --port 8196 --high-ram
```

`yan_s3b_run.py` 内部以 `runpy.run_path(MAIN, run_name="__main__")` 拉起同一进程，`sys.argv` 即为上面 `--` 之后的全部参数，故「进程命令行参数 & cwd」约束等价。为什么不直接用字面 `ComfyUI\main.py`：`TOTAL_PIN_CACHE_MEMORY` / `MAX_PINNED_MEMORY` / `loaded_ram_size()` 都在 ComfyUI 进程内的模块全局量里，外部进程读不到，必须进程内采样；这与第 3–5 节（A/B/C 档）是同一做法。

新增工装（均不改被测源码）：
- `yan_e_run.py`（档 E 监督器：启动前自检 → 拉子进程 → 并行拉系统内存采样器 → 收树 → 端口释放确认 → 日志收割）
- `yan_sysmem_analyze.py`（系统/提交内存 CSV 聚合器，`proc_rss_mb` 为空的行标记为进程退出后基线，**不计入峰值**，防止拿缺失冒充 0）

> ⚠️ 工装踩坑记录：`tasklist /FI "IMAGENAME eq python.exe"` 在 Git Bash 里 `/FI` 会被路径转换吃掉（报错为 `'D:/workbuddy/resources/vendor/PortableGit/FI'`），必须走 `subprocess` 列表传参绕过 shell。另外监督器自己也是 `python.exe`，最初的「残留进程自检」恒命中自己，导致连续两次拒绝启动 —— 已修为排除 `os.getpid()`（实测佐证：当时唯一条目 `python.exe PID=6428` 就是检查进程自身）。这两次拒绝都发生在子进程创建之前，**未产生任何日志桩**（已 `ls` 确认）。

---

## 9.2 门禁三正两负

**run1**（`logs/E-highram_20261009-065622_run1.log`）：

| 项 | 日志 | 判定 |
|---|---|---|
| `published N SYCL queue(s)` | L18 `[INFO] comfy-aimdo XPU: published 1 SYCL queue(s) to the native backend` count=1 | **PASS** |
| `backend ready (mode=native_hook)` | L20 `[INFO] comfy-aimdo XPU backend ready (mode=native_hook)` count=1 | **PASS** |
| `DynamicVRAM support detected and enabled` | L52 `[INFO] DynamicVRAM support detected and enabled` count=1 | **PASS** |
| `XPU backend not requested` | count=0 | **PASS** |
| `No working comfy-aimdo install detected` | count=0 | **PASS** |

**run2**（`logs/E-highram_20261009-070121_run2.log`）：五项同上（三正 count=1，两负 count=0）→ **PASS**。

---

## 9.3 `Enabled XPU RAM cache 8178` 与 `Model storage policy: fast_disk=`

两个样本均出现（count=1，原文）：

- run1 L47：`[INFO] Enabled XPU RAM cache 8178 (default = min(ram*0.25, 24GiB))` → **PASS**
- run2：`[INFO] Enabled XPU RAM cache 8178 (default = min(ram*0.25, 24GiB))` → **PASS**

`Model storage policy` 条数与取值（**本档与 `--disable-fast-disk` 的分水岭，重点确认项**）：

| 样本 | 条数 | 取值 |
|---|---|---|
| run1 | **7** | `grep -o "fast_disk=[A-Za-z]*" \| sort \| uniq -c` → **`7 fast_disk=True`**，无一条 False |
| run2 | **7** | 全部 `fast_disk=True` |
| F-gb4 | **7** | 全部 `fast_disk=True` |

→ **PASS**。`--high-ram` 下 `fast_disk` **仍为 True**，即落到 `weights-fast` 子集（`comfy/ops.py:189` `subset = "weights-fast" if fast_disk else "weights"`），与 `--disable-fast-disk` 走 `weights` / `weights-loaded` 的路径**确实不同**。第 1 节注明的拳脚之分已实证成立。

---

## 9.4 RAM 缓存曲线（`TOTAL_PIN_CACHE_MEMORY` / `TOTAL_PINNED_MEMORY` / `MAX_PINNED_MEMORY` / `loaded_ram_size()`）

| 指标 | run1（174 采样） | run2（175 采样） | F-gb4（190 采样） |
|---|---|---|---|
| `MAX_PINNED_MEMORY`（预算） | 8178.043 MiB | 8178.043 MiB | 4096.000 MiB |
| `TOTAL_PIN_CACHE_MEMORY` 峰值 | **8177.79 MiB @ #46** | **8177.79 MiB @ #46** | **4095.983 MiB @ #46** |
| 峰值 vs 预算 | 差 0.25 MiB（**已触顶**） | 同 | 差 0.017 MiB（触顶） |
| 处于预算 ±1 MiB 的采样数 | 102 / 174 | 101 / 175 | 118 / 190 |
| 峰值后最低（tail_min） | 7797.179 MiB | 7820.354 MiB | 3842.456 MiB |
| **峰后回落 drop** | **380.611 MiB** | **357.436 MiB** | **253.527 MiB** |
| 单调性 | `monotonic=False`（**有回落**） | 同 | 同 |
| 进程退出时残留 | 8142.635 MiB | 8142.635 MiB | 4089.73 MiB |
| `TOTAL_PINNED_MEMORY` | 恒 0.0，distinct=1 | 同 | 同 |
| `sum(loaded_ram_size())` | max 8177.79，distinct=37（与 cache 同值同形状） | max 8177.79，distinct=36 | max 4095.983，distinct=31 |

**判据「必须触顶后回落，只升不落按 FAIL」→ PASS**（`monotonic=False`，drop 357–381 MiB）。

> 诚实注记：**回落幅度只有 4.4%–4.7%**（380.6 / 8177.8），且发生在最后约 25 个采样点。它证明缓存**会**释放，但**不证明**释放在更重的压力（例如并发多路、更大批次、更小 RAM 的机器）下够用。
> `loaded_ram_size()` 与 `TOTAL_PIN_CACHE_MEMORY` 逐点同值（`distinct` 数量也一致），是「缓存字节＝模型侧真实 RAM 占用」的强互证 —— 不是只有一层记账在动。

---

## 9.5 核心判据：SD1.5 出图 + MiniMax H3 出片

| 样本 | 作业 | 产物路径 | 字节 | 阈值 | 判定 |
|---|---|---|---|---|---|
| run1 | SD1.5 | `ComfyUI/output/p3_small/p3_small_00008_.png` | **375,221** | >102,400 | **PASS** |
| run1 | MiniMax H3 | `ComfyUI/output/p3_h3/p3_h3_00007_.mp4` | **1,100,768** | >1,048,576 | **PASS** |
| run2 | SD1.5 | `ComfyUI/output/p3_small/p3_small_00009_.png` | **375,048** | >102,400 | **PASS** |
| run2 | MiniMax H3 | `ComfyUI/output/p3_h3/p3_h3_00008_.mp4` | **1,105,967** | >1,048,576 | **PASS** |
| F-gb4 | SD1.5 | `ComfyUI/output/p3_small/p3_small_00010_.png` | **375,382** | >102,400 | **PASS** |
| F-gb4 | MiniMax H3 | `ComfyUI/output/p3_h3/p3_h3_00009_.mp4` | **1,121,413** | >1,048,576 | **PASS** |

以上字节数以 `os.path.getsize()`（result.json）与磁盘 `ls -la` 双重核对，两者一致。

Prompts 执行状态：`status="success"`（run1/run2 两个作业皆然，见 `*_result.json`）。

**错误扫描**（监督器逐模式原样扫描，`logs/E-highram_*_run*.log` 全量）：

| 模式 | run1 | run2 | F-gb4 |
|---|---|---|---|
| `UR_RESULT_ERROR_` | **0** | **0** | **0** |
| `Traceback` | **0** | **0** | **0** |
| `AttributeError` | **0** | **0** | **0** |
| `DEVICE_LOST` | **0** | **0** | **0** |
| `RuntimeError` | **0** | **0** | **0** |

→ 三档均未复现 B 档那种 `UR_RESULT_ERROR_OUT_OF_RESOURCES` → `DEVICE_LOST`。

日志中仅有的 4 条 WARNING（两个样本相同，均为既有非致命项，与 pin/RAM 无关）：
单 GPU 强切、`Torch already imported`、`aimdo_diag.py did not return a ComfyExtension`、`unet unexpected: ['adaln_basis','adaln_mean']`。

---

## 9.6 系统内存全程采样（重点）

来源：`logs/E-highram_*_run*_sysmem.csv`；**仅统计 `proc_rss_mb` 非空（进程存活）窗口**，进程退出后基线单列。

| 指标 | run1（92/93 行存活） | run2（92/93 行存活） | **参照：B 档 run3** |
|---|---|---|---|
| 提交上限 `ullTotalPageFile` | 43,464.2 MB | 43,464.2 MB | 43,464.2 MB |
| **提交内存峰值** | **37,216.7 MB** | **37,277.4 MB** | 38,680.9 MB |
| **提交占比峰值** | **85.63 %** | **85.77 %** | **88.99 %** |
| **可用内存最低点** | **6,451.5 MB** | **6,418.9 MB** | 6,394.7 MB |
| **`python.exe` RSS 峰值** | **14,588.5 MB** | **14,631.8 MB** | 未提供（无证据） |
| 存活窗口 | 182.0 s | 182.0 s | — |

**与 B 档 run3 对照 → E 档系统内存压力更小，不是更糟：**

- 提交峰值低 **1,464.2 MB**（run1）/ **1,403.5 MB**（run2）
- 提交占比低 **3.36 pp**（run1）/ **3.22 pp**（run2）
- 可用内存最低点**高** 56.8 MB（run1）/ 24.2 MB（run2）

**内存归还检查**（峰值后进程退出，`.csv` 末行回到基线）：

| 样本 | 启动基线 avail | 退出后 avail | 启动基线 commit_used | 退出后 commit_used |
|---|---|---|---|---|
| run1 | 21,276.6 MB | 21,376.4 MB | 13,069.8 MB | 12,984.2 MB |
| run2 | 21,321.3 MB | 21,444.7 MB | 13,033.3 MB | 12,905.7 MB |

→ 均回到启动水平，**无内存泄漏迹象**（PASS，仅 2 样本）。

---

## 9.7 三处已知风险的量化 / 与「无证据」声明

先核实现存代码（不凭描述，读源码）：

| 风险点 | 源码实证 | 状态 |
|---|---|---|
| `ensure_pin_budget()` 首行 return True | `comfy/model_management.py:747-749`：`def ensure_pin_budget(size, evict_active=False, loaded=False):` → L748-749 `if args.high_ram:` / `return True` | **确实存在** |
| `pinned_hostbuf_size()` 变 `size*2` | `comfy/model_management.py:1712-1715`：`def pinned_hostbuf_size(size):` → L1713-1714 `if args.high_ram:` / `return max(0, int(size * 2))` | **确实存在** |
| `ram_release_callback` 变 None | `execution.py:747`：`ram_release_callback = self.caches.outputs.ram_release if self.cache_type == CacheType.RAM_PRESSURE else None`；`comfy/cli_args.py:291-292`：`if args.high_ram:` / `args.cache_classic = True` | **确实存在** |

量化结果：

1. **`ensure_pin_budget()` 跳过可用性检查** —— 量化后果：缓存**长期贴着预算上限**（102/174、101/175 个采样点落在 ±1 MiB 内），把系统推到 **提交占比 85.63/85.77%、可用内存只剩 6,418–6,452 MB**。在这 2 次运行中**没有**发生页互换失败或 OOM。
   **边界（无证据）**：这只能说明「本机 32,712 MB RAM + 43,464 MB 提交上限 + 本 workload」撑住了。RAM 更少 / 提交上限更紧的机器上会不会爆，**本次未测，无证据**。

2. **`pinned_hostbuf_size()` 的 `size*2`** —— 在 pin 记账层**无证据**：`TOTAL_PINNED_MEMORY` 全程恒 0.0（distinct=1），XPU 路径不往这个会计通道写数，所以 `size*2` 在本档数据中**无法被直接观测**。其后果只能间接体现在进程 RSS（峰值 14,588.5 / 14,631.8 MB）与系统提交峰值上。**我不声称它是好是坏 —— 这一个 API 的实际放大倍数本档拿不到真值。**

3. **`ram_release_callback=None`（内存告急时不再主动销毁 pin）** —— 量化后果：同上系统内存三项指标。**「classic 缓存比 RAM_PRESSURE 多留了多少」本档无法隔离**：档 A（不带 flag）当时未采系统内存 CSV（见第 7 节产物清单，A 档无 sysmem 文件），所以**没有同 workload 的 CLASSIC vs RAM_PRESSURE 对照 → 无证据**，只能用 B 档 run3 作不同 flag 的跨档参照。

---

## 9.8 备选档 F：`--high-ram` + `AIMDO_XPU_RAM_CACHE_GB=4`（1 样本）

`logs/E-highram-gb4_20261009-070454_f4.log` / `_f4_pin.csv` / `_f4_sysmem.csv` / `_f4_result.json`

- 日志出现 `[INFO] Enabled XPU RAM cache 4096 (AIMDO_XPU_RAM_CACHE_GB=4.0)` → 旋钮生效
- `Model storage policy` 7 条，全部 `fast_disk=True` → **三层仍在**
- 三层行为保持：预算 4096.000 MiB，峰值 4095.983 MiB @ #46，触顶后回落 253.527 MiB，`monotonic=False`
- 出图出片：SD1.5 **375,382 B**、H3 **1,121,413 B**，`status="success"`，错误扫描五项全 0
- 系统内存：提交峰值 **33,280.8 MB = 76.57 %**（比 E 档低 **9.20 pp**、比 B 档 run3 低 **12.42 pp**），可用内存最低 **10,486.9 MB**（E 档约 6,435 MB → 多出约 4.05 GB 头寸），RSS 峰值 10,579.5 MB

→ **若目标是「既保住三层又压住内存头寸」，`AIMDO_XPU_RAM_CACHE_GB=4` 是本轮实测中性价比最高的一档。**
这只是 **1 个样本**，不足以判定稳定性；也不构成「推荐默认改为 4 GiB」的依据（默认预算的取舍涉及不同 workload，未测）。

---

## 9.9 档 E 判定

| 项 | 判据 | 结果 |
|---|---|---|
| 门禁三正 | 三条必需日志各出现 ≥1 次 | **PASS**（run1/run2 各 count=1） |
| 门禁两负 | 两条禁止日志必须缺 | **PASS**（count=0） |
| `Enabled XPU RAM cache 8178` | 日志出现 | **PASS**（L47 原文） |
| `Model storage policy: fast_disk=` | 须仍为 True | **PASS**（7/7 全 True） |
| RAM 缓存峰值 | 应达 ~8178 MiB | **PASS**（8177.79 MiB @ #46） |
| 触顶后回落（不回落 = FAIL） | `monotonic=False` 且有 drop | **PASS**（drop 380.611 / 357.436 MiB） |
| SD1.5 > 100 KB | 产物字节 | **PASS**（375,221 / 375,048 B） |
| MiniMax H3 > 1 MB | 产物字节 | **PASS**（1,100,768 / 1,105,967 B） |
| 无 `UR_RESULT_ERROR_*` / Traceback / AttributeError | 五项扫描全 0 | **PASS** |
| 系统内存不比 B 档 run3 更糟 | 提交占比 ≤ 88.99%、可用最低 ≥ 6,394.7 MB | **PASS**（85.63 / 85.77 %；6,451.5 / 6,418.9 MB） |
| 进程退出后内存归还 | 回到启动基线 | **PASS**（差值 <100 MB） |

### 档 E 总判定：**PASS**（2/2 样本全通过；无一例设备资源失败）

**一句话**：`--high-ram` 是一条**比 `--disable-fast-disk` 更稳的三层开启方式** —— 它保持了 `fast_disk=True`（走 `weights-fast` 子集），在**同等默认预算 8178.043 MiB** 下同样触到 8177.79 MiB 并出现峰后回落，且 SD1.5 + H3 两次全通过、零 `UR_RESULT_ERROR`，同时系统提交内存压力**低于** B 档 run3（85.63/85.77 % vs 88.99 %）。

**关于「是否绕开了每块一次的宿主缓存销毁路径」**：源码依据成立 ——
`comfy/model_prefetch.py:115-116`：`if not (comfy_modules and comfy_modules[0]._pin_state["fast_disk"]): comfy.model_management.ensure_pin_registerable(registerable_size)`；
而 `ensure_pin_registerable` 的 XPU 分支（`comfy/model_management.py:776-790`）在 `HOST_PIN_REGISTRATION_SUPPORTED` 为假时走 `free_pins(shortfall + PIN_PRESSURE_HYSTERESIS, …)` → `partially_unload_ram` → 真正截断 hostbuf（NVIDIA 分支只做 `free_registrations` 解注册、数据保留）。
由于 E 档 7/7 条 `fast_disk=True`，该条件为假 → **`ensure_pin_registerable` 在 E 档不被触发**（源码推断）。
⚠️ 但这是**源码推断，不是运行时探针**：本档**没有**直接探测该函数在 XPU 分支上的实际调用计数。日志面也拿不到间接证据 —— 对 `unload|truncate|unregister` 的 grep 在 E 档两份日志里均为 0 命中，但在 B 档 run3 日志里**同样为 0 命中**，故日志面**无法区分**两档在这条路径上的行为。要硬证需另加调用计数探针（本次未做）。

**保留意见（必须随结论交付）**：
1. 样本数 2，**不构成统计显著**；B 档教训是「3 次里挂 1 次」，因此 E 档「暂未见失败」与「不会失败」之间有本质区别。若判的稳定性标准高于「2 次通过」，需更多样本。
2. 峰值后回落只有 **~4.5%**（357–381 MiB），**不等于**释放在更重负载下够用。
3. 第 9.7 节三项风险的**边界部分标注为「无证据」**，未测。
4. `--high-ram` 会同时打开 `cache_classic=True`（`cli_args.py:291`）等**副作用**，它是一个「开一堆东西」的开关而不是「只开三层」的开关；本档数字是**整个开关组合**的结果，不能单归因于 RAM 缓存。

---

## 9.10 本档新增产物

`C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/logs/`

档 E run1（20261009-065622）：
- `E-highram_20261009-065622_run1.log`（ComfyUI 全量日志 + 监督器 header/footer）
- `E-highram_20261009-065622_run1_pin.csv`（174 采样）
- `E-highram_20261009-065622_run1_sysmem.csv`（93 行）
- `E-highram_20261009-065622_run1_result.json`

档 E run2（20261009-070121）：
- `E-highram_20261009-070121_run2.log` / `_run2_pin.csv`（175） / `_run2_sysmem.csv`（93） / `_run2_result.json`

备选档 F（20261009-070454）：
- `E-highram-gb4_20261009-070454_f4.log` / `_f4_pin.csv`（190） / `_f4_sysmem.csv`（101） / `_f4_result.json`

历史 JSON（由 `yan_s3b_run.py` 落）：`s3b_20261009-065637_small_hist.json`、`s3b_20261009-065646_h3_hist.json`、`s3b_20261009-070137_small_hist.json`、`s3b_20261009-070146_h3_hist.json`、`s3b_20261009-070510_small_hist.json`、`s3b_20261009-070519_h3_hist.json`

新增工装（不改被测源码）：`yan_e_run.py`（监督器）、`yan_sysmem_analyze.py`（系统内存聚合器）

---

## 9.11 收尾状态

- **文件闸复核（跑完后）**：`md5sum` 四文件与第 1 节闸值逐位一致 ——
  `53c3fac684a22b8995ee0d5ba7b0becd  model_management.py`
  `c629da9c2089c7c75d3b53386f49fdc9  pinned_memory.py`
  `d523b099933dad858e3a9e73a567c730  model_patcher.py`
  `9e3f9620541118ee30479660a9191557  ops.py`
  （`*` 前缀 = 二进制模式读取，CRLF 行尾未被转换）
- 4 个文件**未修改**；8 个 `.bak.*` 备份**全部仍在**（已 `ls` 核对）。
- `netstat`：8196 **无 LISTENING / 无 ESTABLISHED**（仅客户端侧 TIME_WAIT，非占用）；`tasklist` 中**无残留 python.exe**（唯一的一条是执行检查的自身进程）。
- 每轮跑完均 `taskkill /F /T` 收树 → 确认端口释放 → 才起下一轮；共 3 轮（run1 / run2 / F），无交叉污染。

---

# 10. 档 F 补样 —— `--high-ram` + `AIMDO_XPU_RAM_CACHE_GB=4`，n=1 → n=3

## 10.0 为什么要补

第 9.8 节的档 F 只有 **1 个样本**，却因系统内存数字好看（提交峰值 76.57 %、可用最低 10,486.9 MB，明显优于档 E 的 85.63–85.77 % / 6,419–6,452 MB）而很可能被写进交付文档当**推荐配置**。
档 B 的教训是「3 次里挂 1 次」—— 一个只跑过 1 次的档位，**没有资格**被称为稳定。本节把这个档位抬到 n=3。

**一句话总结论（先给结论，再给证据）**：
**补跑的 run2 通过、run3 失败** —— 档 F 的 H3 出片 **2/3 通过、1/3 失败**，失败形态与档 B run3 **逐字同类**（`UR_RESULT_ERROR_OUT_OF_RESOURCES` → `UR_RESULT_ERROR_DEVICE_LOST`，死在 MiniMaxH3 的 `Model Initializing` 阶段）。
因此 **档 F 不能作为「已验证稳定」的推荐配置交付**；也不能因为它系统内存好看就优先于档 E —— 它和档 E 一样，都带着同一条**未排除的间歇设备资源失败**。

## 10.1 启动方式与硬约束（两个新样本与第 1 样本完全一致）

两个样本均走同一条命令链（`yan_e_run.py` 监督器 → `yan_s3b_run.py` 进程内 runpy + 采样 + 提交），逐字复刻第 9.8 节 F 第 1 样本的调用：

```
环境变量：AIMDO_XPU_RAM_CACHE_GB=4      （显式设置）
剥离项  ：AIMDO_XPU_ENABLED              （监督器 env.pop + assert 不在环境里）
cwd     = E:\aiwork\ComfyUI_windows_portable_intel\ComfyUI_windows_portable
子进程  ：python_embeded\python.exe -s -u ComfyUI\main.py \
            --windows-standalone-build --disable-auto-launch --port 8196 --high-ram
```

- **未**使用 `run_intel_gpu.bat`；**未**设 `AIMDO_XPU_ENABLED`；**未**加 `--enable-dynamic-vram`；**未**加 `--disable-fast-disk`。
- 未使用 `AIMDO_XPU_RAM_CACHE_GB=4` 之外的任何旋钮。
- 单卡独占：每轮启动前用 `netstat -ano` 确认 8196 无 LISTENING/ESTABLISHED、用 `tasklist /FI "IMAGENAME eq python.exe"`（**subprocess 列表传参**，避开 Git Bash 的 `/FI` 路径转换）确认除监督器自身（`os.getpid()` 排除）外**无残留 python.exe**；不满足即拒绝启动（返回码 5）。两轮**均**打印 `precheck OK` 才起进程。
- 每轮跑完 `taskkill /F /T` 收树 → 轮询确认端口 FREE → 才起下一轮。run2 与 run3 之间**无交叉污染**。

## 10.2 门禁三正两负 + 预算字样（3/3 全中）

三个样本的日志均逐条命中，无一例外：

| 门禁项 | F 第1样本 | run2 | run3 |
|---|---|---|---|
| `published 1 SYCL queue(s)` | ✓ count=1 | ✓ count=1 | ✓ count=1 |
| `backend ready (mode=native_hook)` | ✓ count=1 | ✓ count=1 | ✓ count=1 |
| `DynamicVRAM support detected and enabled` | ✓ count=1 | ✓ count=1 | ✓ count=1 |
| `XPU backend not requested`（须缺） | 0 ✓ | 0 ✓ | 0 ✓ |
| `No working comfy-aimdo install detected`（须缺） | 0 ✓ | 0 ✓ | 0 ✓ |
| `Enabled XPU RAM cache 4096 (AIMDO_XPU_RAM_CACHE_GB=4.0)` | ✓ | ✓ | ✓ |
| `Model storage policy: fast_disk=True` | 7/7 | 7/7 | 7/7 |
| `Model storage policy: fast_disk=False` | 0 | 0 | 0 |

`fast_disk=` 分布：三份日志**均为 7 条全 True、0 条 False**，与上一轮档 E 的分布一致，符合「`--high-ram` 走 `weights-fast` 子集」的预期。

## 10.3 RAM 缓存曲线（3/3 触顶并回落）

采样源：`comfy.model_management` 的 `TOTAL_PIN_CACHE_MEMORY` / `TOTAL_PINNED_MEMORY` / `MAX_PINNED_MEMORY`，以及模型侧 `sum(model.loaded_ram_size() for 动态加载模型)`（`loaded_ram_size()` 挂在 `ModelPatcher` 上，不是 `LoadedModel` —— 这是仓库里 `probe_s3b_launch.py` 产出恒 0 伪证据的根因，本轮未使用该脚本）。

| 样本 | 采样数 | budget (`MAX_PINNED_MEMORY`) | 峰值 `TOTAL_PIN_CACHE_MEMORY` | 峰后 tail_min | drop | `monotonic` | 首次非 0 | `loaded_ram_size()` 峰值 | `TOTAL_PINNED_MEMORY` |
|---|---|---|---|---|---|---|---|---|---|
| F 第1样本 | 190 | 4096.0 MiB | **4095.983** MiB @ #46 | 3842.456 | **253.527** MiB | False | #11 = 234.562 | 4095.983 MiB | max 0.0（distinct=1） |
| run2 | 197 | 4096.0 MiB | **4095.983** MiB @ #52 | 3743.437 | **352.546** MiB | False | #12 = 73.502 | 4095.983 MiB | max 0.0（distinct=1） |
| run3 | 35 | 4096.0 MiB | **4094.324** MiB @ #27 | 3945.733 | **148.591** MiB | False | #11 = 72.375 | 4094.324 MiB | max 0.0（distinct=1） |

判据核对：
- **触顶**：三样本峰值 4094.3–4096.0 MiB，均贴满 4096 MiB 预算。**PASS**。
- **峰后回落**：三样本 `monotonic=False` 且 drop > 0（148.6 / 352.5 MiB）。**不是「只升不落」**。**PASS**（但见 10.7 保留意见 2：回落幅度只有 3.6–8.6 %）。
- **`TOTAL_PINNED_MEMORY` 恒 0**：三样本 `max=0.0`、`distinct=1`，符合「XPU 无驱动注册」的设计预期。**PASS**。
- run3 只有 35 个采样（进程 44.2 s 就退出），其峰值 4094.324 是在 H3 崩之前达到的，不是完整负载下的峰值 —— **这一行的可比性弱于前两个样本**，如实标注。

## 10.4 核心判据：SD1.5 出图 + MiniMax H3 出片

| 样本 | SD1.5 产物 | 字节 | 判据 >100 KB | H3 产物 | 字节 | 判据 >1 MB |
|---|---|---|---|---|---|---|
| F 第1样本 | `ComfyUI\output\p3_small\p3_small_00010_.png` | 375,382 | **PASS** | `ComfyUI\output\p3_h3\p3_h3_00009_.mp4` | 1,121,413 | **PASS** |
| run2 | `ComfyUI\output\p3_small\p3_small_00011_.png` | 375,017 | **PASS** | `ComfyUI\output\p3_h3\p3_h3_00010_.mp4` | 1,123,486 | **PASS** |
| run3 | `ComfyUI\output\p3_small\p3_small_00012_.png` | 375,368 | **PASS** | **无产物**（`status=error`） | — | **FAIL** |

**SD1.5：3/3 通过。MiniMax H3：2/3 通过、1/3 失败。**

### run3 的失败原样记录（不省略、不 paraphrase）

`logs/F-gb4_20261009-130138_run3.log:154-283`，三条 traceback：

1. **:154-250** —— `!!! Exception during processing !!! level_zero backend failed with error: 20 (UR_RESULT_ERROR_DEVICE_LOST)`
   栈：`execution.py:547 execute` → `samplers.py:335 _calc_cond_batch` → `model_base.py:254 _apply_model` → `comfy/ldm/minimax/model.py:777 _forward`
   → **`RuntimeError: level_zero backend failed with error: 40 (UR_RESULT_ERROR_OUT_OF_RESOURCES)`**
   （触发点：`t_emb = torch.lerp(table[i0], table[i0 + 1], (pos - i0).unsqueeze(1))`）
2. **:254-267** —— 处理上述异常的 `comfy.model_management.reset_cast_buffers()` → `model_management.py:2220 synchronize()` → `torch.xpu.synchronize()`
   → `RuntimeError: ... error: 20 (UR_RESULT_ERROR_DEVICE_LOST)`
3. **:270-283** —— `main.py:416 prompt_worker` → `comfy.model_management.soft_empty_cache()` → `model_management.py:2233 soft_empty_cache` → `torch.xpu.synchronize()`
   → `RuntimeError: ... error: 20 (UR_RESULT_ERROR_DEVICE_LOST)`

死亡位置：进度条停在 `0%| | 0/8 [00:00<?, ?it/s, Model Initializing ... ]`，即 **MiniMaxH3 的 `Model Initializing` 阶段、8 步的第 0 步**（`logs/...run3.log:153`）。
对照：run2 / F 第1样本在同一位置打印 `0/8 [00:17<?]` → `Model Initialization complete!` 并跑满 8/8。

**与档 B run3 的同型性**：失败形态、异常码序列（`40 OUT_OF_RESOURCES` → `20 DEVICE_LOST`）、死亡阶段（`Model Initializing`）**与档 B run3 逐字同类**。这是同一条间歇设备资源失败路径的又一次出现，**不是本档新引入的回归**。

### 排除了「系统内存不足导致」这一解释（重要）

run3 崩溃时刻的系统内存并不紧张，反而**好于**通过了的 run2：

| | 崩溃/峰值时提交占比 | 崩溃/峰值时可用内存最低点 |
|---|---|---|
| run3（**失败**） | 77.65 % | **10,987.2 MB** |
| run2（**通过**） | 81.69 % | **8,549.3 MB** |

run3 失败时可用内存还有 ~11 GB、提交占比只有 77.65 %，**均优于**通过了的 run2（8,549.3 MB / 81.69 %）。
**结论：系统 RAM 压力不是 run3 失败的成因；这是设备侧（显存）资源失败。** 因此「档 F 系统内存数字好看」**不能**被解读为「档 F 更不容易崩」。

## 10.5 错误计数（逐样本、逐模式）

| 模式 | F 第1样本 | run2 | run3 |
|---|---|---|---|
| `UR_RESULT_ERROR_` | 0 | 0 | **4** |
| `Traceback` | 0 | 0 | **3** |
| `AttributeError` | 0 | 0 | 0 |
| `DEVICE_LOST` | 0 | 0 | **3** |
| `RuntimeError` | 0 | 0 | **3** |

判据要求核心判据相关错误**计数为 0**：**F 第1样本 PASS、run2 PASS、run3 FAIL**（4/3/0/3/3）。

## 10.6 系统内存全程采样（三样本对照）

口径（`yan_sysmem_analyze.py`）：只在 `proc_rss_mb` 非空（进程存活）的窗口内取峰值/谷值；进程退出后的基线单独报告，不计入峰值。

| 指标 | F 第1样本（20261009-070454） | run2（20261009-125719） | run3（20261009-130138，**失败早退**） |
|---|---|---|---|
| 采样行数（进程存活） | 100 | 104 | **22** |
| 存活窗口 | 198.0 s | 206.1 s | **42.0 s** |
| 提交上限 `commit_limit` | 43,464.2 MB | 43,464.2 MB | 43,464.2 MB |
| **提交内存峰值** | 33,280.8 MB | 35,503.9 MB | 33,750.4 MB |
| **提交占比峰值** | **76.57 %** | **81.69 %** | 77.65 % |
| **可用内存最低点** | 10,486.9 MB | **8,549.3 MB** | 10,987.2 MB |
| python RSS 峰值 | 10,579.5 MB | 10,595.7 MB | **8,315.2 MB**（早退，未到顶） |
| 启动基线 可用 / 提交 | 21,377.6 / 12,976.1 MB | **19,342.5 / 15,628.9 MB** | **19,399.2 / 15,332.6 MB** |
| 进程退出后 可用 / 提交 | 21,401.4 / 12,956.9 MB | 19,546.4 / 15,318.8 MB | 19,508.5 / 15,226.6 MB |

**必须带着基线读这张表 —— 这是本轮最重要的一个发现**：
第 9.8 节把 F 第 1 样本的「提交峰值 76.57 %」与档 E 的「85.63–85.77 %」直接对比，得出「F 明显优于 E」。但两个样本的**机器基线并不相同**：
- F 第1样本启动时可用 21,377.6 MB / 提交已用 12,976.1 MB → **本进程增量约 20,304.7 MB**
- run2 启动时可用 19,342.5 MB / 提交已用 15,628.9 MB → **本进程增量约 19,875.0 MB**

即：**按「本进程实际增量」算，两个样本几乎持平（20,305 vs 19,875 MB，差 430 MB / 2.1 %）**；76.57 % 与 81.69 % 的差距**主要来自基线不同**（机器当时已被其它程序多占了约 2.7 GB 提交、少 2.0 GB 可用），**不是档 F 配置本身的差异**。

因此：**第 9.8 节「F 档系统内存明显优于 E 档」这一观察，在 n=3 下不再成立**；至少应改写为「绝对值受机器基线主导，按进程增量计 F 与自身前一样本一致」。
⚠️ 与档 E 的跨档对比同样受此影响：档 E 两样本与档 F 三样本**不在同一基线下采集**，第 9.6/9.8 节的跨档内存对比**建议降级为参考值而非结论依据**，除非重跑时先固定基线。

**内存归还**：三样本退出后均回到启动基线水平（可用差值 <250 MB），无泄漏迹象。

## 10.7 判定

| 判据 | 要求 | F 第1样本 | run2 | run3 |
|---|---|---|---|---|
| 门禁三正 | 全中 | PASS | PASS | PASS |
| 门禁两负 | 必缺 | PASS | PASS | PASS |
| `Enabled XPU RAM cache 4096` | 出现且为 4096 | PASS | PASS | PASS |
| RAM 缓存触顶 | ≈4096 MiB | PASS（4095.983） | PASS（4095.983） | PASS（4094.324） |
| 触顶后回落 | `monotonic=False` 且 drop>0 | PASS（253.5） | PASS（352.5） | PASS（148.6） |
| `TOTAL_PINNED_MEMORY` 恒 0 | 符合 XPU 设计 | PASS | PASS | PASS |
| `fast_disk=True` 分布 | 全 True | PASS（7/7） | PASS（7/7） | PASS（7/7） |
| SD1.5 > 100 KB | 出图成功 | PASS（375,382 B） | PASS（375,017 B） | PASS（375,368 B） |
| H3 > 1 MB | 出片成功 | PASS（1,121,413 B） | PASS（1,123,486 B） | **FAIL（无产物）** |
| 错误计数 0 | 五项全 0 | PASS | PASS | **FAIL（4/3/0/3/3）** |
| 系统内存采样 | 报出峰值/最低点 | PASS | PASS | PASS（但窗口仅 42 s） |

### 档 F 总判定：**FAIL / 2 通过 1 失败**（n=3）

- **SD1.5：3/3 通过**（375,382 / 375,017 / 375,368 B）—— 稳定。
- **MiniMax H3：2/3 通过、1/3 失败** —— **不稳定**。
- **错误计数判据：run3 不成立** —— 档 F **不满足**「核心判据零错误」这一条。

### n=3 够不够？—— 直说：**不够。**

- **够的部分**：n=3 已经足以**否证**「档 F 稳定」—— 因为 3 次里就出现了 1 次与档 B 同型的 `DEVICE_LOST` 崩。要把 F 当推荐配置交付，这个结果**是反证而不是佐证**。
- **不够的部分**：n=3 **不足以量化失败率**。3 次 1 次失败的 Wilson 95 % 置信区间大致落在 **[6 %, 66 %]**（点估计 33 %），区间宽到没有工程意义 —— 既不能说「约 1/3」，也不能排除「实际更低/更高」。要给出可用于交付文档的数字，需要**显著更多样本**（粗略地说，把区间压到 ±10 % 量级需要几十次量级；本次**未做**）。
- **也不足以定位根因**：本次只记录了现象（死在 MiniMaxH3 `Model Initializing`，设备 OOR），**没有**加调用计数探针去证明是哪条路径在 XPU 上申请失败；这一点与第 9.9 节保留意见一致，**仍属「无证据」**。
- **基线不可控**：10.6 节显示跨样本基线漂移可达 2.7 GB 提交，n=3 且基线未固定，**任何跨样本/跨档的内存对比都只能当参考**。

### 对交付文档的直接建议（基于本轮证据）

1. **不要把档 F（或档 E）写成「已验证稳定的推荐配置」** —— 档 F 现在有 1/3 的 H3 失败实据，档 E 也只有 n=2。
2. 若必须给推荐，**安全档是档 C（`AIMDO_XPU_RAM_CACHE_GB=0` + `--disable-fast-disk`，RAM 层恒 0，A/B/C 三档中唯一无任何设备失败记录的开启方式）**；它的代价是 RAM 中间层实际不参与，收益为零 —— 这是**诚实的取舍**，应原样写给用户。
3. 第 9.8 节「F 档系统内存明显优于 E 档」的结论**建议撤下或降级**（见 10.6：差距主要来自机器基线，按进程增量计几乎持平）。

## 10.8 本档新增产物

`C:/Users/HE/WorkBuddy/2026-10-08-23-50-54/aimdo-xpu/logs/`

run2（20261009-125719）：
- `F-gb4_20261009-125719_run2.log`（ComfyUI 全量日志 + 监督器 header/footer）
- `F-gb4_20261009-125719_run2_pin.csv`（197 采样）
- `F-gb4_20261009-125719_run2_sysmem.csv`（105 行）
- `F-gb4_20261009-125719_run2_result.json`
- `F-gb4_run2_supervisor.txt`（监督器 stdout 全量留档）
- 历史 JSON：`s3b_20261009-125737_small_hist.json`、`s3b_20261009-125749_h3_hist.json`

run3（20261009-130138，**失败样本**）：
- `F-gb4_20261009-130138_run3.log`（含 :154-283 三条 traceback 原文）
- `F-gb4_20261009-130138_run3_pin.csv`（35 采样）
- `F-gb4_20261009-130138_run3_sysmem.csv`（24 行）
- `F-gb4_20261009-130138_run3_result.json`（`ok=false`，h3 `status=error`）
- `F-gb4_run3_supervisor.txt`
- 历史 JSON：`s3b_20261009-130155_small_hist.json`、`s3b_20261009-130207_h3_hist.json`

第 1 样本沿用 9.10 节已有的 `E-highram-gb4_20261009-070454_f4.*`（**未被覆盖**）。

复用工装（未改动）：`yan_e_run.py`（监督器）、`yan_s3b_run.py`（进程内 runpy + 采样 + 提交）、`yan_sysmem.py` / `yan_sysmem_analyze.py`、`yan_pin_analyze.py`、`run_acceptance.py`、`PROMPT_small.json`、`PROMPT_h3.json`。
**刻意未用**：`probe_h1_hostbuf_fill.py`、`probe_h2_hostbuf_h2d.py`、`probe_s3b_launch.py`（三个已被真机证伪的脚本，见任务书第五节）。

## 10.9 收尾状态

- **文件闸复核（两轮跑完后）**：4 文件 md5 与第 1 节闸值**逐位一致**，未修改：
  ```
  53c3fac684a22b8995ee0d5ba7b0becd *model_management.py
  c629da9c2089c7c75d3b53386f49fdc9 *pinned_memory.py
  d523b099933dad858e3a9e73a567c730 *model_patcher.py
  9e3f9620541118ee30479660a9191557 *ops.py
  ```
  （`*` = 二进制模式读取，CRLF 行尾未被转换）
- `.bak.*` 备份仍为 **8 个**，未删除。
- `netstat -ano`：8196 **无 LISTENING、无 ESTABLISHED**。
- 残留 `python.exe`：**0**（`tasklist` 经 subprocess 列表传参，排除检查进程自身 PID 后为空）。
- 两轮均 `taskkill /F /T` 收树 → 确认端口 FREE → 才起下一轮；run2 与 run3 之间无交叉污染。
- 本轮**未修改任何被测源码**，也未修改本文件第 0–9 节任何内容（本节为纯追加）。
