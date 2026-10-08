"""Intel XPU backend for comfy-aimdo.

Clean, first-class XPU support for comfy-aimdo (target: upstream v0.5.5).
This module owns every XPU-specific concern so ``control.py`` only needs a
small vendor branch:

  * detects whether the AIMDO XPU runtime (``aimdo_xpu.{dll,so}``) and the
    Unified-Runtime USM hook are usable, and **fails closed** when they are
    not -- ComfyUI then transparently keeps PyTorch's native XPU allocator;
  * declares the ctypes contract for the XPU native library;
  * installs the XPU pluggable allocator (Linux ``global`` mode) or arbitrates
    allocation pressure through the UR-USM hook (``native_hook`` mode, the
    Windows default because Windows keeps PyTorch's native caching allocator).

The design deliberately does **not** replicate the community fork's
``canonical_control_overlay`` hack: XPU is a normal vendor like ``cuda`` /
``rocm`` here.

Hardware reality: Intel Level Zero / Unified Runtime has no CUDA-VMM style
page-fault engine, so the true on-demand offload model (the "layer 3" work)
lives in M2. This module is the foundation: vendor wiring + capability gate.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import sys

import torch

from . import control

# XPU pluggable allocator function names exported by aimdo_xpu.{dll,so}
_ALLOC_FN = "xpu_alloc_fn"
_FREE_FN = "xpu_free_fn"

# Functions that must all be present for the native UR-USM hook to arbitrate
# allocation pressure on XPU.
_NATIVE_HOOK_SYMBOLS = (
    "xpu_ur_hook_is_interposed",
    "xpu_ur_hook_enable",
    "xpu_ur_hook_disable",
    "xpu_ur_hook_get_stats",
)

_xpu_allocator_ready = False
_xpu_allocator_mode = None
_torch_xpu_empty_cache_original = None
_torch_xpu_memory_stats_original = None
_torch_xpu_reset_peak_stats_original = None


def is_runtime_available() -> bool:
    """Whether the AIMDO XPU native library can be imported/loaded at all.

    Fail-closed: any problem returns False and ComfyUI keeps stock torch.xpu.
    """
    try:
        import importlib.util

        return importlib.util.find_spec("comfy_aimdo") is not None
    except Exception:
        return False


def _normalize_mode(mode: str | None) -> str:
    # Windows retains PyTorch's native caching allocator; arbitration happens
    # through the UR-USM hook (native_hook). Linux may also use native_hook, or
    # replace the allocator entirely (global). Default to native_hook.
    if mode is None:
        return "native_hook"
    mode = mode.lower()
    if mode not in ("global", "native_hook"):
        raise ValueError(
            f"unsupported XPU allocator mode {mode!r}; expected 'global' or 'native_hook'"
        )
    return mode


def _declare_argtypes(lib) -> None:
    """Declare the XPU-native ctypes contract (subset needed for M1)."""
    lib.xpu_set_queues.argtypes = [
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.c_size_t,
    ]
    lib.xpu_set_queues.restype = ctypes.c_bool

    lib.xpu_get_vmm_stats.argtypes = [
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.c_size_t,
    ]
    lib.xpu_get_vmm_stats.restype = ctypes.c_bool

    lib.xpu_allocator_empty_cache.argtypes = [ctypes.c_bool]
    lib.xpu_allocator_empty_cache.restype = ctypes.c_bool

    lib.xpu_allocator_get_memory_stats.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint64),
        ctypes.c_size_t,
    ]
    lib.xpu_allocator_get_memory_stats.restype = ctypes.c_bool

    lib.xpu_allocator_reset_peak_stats.argtypes = [ctypes.c_int]
    lib.xpu_allocator_reset_peak_stats.restype = None

    # M2: Book A（单一记账源）导出；老 DLL 可能缺失，故仅在有符号时声明。
    if hasattr(lib, "xpu_get_total_vram_usage"):
        lib.xpu_get_total_vram_usage.argtypes = [ctypes.c_int]
        lib.xpu_get_total_vram_usage.restype = ctypes.c_uint64
    if hasattr(lib, "xpu_get_peak_total_vram_usage"):
        lib.xpu_get_peak_total_vram_usage.argtypes = [ctypes.c_int]
        lib.xpu_get_peak_total_vram_usage.restype = ctypes.c_uint64

    if platform.system() == "Windows":
        # Windows-specific small-VBAR copy workaround entry points.
        if hasattr(lib, "aimdo_xpu_is_mapped_pinned_vbar"):
            lib.aimdo_xpu_is_mapped_pinned_vbar.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
            ]
            lib.aimdo_xpu_is_mapped_pinned_vbar.restype = ctypes.c_bool
        if hasattr(lib, "aimdo_xpu_needs_small_vbar_copy_workaround"):
            lib.aimdo_xpu_needs_small_vbar_copy_workaround.argtypes = [ctypes.c_int]
            lib.aimdo_xpu_needs_small_vbar_copy_workaround.restype = ctypes.c_bool
        if hasattr(lib, "aimdo_xpu_copy_host_to_vbar"):
            lib.aimdo_xpu_copy_host_to_vbar.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_int,
            ]
            lib.aimdo_xpu_copy_host_to_vbar.restype = ctypes.c_bool

    if all(hasattr(lib, n) for n in _NATIVE_HOOK_SYMBOLS):
        lib.xpu_ur_hook_is_interposed.argtypes = []
        lib.xpu_ur_hook_is_interposed.restype = ctypes.c_bool
        lib.xpu_ur_hook_enable.argtypes = []
        lib.xpu_ur_hook_enable.restype = ctypes.c_bool
        lib.xpu_ur_hook_disable.argtypes = []
        lib.xpu_ur_hook_disable.restype = ctypes.c_bool
        lib.xpu_ur_hook_get_stats.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.c_size_t,
        ]
        lib.xpu_ur_hook_get_stats.restype = ctypes.c_bool


def _native_hook_capable(lib) -> bool:
    if platform.system() not in ("Linux", "Windows"):
        return False
    if not all(hasattr(lib, n) for n in _NATIVE_HOOK_SYMBOLS):
        return False
    try:
        return bool(lib.xpu_ur_hook_is_interposed())
    except Exception:
        return False


def _install_global_allocator(lib):
    """Linux ``global`` mode: register AIMDO's XPU pluggable allocator with torch."""
    from . import torch as aimdo_torch

    allocator = aimdo_torch.get_torch_allocator()
    if allocator is None:
        raise RuntimeError("AIMDO XPU allocator is unavailable")
    torch.xpu.memory.change_current_allocator(allocator)


