#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""S4-H1 探针：证明「文件 -> hostbuf(RAM)」这条腿在 XPU 构建下确实写入字节。
**不启动 ComfyUI、不 import comfy.*（零 ComfyUI 依赖），只用 site-packages 的 comfy_aimdo。**

它复刻 memory_management.py:65-75 的 hostbuf 分支语义：
    hostbuf.read_file_slice(file_obj, offset, size, offset=<数据指针相对偏移>,
                            stream=0, device_ptr=0, device=None)   # 纯宿主腿（device_ptr=0）
随后把 hostbuf 原始内存读回，与写入文件逐字节比对。

预期输出：HOSTBUF_FILLED_OK
失败输出：HOSTBUF_FILLED_FAIL  —— 说明“文件->RAM”腿不成立，R6 前提崩，直接阻塞。
（若 hostbuf 分配/extend 就抛异常，则打印 HOSTBUF_API_FAIL 与回溯。）

用法（B580 机器，python_embeded）：
    python_embeded\\python.exe <此文件> [--size-mib 4]

约束：读/写均在系统临时目录，不触碰 ComfyUI 目录；本文件为“预备工装”，由干系人或
      runtime-verifier 真机执行；reviewer 不代为运行。
"""
import argparse
import ctypes
import os
import sys
import tempfile
import traceback

EXPECTED_IMPORT_ERR = None
try:
    import comfy_aimdo.host_buffer as hb   # noqa: E402
except Exception as e:                     # pragma: no cover
    EXPECTED_IMPORT_ERR = e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mib", type=int, default=4)
    a = ap.parse_args()

    if EXPECTED_IMPORT_ERR is not None:
        print(f"HOSTBUF_API_FAIL: cannot import comfy_aimdo.host_buffer: {EXPECTED_IMPORT_ERR!r}")
        return 2

    n = a.size_mib * 1024 * 1024
    payload = bytes((i * 31 + 7) & 0xFF for i in range(4096)) * (n // 4096)

    fd, path = tempfile.mkstemp(prefix="aimdo_probe_", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)

        # 复刻 pinned_memory 的建法：size=0, prewarm=8MiB, max_grow_size 取大值
        buf = hb.HostBuffer(0, 8 * 1024 * 1024, max(n * 2, 16 * 1024 * 1024))
        buf.extend(len(payload), register=False)

        raw = buf.get_raw_address()
        if not raw:
            print("HOSTBUF_API_FAIL: hostbuf raw address is 0")
            return 2

        with open(path, "rb") as fh:
            # 与 memory_management.py:70-74 等价：destination 是 hostbuf 视图, destination2=None
            # => offset = data_ptr - raw（此处数据从 raw+0 开始）, device_ptr = 0, device = None
            buf.read_file_slice(fh, 0, len(payload), offset=0, stream=0,
                                device_ptr=0, device=None)

        view = (ctypes.c_uint8 * buf.size).from_address(raw)
        got = bytes(view[:len(payload)])
        if got == payload:
            print("HOSTBUF_FILLED_OK "
                  f"(size={len(payload)}B, hostbuf.size={buf.size}B)")
            return 0
        first = next((i for i, (x, y) in enumerate(zip(got, payload)) if x != y), None)
        print(f"HOSTBUF_FILLED_FAIL: first mismatch at byte {first} "
              f"(hostbuf.size={buf.size}B, expected {len(payload)}B)")
        return 1
    except Exception:
        print("HOSTBUF_API_FAIL:")
        traceback.print_exc()
        return 2
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
