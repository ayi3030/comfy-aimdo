#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""H2 探针（自写，替代不可用的 probe_h2_hostbuf_h2d.py）。

证什么：证明「hostbuf(RAM) -> XPU 显存」这条腿真的写入正确字节。
这是排除「C 侧返回 True 却没真写显存」这个**静默错误**的关键：首次 fault 命中
hostbuf 分支后快路径 `return True` 会跳过 torch 回退拷贝，若 H2D 未真写，结果错且不报错。

与 H1 同样的重写理由：原脚本缺 control.init()，host_buffer.lib 恒 None -> 假 API_FAIL。

判据：XPU 显存内容 == 写入文件内容（逐字节）。
  通过 -> H2D_OK         退出 0
  不一致 -> H2D_FAIL     退出 1（含首个不一致字节位置）
  异常/API 不可用 -> H2D_API_FAIL 退出 2
  非 XPU 机器 -> H2D_SKIP 退出 3（**明确 skip，不静默通过**）

约束：不启动 ComfyUI、不 import comfy.*；只在系统临时目录读写；不触碰被测 4 文件。
"""
import argparse
import hashlib
import os
import sys
import tempfile
import traceback


def _init_backend():
    import comfy_aimdo.control as control
    ok = control.init("xpu")
    if not ok:
        print(f"H2_NOTE: control.init('xpu') returned {ok} "
              f"(allocator backend not activated; attempting hostbuf anyway)")
    sys.modules.pop("comfy_aimdo.host_buffer", None)
    from comfy_aimdo import host_buffer as hb
    if hb.lib is None:
        hb.lib = control.lib
    # hostbuf_* 依赖平台/设备初始化（ComfyUI main.py:285 调 control.init_devices()）。
    if control.lib is not None:
        dev_ok = control.init_devices([0])
        print(f"H2_ENV: init_devices([0]) -> {dev_ok}")
    return control, hb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mib", type=int, default=4)
    a = ap.parse_args()

    try:
        import torch
    except Exception:
        print("H2D_API_FAIL: cannot import torch")
        traceback.print_exc()
        return 2

    if not (hasattr(torch, "xpu") and torch.xpu.is_available()):
        print("H2D_SKIP: torch.xpu not available on this machine (not a real H2D test).")
        return 3

    try:
        control, hb = _init_backend()
    except Exception:
        print("H2D_API_FAIL: cannot init/import comfy_aimdo host_buffer")
        traceback.print_exc()
        return 2
    if hb.lib is None:
        print("H2D_API_FAIL: host_buffer.lib is None after control.init")
        return 2
    print(f"H2_ENV: torch={torch.__version__} xpu_avail={torch.xpu.is_available()} "
          f"device={torch.xpu.get_device_name(0) if hasattr(torch.xpu, 'get_device_name') else '?'}")

    n = a.size_mib * 1024 * 1024
    payload = os.urandom(n)
    print(f"H2_PAYLOAD: {n}B sha256={hashlib.sha256(payload).hexdigest()}")

    fd, path = tempfile.mkstemp(prefix="yan_h2d_", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)

        dest = torch.empty(n, dtype=torch.uint8, device="xpu")
        dev_index = dest.device.index
        if dev_index is None:
            dev_index = torch.xpu.current_device()
        buf = hb.HostBuffer(0, 8 * 1024 * 1024, max(n * 2, 16 * 1024 * 1024))
        buf.extend(n, register=False)

        with open(path, "rb") as fh:
            # 复刻 memory_management.py:70-74 的 device 侧调用（device_ptr != 0 => H2D）。
            buf.read_file_slice(fh, 0, n, offset=0, stream=0,
                                device_ptr=dest.data_ptr(), device=int(dev_index))
        torch.xpu.synchronize()

        got = bytes(dest.cpu().numpy().tobytes())
        if got == payload:
            print(f"H2D_OK (size={n}B, device={dest.device}) "
                  "-> hostbuf(RAM)->XPU 显存 writes exact bytes")
            return 0
        first = next((i for i, (x, y) in enumerate(zip(got, payload)) if x != y), None)
        ndiff = sum(1 for x, y in zip(got, payload) if x != y)
        print(f"H2D_FAIL: first mismatch at byte {first}, differing_bytes={ndiff}")
        return 1
    except Exception:
        print("H2D_API_FAIL:")
        traceback.print_exc()
        return 2
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