def _install_native_hook_wrappers():
    """Override torch.xpu cache/stat accessors so AIMDO's bookkeeping is visible."""
    global _torch_xpu_empty_cache_original, _torch_xpu_memory_stats_original
    global _torch_xpu_reset_peak_stats_original

    _torch_xpu_empty_cache_original = torch.xpu.empty_cache
    _torch_xpu_memory_stats_original = torch.xpu.memory_stats
    _torch_xpu_reset_peak_stats_original = torch.xpu.reset_peak_memory_stats

    def aimdo_xpu_empty_cache():
        try:
            control.lib.xpu_allocator_empty_cache(False)
        except Exception:
            pass
        try:
            return _torch_xpu_empty_cache_original()
        except RuntimeError as error:
            if "does not yet support emptyCache" not in str(error):
                raise
            return None

    def aimdo_xpu_memory_stats(device=None):
        stats = get_xpu_allocator_memory_stats(device)
        if stats is None:
            try:
                return _torch_xpu_memory_stats_original(device)
            except RuntimeError:
                return {}
        active, reserved, peak_active, peak_reserved = stats
        return {
            "active_bytes.all.current": active,
            "active_bytes.all.peak": peak_active,
            "allocated_bytes.all.current": active,
            "allocated_bytes.all.peak": peak_active,
            "reserved_bytes.all.current": reserved,
            "reserved_bytes.all.peak": peak_reserved,
        }

    def aimdo_xpu_reset_peak_memory_stats(device=None):
        reset_xpu_allocator_peak_stats(device)
        try:
            return _torch_xpu_reset_peak_stats_original(device)
        except RuntimeError as error:
            if "does not yet support resetPeakStats" not in str(error):
                raise
            return None

    torch.xpu.empty_cache = aimdo_xpu_empty_cache
    torch.xpu.memory.memory_stats = aimdo_xpu_memory_stats
    torch.xpu.memory_stats = aimdo_xpu_memory_stats
    torch.xpu.memory.reset_peak_memory_stats = aimdo_xpu_reset_peak_memory_stats
    torch.xpu.reset_peak_memory_stats = aimdo_xpu_reset_peak_memory_stats


