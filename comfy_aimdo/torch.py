import torch
import ctypes

import logging
from pathlib import Path

from . import control

def get_tensor_from_raw_ptr(ptr, size, device):
    container = {
        "shape": (size,),
        "typestr": "|u1",
        "data": (ptr, False), #writable
        "version": 3,
    }

    class Holder:
        pass

    # 防御（审查项 R-g）：__cuda_array_interface__ 是 CUDA/HIP 专有协议，XPU 张量由
    # XPUPluggableAllocator 直接产出、不应经此函数构造。若在此对非 CUDA 设备继续使用该协议，
    # 会得到难以定位的错误结果。故仅在确认设备为 cuda/HIP（torch 中 HIP 也报 "cuda"）或未指定时放行，
    # 其余（如 "xpu"）显式报错，避免误用。此守卫对既有 CUDA/HIP 调用路径为恒等放行、无行为变化。
    _dev_type = getattr(device, "type", None)
    if _dev_type is None and isinstance(device, str):
        _dev_type = device.split(":")[0]
    if _dev_type not in (None, "cuda"):
        raise NotImplementedError(
            "comfy-aimdo: get_tensor_from_raw_ptr only supports CUDA/HIP tensors "
            f"(got device={device!r}). XPU tensors are produced directly by the "
            "XPUPluggableAllocator; do not route them through the CUDA array interface."
        )

    holder = Holder()
    # 注意：这里是 CUDA 专用的 __cuda_array_interface__ 包装；XPU 后端不走此路径。
    holder.__cuda_array_interface__ = container

    return torch.as_tensor(holder, device=device)

def aimdo_to_tensor(alloc, device):
    _, ptr, size = alloc
    return get_tensor_from_raw_ptr(ptr, size, device)

def hostbuf_to_tensor(hostbuf):
    byte_view = (ctypes.c_uint8 * hostbuf.size).from_address(hostbuf.get_raw_address())
    return torch.frombuffer(byte_view, dtype=torch.uint8)

#pytorch doesnt have an API for a CUDAPluggableAllocator from an already loaded
#library. Rather than force a second load that pytorch owns, construct these
#pytorch internals outselves as sperate CDLL loads is far too risky.

# CUDA/HIP 路径：仅在 torch 提供了 CUDA 可插拔分配器基类时定义（XPU-only 环境下不定义，避免导入崩溃）。
try:
    _CudaPluggableAllocatorBase = torch.cuda.memory.CUDAPluggableAllocator
except Exception:
    _CudaPluggableAllocatorBase = None

if _CudaPluggableAllocatorBase is not None:
    class CUDAPluggableAllocator(_CudaPluggableAllocatorBase):
        def __init__(self):
            alloc_fn = ctypes.cast(getattr(control.lib, "alloc_fn"), ctypes.c_void_p).value
            free_fn = ctypes.cast(getattr(control.lib, "free_fn"), ctypes.c_void_p).value
            assert alloc_fn is not None
            assert free_fn is not None
            self._allocator = torch._C._cuda_customAllocator(alloc_fn, free_fn)


def _xpu_extension_path():
    """返回本扩展 XPU 动态库的绝对路径（PyTorch XPUPluggableAllocator 需要路径+符号名）。"""
    import platform
    ext = "dll" if platform.system() == "Windows" else "so"
    return str(Path(__file__).parent.resolve() / f"aimdo_xpu.{ext}")


def _get_xpu_allocator():
    """XPU 后端：用 PyTorch XPUPluggableAllocator 注册本扩展导出的 alloc_fn/free_fn。

    API（已联网查证）：torch.xpu.memory.XPUPluggableAllocator(so_path, "alloc_fn", "free_fn")
                       torch.xpu.memory.change_current_allocator(allocator)
    必须在首次 XPU 分配前注册。
    """
    if control.lib is None:
        return None
    try:
        from torch.xpu import memory as xpu_memory
    except Exception as e:
        logging.warning(f"comfy-aimdo: torch.xpu.memory unavailable ({e})")
        return None
    try:
        allocator = xpu_memory.XPUPluggableAllocator(_xpu_extension_path(), "alloc_fn", "free_fn")
        xpu_memory.change_current_allocator(allocator)
        return allocator
    except Exception as e:
        logging.warning(f"comfy-aimdo: failed to register XPUPluggableAllocator ({e})")
        return None


def get_torch_allocator():
    #As of this writing (pytorch 2.10), pytorch MemPools + CUDAPluggableAllocator
    #considers the Mempool and pool usage context each as a hard reference to the
    #tensors completely preventing reasonable garbage collection. A read of the code
    #suggests that the assumptions of cudaGraphs completely prohibits pool cleanup
    #on VRAM pressure which ultimately makes this un-usable for our high pressure
    #allocator.
    if control.lib is None:
        return None
    if control.detect_vendor() == "xpu":
        return _get_xpu_allocator()
    logging.warning(f"WARNING: Aimdo+CUDAPluggableAllocator is experimental and unsupported.")
    if _CudaPluggableAllocatorBase is None:
        return None
    return CUDAPluggableAllocator()
