#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""H1 探针（自写，替代不可用的 probe_h1_hostbuf_fill.py）。

证什么：证明「文件 -> hostbuf(RAM)」这条腿在 XPU 构建下确实写入正确字节。

为什么重写：原 probe_h1_hostbuf_fill.py 直接 `import comfy_aimdo.host_buffer`，
而 host_buffer.py:6 在**导入期**执行 `lib = control.lib`；此时 control.init() 从未被调用，
lib 恒为 None -> `if lib is not None:` 的 argtypes 绑定被跳过 -> HostBuffer() 抛
AttributeError('NoneType' has no attribute 'hostbuf_allocate')。实测 100% 打印
HOSTBUF_API_FAIL，永远拿不到真值。本脚本先 control.init() 再导入 host_buffer，
并显式校验 hb.lib 非空，杜绝“探针坏了当成功能坏了”。

判据：hostbuf 内容 == 写入文件内容（逐字节）。
  通过 -> H1_OK          退出 0
  不一致 -> H1_FAIL      退出 1
  环境/API 不可用 -> H1_API_FAIL 退出 2（**不静默通过**）

约束：不启动 ComfyUI、不 import comfy.*；只在系统临时目录读写；不触碰被测 4 文件。
"""
import argparse
import ctypes
import os
import sys
import tempfile
import traceback


def _init_backend():
    import comfy_aimdo.control as control
    ok = control.init("xpu")
    # 即便 setup_backend 失败（例如裸进程没有 UR loader 插桩），DLL 通常已载入，
    # hostbuf_* 是独立于 allocator 钩子的导出，仍可用；是否真可用由 hb.lib 判定。
    if not ok:
        print(f"H1_NOTE: control.init('xpu') returned {ok} "
              f"(allocator backend not activated; attempting hostbuf anyway)")
    # host_buffer 必须在 control.init 之后再导入：其 argtypes 只在 import 期且
    # lib 非空时绑定。先清缓存强制重导入，确保绑定生效。
    sys.modules.pop("comfy_aimdo.host_buffer", None)
    from comfy_aimdo import host_buffer as hb
    if hb.lib is None:
        hb.lib = control.lib
    # hostbuf_* 依赖平台/设备初始化：ComfyUI 在 main.py:285 调 control.init_devices()。
    # 少了它，hostbuf_read_file_slice 会 access violation（真机实测）。
    if control.lib is not None:
        dev_ok = control.init_devices([0])
        print(f"H1_ENV: init_devices([0]) -> {dev_ok}")
    return control, hb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-mib", type=int, default=4)
    a = ap.parse_args()

    try:
        control, hb = _init_backend()
    except Exception:
        print("H1_API_FAIL: cannot init/import comfy_aimdo host_buffer")
        traceback.print_exc()
        return 2
    if hb.lib is None:
        print("H1_API_FAIL: host_buffer.lib is None after control.init "
              "(native backend not loaded)")
        return 2
    print(f"H1_ENV: control.lib={'set' if control.lib is not None else 'None'} "
          f"host_buffer.lib={'set' if hb.lib is not None else 'None'}")

    n = a.size_mib * 1024 * 1024
    # 用 os.urandom：非周期，任何字节级错误/错位都会暴露（固定 4096 周期会被错位掩盖）。
    payload = os.urandom(n)
    import hashlib
    print(f"H1_PAYLOAD: {n}B sha256={hashlib.sha256(payload).hexdigest()}")

    fd, path = tempfile.mkstemp(prefix="yan_h1_", suffix=".bin")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)

        # 复刻 pinned_memory 的建法与 memory_management.py:70-74 的纯宿主腿调用
        # （device_ptr=0 => 阻塞式 host-only；device=None => -1）。
        buf = hb.HostBuffer(0, 8 * 1024 * 1024, max(n * 2, 16 * 1024 * 1024))
        buf.extend(n, register=False)

        raw = buf.get_raw_address()
        if not raw:
            print("H1_API_FAIL: hostbuf raw address is 0")
            return 2

        with open(path, "rb") as fh:
            buf.read_file_slice(fh, 0, n, offset=0, stream=0,
                                device_ptr=0, device=None)

        if buf.size < n:
            print(f"H1_API_FAIL: hostbuf.size={buf.size} < expected {n}")
            return 2

        view = (ctypes.c_uint8 * buf.size).from_address(raw)
        got = bytes(view[:n])
        if got == payload:
            print(f"H1_OK (size={n}B, hostbuf.size={buf.size}B) "
                  "-> file->hostbuf(RAM) writes exact bytes")
            return 0
        first = next((i for i, (x, y) in enumerate(zip(got, payload)) if x != y), None)
        ndiff = sum(1 for x, y in zip(got, payload) if x != y)
        print(f"H1_FAIL: first mismatch at byte {first}, differing_bytes={ndiff} "
              f"(hostbuf.size={buf.size}B, expected {n}B)")
        return 1
    except Exception:
        print("H1_API_FAIL:")
        traceback.print_exc()
        return 2
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