def _publish_queues(lib) -> bool:
    """Hand PyTorch's SYCL queue pointers to the native backend.

    The native side keeps its device table (g_devices) in xpu_set_queues();
    every CUDA-shaped shim entry point resolves its device through it. Without
    this call the table stays empty and every dispatch fails with
    "CUDA API FAILED (999): Level Zero or SYCL error".

    This was a missing link in the community port: xpu_set_queues had its
    ctypes signature declared in _declare_argtypes() but no caller anywhere in
    the tree, so it was never invoked.

    The queue pointer must come from the same oneAPI major as the DLL's import
    table (dispatch.cpp receives sycl::queue* directly and reads it), which is
    why this can only run inside a live torch process.
    """
    device_ids = []
    queue_pointers = []

    for index in range(torch.xpu.device_count()):
        try:
            stream = torch.xpu.current_stream(index)
            queue = stream.sycl_queue
        except Exception as error:  # pragma: no cover - torch internals
            logging.error(
                f"comfy-aimdo XPU: could not obtain the SYCL queue for device "
                f"{index}: {error}"
            )
            return False
        if not queue:
            logging.error(
                f"comfy-aimdo XPU: torch reported an empty SYCL queue for "
                f"device {index}"
            )
            return False
        device_ids.append(index)
        queue_pointers.append(ctypes.c_uint64(int(queue)))

    if not device_ids:
        logging.error("comfy-aimdo XPU: torch reports no XPU devices")
        return False

    ids_array = (ctypes.c_int * len(device_ids))(*device_ids)
    queues_array = (ctypes.c_uint64 * len(queue_pointers))(*queue_pointers)
    if not lib.xpu_set_queues(ids_array, queues_array, len(device_ids)):
        logging.error(
            "comfy-aimdo XPU: the native backend rejected the SYCL queue "
            "registry (ABI mismatch between torch and aimdo_xpu.dll?)"
        )
        return False

    logging.info(
        f"comfy-aimdo XPU: published {len(device_ids)} SYCL queue(s) to the "
        f"native backend"
    )
    return True


def setup_backend(lib, mode: str | None, system: str, explicitly_requested: bool = False) -> bool:
    """Wire up the XPU backend. Returns False (fail-closed) if unusable.

    Caller (control.init) treats False as "use native torch.xpu instead".
    """
    global _xpu_allocator_ready, _xpu_allocator_mode

    try:
        requested_mode = _normalize_mode(mode)
    except ValueError as error:
        logging.error(f"comfy-aimdo XPU: {error}")
        return False

    # Opt-in: XPU only activates when explicitly requested or dynamic-VRAM is on.
    if not explicitly_requested and not _xpu_opt_in():
        logging.info(
            "comfy-aimdo XPU backend not requested; using native PyTorch XPU allocator"
        )
        return False

    _declare_argtypes(lib)

    # Publish PyTorch's SYCL queues before anything else: the native dispatch
    # table is resolved per call against this registry, so an empty table makes
    # every allocation path fail. Must run before plat_init()/init_devices().
    if not _publish_queues(lib):
        logging.error(
            "comfy-aimdo XPU: falling back to the native PyTorch XPU allocator "
            "because the SYCL queue registry could not be published"
        )
        return False

    # Windows keeps PyTorch's native XPU caching allocator; we arbitrate
    # pressure via the UR-USM hook. The hook must be interposed (the
    # ComfyUI-OmniXPU prestartup patch does this) or we cannot arbitrate.
    if requested_mode == "native_hook":
        if not _native_hook_capable(lib):
            logging.error(
                "comfy-aimdo XPU native hook could not attach to the Unified "
                "Runtime; AIMDO XPU requires the OmniXPU bootstrap to interpose "
                "ur_loader before Python starts. Falling back to native XPU allocator."
            )
            return False
        try:
            lib.xpu_ur_hook_enable()
        except Exception as error:
            logging.error(f"comfy-aimdo XPU failed to enable native hook: {error}")
            return False
    else:
        # global mode (Linux): replace torch's allocator with AIMDO's.
        try:
            _install_global_allocator(lib)
        except Exception as error:
            logging.error(f"comfy-aimdo XPU failed to install allocator: {error}")
            return False

    try:
        _install_native_hook_wrappers()
    except Exception as error:
        logging.warning(f"comfy-aimdo XPU stats wrappers skipped: {error}")

    _xpu_allocator_mode = requested_mode
    _xpu_allocator_ready = True
    logging.info(f"comfy-aimdo XPU backend ready (mode={requested_mode})")
    return True


