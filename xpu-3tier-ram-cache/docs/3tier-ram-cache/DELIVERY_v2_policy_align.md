# XPU 三层卸载 · v2（策略对齐上游 / NVIDIA 同策略）

> 本文是 **v2 的交付说明**，取代 `DELIVERY_3tier_ram_cache.md` 作为部署入口。
> 环境基线：Windows + Intel Arc B580（驱动 32.0.101.9034）+ torch 2.14.0+xpu + ComfyUI v0.39.0 便携版 + comfy-aimdo 0.5.6.dev28。
> 所有数字均来自本机真机运行，未做跨平台推断。

---

## 1. 一句话结论

**v2 把 XPU 的策略层完全对齐上游（NVIDIA）：默认配置下 XPU 走与 NVIDIA 相同的判定链，RAM 中间层不再被强制启用。**
核心对照数字：默认配置下 RAM 缓存峰值从 v1 的 **8177.8 MiB → 0.0 MiB**（203 个采样点全程恒 0）。

**但必须一起读**：当 RAM 层被**显式启用**时，MiniMax H3 观测到约 1/4 概率的设备级失败（`UR_RESULT_ERROR_OUT_OF_RESOURCES` → `DEVICE_LOST`）。详见 §5。这不是可以略过的脚注。

---

## 2. v2 相对 v1 改了什么

只有两处，都是「移除策略特判」，不含任何新功能：

| 位置 | 改动 |
|---|---|
| `comfy/ops.py` | `handle_pin()` 删掉 `or comfy.model_management.xpu_ram_cache_enabled()`，恢复上游三判据 `signature is None or not fast_disk or args.high_ram` |
| `comfy/model_management.py` | 删掉因此变成死代码的 `xpu_ram_cache_enabled()` 整个函数（原 1744–1757 行） |

**`ops.py` 现在与 ComfyUI 原厂备份逐字节相同**（`cmp` 证实），即 v1 在 `ops.py` 上的全部改动就是那一处覆盖。

**保留不动**（这些是「能力补齐」，不是策略）：

- `MAX_PINNED_MEMORY` 的 XPU 预算块，默认 `min(ram*0.25, 24GiB)`（本机 8178.043 MiB）
- `HOST_PIN_REGISTRATION_SUPPORTED` 守卫（XPU 无 `torch.cuda.cudart()`，无守卫会抛 `AssertionError`）
- `host_register_pin` / `host_unregister_pin` 守卫
- `TOTAL_PIN_CACHE_MEMORY` 独立记账与 `total_pin_memory_used()`
- `ensure_pin_registerable()` 的 XPU 分支（超预算时真销毁 hostbuf，而非无效的解注册）
- `AIMDO_XPU_RAM_CACHE_GB` 环境变量旋钮与 `=0` 软回退

### 文件闸（部署后必须逐字相符）

```
53c3fac684a22b8995ee0d5ba7b0becd  comfy/model_management.py
c629da9c2089c7c75d3b53386f49fdc9  comfy/pinned_memory.py   （本次未动，与 v1 相同）
d523b099933dad858e3a9e73a567c730  comfy/model_patcher.py   （本次未动，与 v1 相同）
9e3f9620541118ee30479660a9191557  comfy/ops.py             （= ComfyUI 原厂）
```
4 个文件均为 **PURE_CRLF**（裸 LF = 0）。

---

## 3. 为什么 v1 那处覆盖是错的

v1 加的 `or xpu_ram_cache_enabled()` 使 XPU 在**磁盘被判为快盘时也强制建 RAM pin**。而 NVIDIA 用户在快盘上正是走磁盘直通 —— 这是 ComfyUI 自己的策略，不是 XPU 的缺陷。

本机 SD1.5 + MiniMax H3 共 5 个模型，自动判定**全部** `fast_disk=True`（判定来自 `comfy/storage.py` 的 `state_dict_fast_disk()`，Windows 上委托 `comfy_aimdo.storage.fast_disk()` 原生扩展）。所以 v1 是在**无条件**地抵消上游策略。

---

## 4. 行为对照（本机真机实测）

| 配置 | RAM 缓存 | SD1.5 | MiniMax H3 | 判定 |
|---|---|---|---|---|
| **默认（无 flag）** | **0.0 MiB，全程恒 0** | ✓ 375,208 B | ✓ 1,085,024 B | **PASS**（推荐） |
| `--high-ram` | 触顶 8177.8 MiB，峰后回落 380.6 / 357.4 MiB | ✓ | ✓（n=2 全过） | 见 §5 |
| `--high-ram` + `GB=4` | 触顶 4095.98，回落 253.5 / 352.5 MiB | ✓ | **2 过 1 挂**（n=3） | 见 §5 |
| `--disable-fast-disk` | 触顶 8109–8176 MiB，回落 379.8 / 444.9 MiB | ✓ | **2 过 1 挂**（n=3） | 见 §5 |
| `AIMDO_XPU_RAM_CACHE_GB=0` | 恒 0（磁盘直通） | ✓ | ✓ 1,126,374 B | **PASS**（回滚档） |

