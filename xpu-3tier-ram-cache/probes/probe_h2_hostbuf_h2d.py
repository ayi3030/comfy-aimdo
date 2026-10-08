#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4-H2 探针：证明「hostbuf(RAM) -> 显存(XPU)」这条腿真的写入正确字节（R6 最硬前提）。

背景：首次 fault 的快路径 read_tensor_file_slice_into 命中 hostbuf 分支后会 `return True`
      并跳过 torch 回退拷贝。**若 C 侧 H2D 在 XPU 上“返回 True 却没真写显存”，结果是静默
      错误、不会被回退纠正。** 本探针用 device_ptr 显式指向一块 XPU 显存，写入后逐字节比对。

复刻调用（等价 memory_management.py:70-74 的 device 侧）：
    hostbuf.read_file_slice(f, 0, n, offset=0, stream=0,
                            device_ptr=dest.data_ptr(), device=dest.device.index)
    torch.xpu.synchronize()
    dest.cpu() 与源文件比对

预期输出：H2D_OK
失败：H2D_FAIL（首字节位置）| H2D_API_FAIL（异常/无 XPU）
用法（B580 机器，python_embeded）：
    python_embeded\\python.exe <此文件> [--size-mib 4]
约束：不启动 ComfyUI；仅用 torch + comfy_aimdo；本文件为“预备工装”，reviewer 不代为运行。
"""
import argparse
import os
import sys
import tempfile
import traceback


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mib", type=int, default=4)
    a = ap.parse_args()

    try:
        import torch
        import comfy_aimdo.host_buffer as hb
    except Exception:
        print("H2D_API_FAIL: import failed")
        traceback.print_exc()
        return 2

    if not (hasattr(torch, "xpu") and torch.xpu.is_available()):
        print("H2D_API_FAIL: torch.xpu not available on this machine (skip H2 on non-XPU).")
        return 3

    n = a.size_mib * 1024 * 1024
    payload = bytes((i * 131 + 17) & 0xFF for i in range(4096)) * (n // 4096)

    fd, path = tempfile.mkstemp(prefix="aimdo_h2d_", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)

        dest = torch.empty(len(payload), dtype=torch.uint8, device="xpu")
        buf = hb.HostBuffer(0, 8 * 1024 * 1024, max(n * 2, 16 * 1024 * 1024))
        buf.extend(len(payload), register=False)

        with open(path, "rb") as fh:
            buf.read_file_slice(fh, 0, len(payload), offset=0, stream=0,
                                device_ptr=dest.data_ptr(), device=dest.device.index)
        torch.xpu.synchronize()

        got = bytes(dest.cpu().tolist())
        if got == payload:
            print(f"H2D_OK (size={len(payload)}B, device={dest.device})")
            return 0
        first = next((i for i, (x, y) in enumerate(zip(got, payload)) if x != y), None)
        print(f"H2D_FAIL: first mismatch at byte {first}")
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