def _xpu_opt_in() -> bool:
    """Whether XPU dynamic offload should activate.

    Mirrors the community fork's opt-in: explicit env flag, or ComfyUI's
    --enable-dynamic-vram, or an explicit implementation request from the caller.
    """
    if os.environ.get("AIMDO_XPU_ENABLED") == "1":
        return True
    comfy_cli = sys.modules.get("comfy.cli_args")
    if comfy_cli is not None:
        args = getattr(comfy_cli, "args", None)
        if args is not None and getattr(args, "enable_dynamic_vram", False):
            return True
    return False


def get_torch_allocator():
    """Return the XPU pluggable allocator, or None if XPU is not active."""
    if getattr(control, "implementation", None) != "xpu" or control.lib is None:
        return None
    if not hasattr(control.lib, _ALLOC_FN) or not hasattr(control.lib, _FREE_FN):
        logging.error("comfy-aimdo XPU native library lacks allocator entry points")
        return None
    from . import torch as aimdo_torch

    return aimdo_torch.XPUPluggableAllocator()


def get_xpu_allocator_memory_stats(device=None):
    """返回 (active, reserved, peak_active, peak_reserved)。

    M2 单一记账源：优先读 Book A（xpu_get_total_vram_usage，即
    g_devctx->_total_vram_usage，AIMDO 权威物理显存账），彻底避免与
    torch 自记账副本（Book B / g_torch_*_bytes）造成的双记账。XPU 上
    active 与 reserved 的拆分无权威来源，采用保守近似 active == reserved；
    峰值用 AIMDO 维护的 Book A 峰值。

    老 DLL 未导出 Book A 时回落 Book B 并告警（双记账风险，仅兼容）。
    """
    if control.lib is None:
        return None
    dev = torch.xpu.current_device() if device is None else device
    dev = dev if isinstance(dev, int) else dev.index
    if hasattr(control.lib, "xpu_get_total_vram_usage"):
        try:
            active = reserved = int(control.lib.xpu_get_total_vram_usage(int(dev)))
            peak = (
                int(control.lib.xpu_get_peak_total_vram_usage(int(dev)))
                if hasattr(control.lib, "xpu_get_peak_total_vram_usage")
                else active
            )
            return (active, reserved, peak, peak)
        except Exception:
            pass
    # 兼容路径：老 DLL 仅暴露 Book B（双记账风险）
    try:
        out = (ctypes.c_uint64 * 4)()
        ok = control.lib.xpu_allocator_get_memory_stats(int(dev), out, 4)
    except Exception:
        return None
    if not ok:
        return None
    logging.warning(
        "comfy-aimdo XPU: DLL 未导出 Book A (xpu_get_total_vram_usage)，"
        "显存统计回落 Book B，存在双记账风险（请升级 aimdo_xpu.dll）"
    )
    return int(out[0]), int(out[1]), int(out[2]), int(out[3])


def reset_xpu_allocator_peak_stats(device=None):
    if control.lib is None:
        return
    dev = torch.xpu.current_device() if device is None else device
    dev = dev if isinstance(dev, int) else dev.index
    try:
        control.lib.xpu_allocator_reset_peak_stats(int(dev))
    except Exception:
        pass


def get_xpu_vmm_stats():
    if control.lib is None:
        return None
    out = (ctypes.c_uint64 * 16)()
    try:
        ok = control.lib.xpu_get_vmm_stats(out, 16)
    except Exception:
        return None
    return out if ok else None


def get_allocator_mode():
    return _xpu_allocator_mode


def is_ready():
    return _xpu_allocator_ready