**默认档的 `signature is None` 分支**：`comfy_aimdo.model_vbar` 的 `ModelVBAR.fault()` 在 native 返回 `res==1`（需宿主回读）时返回 `None`，此时上游的 `signature is None` 判据成立、会建 RAM pin。这是**原生路径**，在最需要 RAM 中间层的场景下仍然生效 —— 所以「对齐上游」不等于「砍掉功能」。
（本次默认档实测峰值恒为 0.0；1 秒采样间隔内建了又释放的 pin 可能漏采，**正反都无证据**。）

---

## 5. ⚠️ 已知风险（务必读完再决定是否启用 RAM 层）

### 现象
RAM 中间层**被启用**时，MiniMax H3 会间歇性崩在设备侧：

```
RuntimeWarning/Error: level_zero backend failed with error: 40 (UR_RESULT_ERROR_OUT_OF_RESOURCES)
  → comfy/ldm/minimax/model.py:777  torch.lerp(...)
  → 后续 torch.xpu.synchronize() 再抛 error: 20 (UR_RESULT_ERROR_DEVICE_LOST)
```

### 观测统计（全部为 H3 出片运行）

| RAM 缓存状态 | 运行数 | H3 失败数 |
|---|---|---|
| **启用**（`--high-ram` 8 GiB ×2、`--high-ram` 4 GiB ×3、`--disable-fast-disk` ×3） | 8 | **2** |
| **未启用**（默认 ×1、预算 `=0` ×1） | 2 | 0 |

### 已否证的两个猜测（别再拿它们当依据）

1. **不是宿主内存不够。** 失败那次提交内存 77.65 %、可用还剩 10,987.2 MB —— **两项都优于通过的那次**（81.69 % / 8,549.3 MB）。失败是**设备侧** OOR。
2. **不是预算太大。** 4 GiB 的样本挂了，8 GiB 的两次反而都过了。失败与缓存规模**不单调相关**。
3. **不是 `--disable-fast-disk` 特有。** `--high-ram`（走 `weights-fast` 子集）同样会挂。

### 置信度（如实标注）
- 8 次运行中 2 次失败，点估计 25 %，但 **3 中 1 的 Wilson 95 % CI 宽约 [6 %, 66 %]** —— 这个区间宽到没有工程意义，**既不能说「约四分之一」，也不能排除更高或更低的失败率**。要压到 ±10 % 需要几十次量级，本次没做。
- 「RAM 缓存未启用 = 安全」同样只有 n=2，**不足以证明**。
- **根因未定位**：未加调用计数探针，无法判定是否与 `partially_unload_ram` 的 hostbuf 销毁、VBAR 缺页解析、或驱动层问题有关。

### 因此
- **不要把 `--high-ram` 或 `--disable-fast-disk` 写成「已验证稳定的推荐配置」。**
- 稳定且零风险的档位是**默认配置**（RAM 层按上游策略休眠）与**回滚档**（预算 `=0`）。

---

## 6. 推荐配置

**就用默认配置，不加任何 flag。** 这与您要求的「NVIDIA 同策略」完全一致，也是本机唯一在真机上有「零失败记录」的档位。

若确实要试验 RAM 中间层：

```
AIMDO_XPU_RAM_CACHE_GB=4  +  --high-ram
```

- 预算仍保留默认 `min(ram*0.25, 24GiB)` = 8178 MiB **不改**（向上兼容），4 GiB 只是运行时旋钮，不写进代码。
- **请先接受 §5 的风险**：H3 可能崩设备，失败后需重启 ComfyUI。

**不要用 `--disable-fast-disk`**：它落 `weights-loaded` 子集并触发 `model_prefetch.py:116` 的 `ensure_pin_registerable()`，在 XPU 分支会**真销毁** hostbuf（NVIDIA 上只是解注册、数据保留），缓存抖动更明显，且同样有上述风险。

---

## 7. 部署与回滚

**部署**：打 `comfyui-3tier-ram-cache-v2.patch`，或直接覆盖 `deliver/comfy/` 下的 4 个文件 → 用 §2 的 md5 文件闸校验 → 直启。

**启动硬约束**：
- 用 `.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --disable-auto-launch --port 8196`
- **不要**用 `run_intel_gpu.bat`；**不要**设 `AIMDO_XPU_ENABLED`（含 `=0`）；**不要**带 `--enable-dynamic-vram`

**三级回滚**：
1. 软：`AIMDO_XPU_RAM_CACHE_GB=0`（不改文件，退回磁盘直通）
2. 补丁反向：`git apply -R`
3. 硬：还原 `comfy/*.py.bak.20261009-032052`（原厂）或 `.bak.20261009-061137`（v1）

---

## 8. 未决问题（不要当成「已解决」）

1. RAM 层启用时 H3 的设备级失败**根因未定位**（§5）。
2. `--disable-fast-disk` 档的失败是否「v2 改动前既有」，**只有代码推演、无真机对照** —— 因需要临时还原 4 个文件，风险高于收益，未做。
3. E 档（8 GiB `--high-ram`）仅 n=2，其「全过」不构成稳定性证据。
4. 跨样本的系统内存对比**不可靠**：机器基线会漂移（观测到约 2.7 GB 差异），按「本进程增量」算 8 GiB 与 4 GiB 档几乎持平（差 2.1 %）。之前「4 GiB 内存更省」的观察**已撤回**。
5. XPU 的 hostbuf 是 **pageable**（VirtualLock 锁页数 0），可被 OS 换页 —— 这是与 NVIDIA pinned 内存的本质差异，未做换页影响评估。
