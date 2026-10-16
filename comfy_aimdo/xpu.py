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
import re
import sys

import torch

from . import control
from . import provider as _provider

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
_wrappers_installed = False
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
    global _torch_xpu_reset_peak_stats_original, _wrappers_installed

    # Idempotence guard (teardown-review F-05). Re-capturing the originals on a
    # second call would store our own wrapper as the "original", so the restore
    # in teardown_backend() would put the wrapper back instead of torch's.
    if _wrappers_installed:
        return

    _torch_xpu_empty_cache_original = torch.xpu.empty_cache
    _torch_xpu_memory_stats_original = torch.xpu.memory_stats
    _torch_xpu_reset_peak_stats_original = torch.xpu.reset_peak_memory_stats

    # 注意：下面三个闭包必须定义在本函数内、且位于 torch.xpu.* 赋值之前。
    # torch_reserved_stats() 是本模块顶层的独立函数（定义在本函数之后），
    # 它刻意绕过下面安装的包装器去读 torch 原生口径。不要把它挪进来，
    # 也不要把下面三段缩进到 torch_reserved_stats 的 return 之后——那会让
    # 整个包装器安装变成不可达的死代码（曾真实发生过，见 xpu.py 历史）。
    def aimdo_xpu_empty_cache():
        try:
            control.lib.xpu_allocator_empty_cache(False)
        except Exception:
            pass
        try:
            _feed_external_vram_usage()
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
        # P2: refresh the external VRAM ledger on every memory-stats query.
        # Sampling steps and VBAR faults query stats frequently, so this keeps
        # Book A from going stale by the ~940 MB window that previously only
        # closed on empty_cache() calls. Same try/except style as the
        # empty_cache wrapper above.
        try:
            _feed_external_vram_usage()
        except Exception:
            pass
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
    _wrappers_installed = True
    # Prime the native admission gate with torch's real device reservation so
    # Book A is not undercounted from the first allocation.
    try:
        _feed_external_vram_usage()
    except Exception:
        pass


def torch_reserved_stats(device=None):
    """读取 torch **原生**预留统计，绕过本模块安装的 M2 包装器。

    为什么必须绕过：M2 的 aimdo_xpu_memory_stats() 为了让 Book A 成为唯一
    权威账，把 reserved 与 allocated 双双映射成 Book A 的当前值
    （见 aimdo_xpu_memory_stats 内 "active = reserved = ..."）。于是任何经由
    torch.xpu.memory_stats() 计算的「缓存量」恒为 reserved - allocated == 0：

        L3 trim 永不触发、anticipated_growth 恒为 0。

    实测（Arc B580 / torch 2.14.0+xpu，Book A=6GB、torch 真实 8GB/4GB）：
        经包装器: reserved=6GB allocated=6GB -> cached=0GB  （L3 判据恒假）
        绕过后  : reserved=8GB allocated=4GB -> cached=4GB  （L3 正常触发）

    所以本函数只捕获并调用「包装前」的原始函数。任何改动
    _install_native_hook_wrappers 捕获时机或 _wrappers_installed 守卫的代码，
    都可能让本函数静默变成 None，进而让 L3 停工 —— 故 model_vbar 的
    _release_native_cache 在 None 时会打一条限流告警。

    ⚠️ 耦合约束：本模块顶层的 M2 包装器（_install_native_hook_wrappers）既是
    「双记账修复」，也是「torch 真实字节」的唯一遮蔽源。改它之前先确认
    torch_reserved_stats 的捕获仍然有效；否则 L3 会静默失效，且实机症状只是
    「采样变慢」，不会有任何报错。

    返回 (reserved, allocated, peak_reserved)；不可用时返回 None。

    另见 torch_reserved_stats_reason()：需要区分失败原因时用它（排查方向
    完全不同 ——「包装器未捕获」指向 M2 未装上，「调用抛异常」指向 torch 侧）。
    """
    return torch_reserved_stats_reason(device)[0]


def torch_reserved_stats_reason(device=None):
    """同 torch_reserved_stats，但额外返回失败原因，供告警定位。

    返回 (stats, reason)；成功时 reason 为 None。失败原因互斥且排查方向不同：
        "not_installed" —— M2 包装器没装上（_install_native_hook_wrappers 未跑）
        "call_failed"   —— torch 侧memory_stats 抛异常（驱动/XPU 上下文问题）
        "empty_stats"   —— 返回了空/假字典，torch 侧没给出可用读数
    之所以要区分：这三者若都报同一句 WARNING，真机排查会被引向错误的
    方向（最常见的是把「torch 侧异常」误当成「M2 没装」）。
    """
    fn = _torch_xpu_memory_stats_original
    if fn is None:
        return None, "not_installed"
    dev = torch.xpu.current_device() if device is None else device
    dev = dev if isinstance(dev, int) else getattr(dev, "index", dev)
    try:
        stats = fn(dev)
    except Exception:
        return None, "call_failed"
    if not stats:
        return None, "empty_stats"
    reserved = int(stats.get("reserved_bytes.all.current", 0))
    allocated = int(stats.get("allocated_bytes.all.current", 0))
    peak = int(stats.get("reserved_bytes.all.peak", 0))
    return (reserved, allocated, peak), None


# Feed torch's real device reservation into the native admission gate so Book A
# (total_vram_usage) is not undercounted on XPU (the M2 wrapper masks the true
# reserved/allocated -- see torch_reserved_stats). The native setter only exists
# in aimdo_xpu.dll builds that include the RAM-layer root-cause fix
# (RAM_LAYER_CRASH_ROOTCAUSE.md sec.12-13); probe once and skip cleanly on older
# DLLs so a version mismatch never crashes the runtime.
_xpu_external_usage_supported = None
_external_usage_feed_warned = set()


def _warn_external_usage_feed_once(key, detail):
    """Rate-limited warning for the external-VRAM feed.

    This call site used to be a bare `except Exception: pass`. That is exactly
    how a ctypes ABI mismatch stayed invisible for a whole release: with no
    `argtypes` declared, ctypes passes a Python int as the platform default
    C int (32-bit), so any reservation >= 4 GiB raised
    ``ArgumentError: int too long to convert`` and was swallowed -- i.e. the
    feed died silently precisely when VRAM pressure made it matter.
    Never swallow this again: warn once per distinct failure.
    """
    if key in _external_usage_feed_warned:
        return
    _external_usage_feed_warned.add(key)
    try:
        logging.getLogger("comfy_aimdo.xpu").warning(
            "aimdo: external VRAM usage feed %s (%s); the native admission gate "
            "will undercount torch's device reservation", key, detail)
    except Exception:
        pass


def _feed_external_vram_usage():
    """Push torch's true device reservation (bytes) into the native gate.

    ABI contract (src/control.c:218):
        ``void aimdo_set_external_vram_usage(uint64_t usage)``
    The parameter is **bytes**, not MiB. ctypes cannot infer that: with no
    ``argtypes`` a Python int is marshalled as a 32-bit C int, so byte counts
    above 4 GiB raise and are lost. Declare the contract explicitly, at the
    only call site, before every call.
    """
    global _xpu_external_usage_supported
    if _xpu_external_usage_supported is None:
        _xpu_external_usage_supported = hasattr(control.lib, "aimdo_set_external_vram_usage")
    if not _xpu_external_usage_supported:
        return
    stats = torch_reserved_stats()
    if stats is None:
        _warn_external_usage_feed_once("unavailable",
                                       "torch_reserved_stats() returned None")
        return
    try:
        setter = control.lib.aimdo_set_external_vram_usage
        if not getattr(setter, "argtypes", None):
            setter.argtypes = [ctypes.c_uint64]
            setter.restype = None
        value = int(stats[0])
        setter(value)
    except Exception as exc:
        _warn_external_usage_feed_once(
            "failed", "%s: %s (value=%s bytes)"
            % (type(exc).__name__, exc, stats[0]))


def teardown_backend() -> None:
    """Undo everything setup_backend() changed (teardown-review F-03).

    Without this, control.deinit() released the native library while the AIMDO
    wrappers stayed bound to torch.xpu -- verified on Arc B580: after deinit()
    returned successfully, torch.xpu.empty_cache still pointed at
    comfy_aimdo.xpu and _xpu_allocator_ready was still True. Any later XPU
    allocation therefore went through a wrapper whose `control.lib` was None,
    and its bookkeeping was silently dropped.

    Safe to call when nothing was installed, and safe to call twice.
    """
    global _torch_xpu_empty_cache_original, _torch_xpu_memory_stats_original
    global _torch_xpu_reset_peak_stats_original
    global _xpu_allocator_ready, _xpu_allocator_mode, _wrappers_installed

    if _wrappers_installed:
        # Only rebind attributes we actually replaced; a caller may have
        # installed their own wrapper after us.
        if _torch_xpu_empty_cache_original is not None:
            torch.xpu.empty_cache = _torch_xpu_empty_cache_original
        if _torch_xpu_memory_stats_original is not None:
            torch.xpu.memory.memory_stats = _torch_xpu_memory_stats_original
            torch.xpu.memory_stats = _torch_xpu_memory_stats_original
        if _torch_xpu_reset_peak_stats_original is not None:
            torch.xpu.memory.reset_peak_memory_stats = (
                _torch_xpu_reset_peak_stats_original)
            torch.xpu.reset_peak_memory_stats = (
                _torch_xpu_reset_peak_stats_original)

        _torch_xpu_empty_cache_original = None
        _torch_xpu_memory_stats_original = None
        _torch_xpu_reset_peak_stats_original = None
        _wrappers_installed = False
        logging.info("comfy-aimdo XPU: restored the torch.xpu entry points")

    # In "global" mode torch's allocator was replaced wholesale; hand it back.
    if _xpu_allocator_mode == "global":
        try:
            from . import torch as _torch_backend
            restore = getattr(_torch_backend, "restore_torch_allocator", None)
            if restore is not None:
                restore()
        except Exception as _error:
            logging.info(f"comfy-aimdo XPU: allocator restore skipped: {_error!r}")

    _xpu_allocator_ready = False
    _xpu_allocator_mode = None


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


def detect_xpu_target() -> str | None:
    """Resolve the actual Intel XPU silicon family at runtime and validate it
    against the pinned ``xpu_targets`` in provider.json.

    Fail-soft: any error (no torch.xpu, no device, unreadable contract) is
    logged and ``None`` returned. Never raises, never alters the allocation
    path.
    """
    try:
        if not (hasattr(torch, "xpu") and torch.xpu.is_available()
                and torch.xpu.device_count() > 0):
            logging.info("comfy-aimdo XPU: no XPU device available; "
                         "silicon target not resolved")
            return None
        name = torch.xpu.get_device_properties(0).name
        family = _provider.map_xpu_target_name(name)
        pinned = _provider.get_xpu_targets()
        if family is None:
            logging.warning(
                f"comfy-aimdo XPU: unrecognized device name {name!r}; "
                f"cannot map to a pinned target (pinned={pinned})")
        elif pinned and family not in pinned:
            logging.warning(
                f"comfy-aimdo XPU: device {name!r} resolved to family "
                f"{family!r} which is NOT in pinned xpu_targets {pinned}; "
                "possible deployment/contract mismatch -- verify the wheel")
        else:
            logging.info(
                f"comfy-aimdo XPU: device {name!r} resolved to family "
                f"{family!r} (pinned={pinned})")
        return family
    except Exception as exc:  # noqa: BLE001 - fail-soft
        logging.info(f"comfy-aimdo XPU: target detection skipped: {exc!r}")
        return None


def setup_backend(lib, mode: str | None, system: str, explicitly_requested: bool = False) -> bool:
    """Wire up the XPU backend. Returns False (fail-closed) if unusable.

    Caller (control.init) treats False as "use native torch.xpu instead".
    """
    global _xpu_allocator_ready, _xpu_allocator_mode

    # Idempotence guard (teardown-review F-05): a second setup_backend() call
    # would wrap our own wrapper and re-run the hook enable, so short-circuit
    # when the requested mode is already live.
    if _xpu_allocator_ready and _xpu_allocator_mode == _normalize_mode(mode):
        logging.info(
            f"comfy-aimdo XPU backend already ready (mode={_xpu_allocator_mode})")
        return True

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

    # Runtime XPU silicon target resolution (makes provider.json xpu_targets
    # real). Fail-soft: logs + stores; never affects the return value.
    detect_xpu_target()

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

    Activation sources, in order:

    1. ``AIMDO_XPU_ENABLED`` env override -- ``"1"`` forces on, ``"0"`` forces off.
       The force-off hatch matters because source (2) makes XPU default-on, so a
       user needs a way to keep native ``torch.xpu`` for AIMDO only without also
       turning DynamicVRAM off for the rest of ComfyUI.
    2. ComfyUI's own gate: ``comfy.cli_args.enables_dynamic_vram()`` **and**
       ``comfy.model_management.is_intel_xpu()``.

    Source (2) is the task-#17 "same source" alignment. ComfyUI decides DynamicVRAM
    support *by vendor* -- ``main.py`` runs ``enables_dynamic_vram() and
    dynamic_vram_supported()``, and ``dynamic_vram_supported()`` returns True for
    Intel XPU. A process that reaches this backend has therefore already been
    approved by ComfyUI's own gate, so it must not be refused by a second, stricter
    gate here. Reading the same predicate makes XPU default-on and opt-*out* the
    usual way (--disable-dynamic-vram / --highvram / --gpu-only / --novram / --cpu),
    i.e. exactly like NVIDIA; both gates read the same args and can no longer
    disagree. The extra ``is_intel_xpu()`` confirmation rejects the rare
    "torch version says xpu but no usable device" case before we start wiring
    queues.

    Degradation is deliberate and conservative:
      * no ``comfy.cli_args`` (plain library / unit-test) -> False unless env set;
      * no ``enables_dynamic_vram`` (older ComfyUI) -> fall back to the
        --enable-dynamic-vram flag only;
      * predicate raises (args not ready) -> warn, then fall back;
      * ``is_intel_xpu`` unavailable or raising -> do NOT block (the backend's own
        fail-closed gate still guards the real wiring).

    Callers that pass ``implementation="xpu"`` bypass this gate entirely via
    ``explicitly_requested`` in ``setup_backend``.
    """
    override = os.environ.get("AIMDO_XPU_ENABLED")
    if override == "1":
        return True
    if override == "0":
        # 显式关闭：即便 ComfyUI 侧判定支持，也不在 XPU 上启用 AIMDO。
        return False

    comfy_cli = sys.modules.get("comfy.cli_args")
    if comfy_cli is None:
        # 纯库 / 单测场景：保留旧的保守默认（仅 env 可开启）。
        return False

    # (1) ComfyUI 的 DynamicVRAM 开关（已编码「默认启用 + 各类 --no* 退出开关」）。
    enabled = None
    enables = getattr(comfy_cli, "enables_dynamic_vram", None)
    if callable(enables):
        try:
            enabled = bool(enables())
        except Exception as error:  # args 未就绪等异常
            logging.warning(
                f"comfy-aimdo XPU: enables_dynamic_vram() failed ({error}); "
                f"falling back to the --enable-dynamic-vram flag"
            )
    if enabled is None:
        # 兜底：旧版 ComfyUI 无 enables_dynamic_vram()，退化为显式 flag 判据。
        args = getattr(comfy_cli, "args", None)
        enabled = bool(args is not None and getattr(args, "enable_dynamic_vram", False))
    if not enabled:
        return False

    # (2) Intel XPU 硬件确认：仅当 ComfyUI 已加载 model_management 且其判据可用时校验；
    #     不可用 / 异常时不阻断（交由 setup_backend 的 fail-closed 兜底）。
    #
    #     IMPORTANT: main.py 在 ~line 74-84 就调用 comfy_aimdo.control.init()（-> 本函数），
    #     但 comfy.model_management 直到 ~line 258 才 import。真实启动调用点上它并不在
    #     sys.modules；此处若「要求它存在」会让门禁恒为 False（即出货版 728d6bb 的 bug）。
    #     故采用「不可用即不阻断」：可用时保留 Intel 确认，不可用时不因时序竞态而静默退出。
    mm = sys.modules.get("comfy.model_management")
    is_intel = getattr(mm, "is_intel_xpu", None) if mm is not None else None
    if callable(is_intel):
        try:
            return bool(is_intel())
        except Exception as error:
            logging.warning(f"comfy-aimdo XPU: is_intel_xpu() failed ({error}); ignoring")
    return True


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


# ---------------------------------------------------------------------------
# XPU allocator cap: override OmniXPU's 0.99 with a DXGI real-free fraction.
#
# Root cause (verified on user's Arc B580): ComfyUI-OmniXPU's prestartup calls
# ``torch.xpu.set_per_process_memory_fraction(0.99)`` (Windows default), leaving
# almost no driver-side headroom, so a heavy load drives the *driver* into an
# unrecoverable DEVICE_LOST instead of a clean, recoverable PyTorch OOM.
#
# Timing (the trap that sank the abandoned xpu-allocator-cap branch 06cd2d5):
# a plain ``set_per_process_memory_fraction`` placed inside setup_backend() runs
# at ComfyUI main.py:74-84 (log ~line 12) and is CLOBBERED by OmniXPU's 0.99 at
# custom-node load (log ~line 25). OmniXPU reads the OMNIXPU_XPU_MEMORY_FRACTION
# env var FIRST and only falls back to 0.99 when it is unset -- so we set that
# env var HERE, early in control.init() (before its own prestartup), and OmniXPU
# applies OUR value at its own prestartup. We also call the direct setter as a
# belt-and-suspenders measure (covers the OmniXPU-absent case and reinforces).
#
# The fraction is DERIVED from the real OS-visible free VRAM (DXGI
# QueryVideoMemoryInfo, LOCAL segment), NOT a hardcoded constant and NOT
# torch.xpu.mem_get_info (which is capacity-derived -- see probe_dxgi_free.py).
# ---------------------------------------------------------------------------

# Env var OmniXPU reads at its prestartup (must match upstream exactly).
_XPU_ALLOCATOR_CAP_ENV = "OMNIXPU_XPU_MEMORY_FRACTION"
# Headroom left for the driver / system pool so allocation failures become clean
# OOM, not DEVICE_LOST. Set to 1.0 GiB (~9% of the B580's 11.084 GiB DXGI budget),
# lifting the per-process cap to allowed ~10.08 GiB. Intentionally permissive: the
# real cold-start OOM fix is the DXGI budget seed (Lever A / coldstart-fit-fix
# branch), which makes ComfyUI's dynamic-VRAM offloader aggressive during cold start
# so the model's peak residency stays LOW -- the cap stops being the binding
# constraint. xiangyuT runs an even smaller 512 MiB pad, but it is backed by VBAR
# budget-deficit eviction our stack lacks, so 1.0 GiB is the conservative floor
# pending Lever A validation on real hardware.
_XPU_ALLOCATOR_CAP_HEADROOM_GIB = 1.0
_XPU_ALLOCATOR_CAP_FLOOR = 0.30   # safety clamp only; real value is ~0.87
_XPU_ALLOCATOR_CAP_CEIL = 0.90    # never exceed 0.90 (vs OmniXPU's 0.99)
_xpu_allocator_cap_fraction = None  # cache for diagnostics / read-back


class _CapGUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]

    def __init__(self, d1, d2, d3, *b):
        super().__init__(d1, d2, d3, (b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7]))


_IID_IDXGIFactory1 = _CapGUID(
    0x7b7166ec, 0x21c7, 0x44ae, 0xb2, 0x1a, 0xc9, 0xae, 0x32, 0x1a, 0xe3, 0x69)
_IID_IDXGIAdapter3 = _CapGUID(
    0x645967a4, 0x1392, 0x4310, 0xa7, 0x98, 0x80, 0x53, 0xce, 0x3e, 0x93, 0xfd)


class _DXGI_QUERY_VIDEO_MEMORY_INFO(ctypes.Structure):
    _fields_ = [
        ("Budget", ctypes.c_uint64),
        ("CurrentUsage", ctypes.c_uint64),
        ("AvailableForReservation", ctypes.c_uint64),
        ("CurrentReservation", ctypes.c_uint64),
    ]


class _CapLUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_uint32), ("HighPart", ctypes.c_int32)]


class _DXGI_ADAPTER_DESC(ctypes.Structure):
    """DXGI_ADAPTER_DESC (dxgi.h) -- the identity fields we match on."""

    _fields_ = [
        ("Description", ctypes.c_wchar * 128),
        ("VendorId", ctypes.c_uint),
        ("DeviceId", ctypes.c_uint),
        ("SubSysId", ctypes.c_uint),
        ("Revision", ctypes.c_uint),
        ("DedicatedVideoMemory", ctypes.c_size_t),
        ("DedicatedSystemMemory", ctypes.c_size_t),
        ("SharedSystemMemory", ctypes.c_size_t),
        ("AdapterLuid", _CapLUID),
    ]


# Upper bound on EnumAdapters ordinals; DXGI_ERROR_NOT_FOUND ends the walk.
_DXGI_ADAPTER_ENUM_LIMIT = 8
# IDXGIAdapter::GetDesc ordinal. Verified on Arc B580: it returns the real
# adapter descriptions, which pins the chain IUnknown 0-2 / IDXGIObject 3-6 /
# IDXGIAdapter 7-9 / IDXGIAdapter1 10 / IDXGIAdapter2 11 ...
_DXGI_GETDESC_VTABLE_ORDINAL = 8
# ...so IDXGIAdapter3 starts at 12 (RegisterHardwareContentProtection...)
# and QueryVideoMemoryInfo is its third method => 14. Measured with a preset
# sentinel: ordinal 13 returns hr=0 and DOES NOT TOUCH the struct at all -- the
# sentinel survives untouched (ordinal 13 is
# UnregisterHardwareContentProtectionTeardownStatusEvent). It only *looks* like
# "Budget = 0" because a freshly built ctypes Structure is zero-initialised, so
# the budget<=0 defence below clears the struct explicitly instead of relying
# on that hidden premise. Ordinal 14 fills in 11.084 GiB / 15.223 GiB, matching
# the differential probe.
_DXGI_QVMI_VTABLE_ORDINAL = 14

# Canonical uuid layout (8-4-4-4-12 hex). Anything else -- notably a future
# torch that renders the type as "<torch._C._XPUuuid object at 0x...>" -- is
# rejected explicitly rather than being swallowed as "vendor unknown".
_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\Z")

# Vendor/marketing tokens stripped before comparing adapter names.
_DXGI_NAME_NOISE_TOKENS = (
    "intel", "microsoft", "nvidia", "amd", "graphics", "gpu", "controller",
    "adapter", "corporation", "corp", "inc", "series", "family", "r", "tm", "c",
)


def _com_release(ptr):
    """Drop one COM reference (IUnknown::Release, vtable ordinal 2).

    Every pointer this module acquires -- the factory, each enumerated
    IDXGIAdapter and each IDXGIAdapter3 from QueryInterface -- must be
    released on every exit path. control.init()/deinit() cycling re-runs the
    probe, so an unbalanced acquire accumulates per cycle.
    """
    try:
        if ptr is None or not ptr.value:
            return
    except Exception:
        return
    try:
        vt = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
        ctypes.cast(
            vt.contents[2],
            ctypes.CFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p))(ptr)
    except Exception:
        pass


def _normalize_adapter_name(name):
    """Vendor-noise-free adapter name ("Intel(R) Arc(TM) B580 Graphics" ->
    "arc b580") so a DXGI description can be compared with a torch name."""
    words = []
    current = []
    for ch in str(name or "").lower():
        if ch.isalnum():
            current.append(ch)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return " ".join(w for w in words if w not in _DXGI_NAME_NOISE_TOKENS)


def _adapter_names_match(want, cand):
    """Same device by name? Exact equality first.

    Substring containment is only trusted when BOTH sides carry at least two
    tokens: a one-token name like "arc" (from "Intel(R) Arc(TM) Graphics")
    would otherwise be contained in "arc b580" and match the wrong adapter.
    """
    if not want or not cand:
        return False
    if want == cand:
        return True
    if len(want.split()) >= 2 and len(cand.split()) >= 2:
        return want in cand or cand in want
    return False


def _xpu_device_identity():
    """Hardware identity of the current torch XPU device, for DXGI matching.

    Returns ``{"vendor_id": int, "device_id": int, "name": str}``; unknown
    fields stay 0 / "".

    torch exposes a Level-Zero ``uuid``, NOT a Windows DXGI ``AdapterLuid``
    (measured on B580: uuid ``86800be2-0000-0000-0800-...`` vs AdapterLuid
    ``0x00009b9d`` -- unrelated), so a true LUID comparison is impossible
    from Python here. What both APIs DO report identically is the PCI
    vendor/device pair -- torch as ``device_id``, DXGI as
    DXGI_ADAPTER_DESC.VendorId/DeviceId -- so that is the identity match.
    """
    ident = {"vendor_id": 0, "device_id": 0, "name": ""}
    try:
        import torch
        xpu = getattr(torch, "xpu", None)
        if xpu is None or not xpu.is_available():
            return ident
        idx = xpu.current_device()
        props = xpu.get_device_properties(idx)
        ident["device_id"] = int(getattr(props, "device_id", 0) or 0)
        ident["name"] = str(getattr(props, "name", "") or "")
        if not ident["name"]:
            ident["name"] = str(xpu.get_device_name(idx) or "")
        # Level-Zero uuid starts with the PCI vendor id + device id (LE). The
        # string is taken through str() because torch's _XPUuuid is a C++ type,
        # so its rendering is validated before use rather than trusted -- a
        # rejected format is logged instead of silently degrading to a
        # device-id-only match.
        text = str(getattr(props, "uuid", "") or "")
        if _UUID_RE.match(text):
            raw = bytes.fromhex(text.replace("-", "")[:8])
            ident["vendor_id"] = raw[0] | (raw[1] << 8)
        elif text:
            logging.getLogger("comfy_aimdo.xpu").debug(
                "comfy-aimdo XPU: device uuid %r is not the canonical "
                "8-4-4-4-12 hex form; vendor id left unknown", text)
    except Exception:
        return ident
    return ident


def _dxgi_probe_adapter(adapter):
    """Return identity + LOCAL budget for one IDXGIAdapter, or None if unusable.

    Raises on DXGI failure (ctypes turns a failure HRESULT into OSError); the
    caller decides whether to skip the adapter or give up.
    """
    avt = ctypes.cast(adapter, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    desc = _DXGI_ADAPTER_DESC()
    get_desc = ctypes.cast(
        avt.contents[_DXGI_GETDESC_VTABLE_ORDINAL],
        ctypes.CFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_DXGI_ADAPTER_DESC)),
    )
    get_desc(adapter, ctypes.byref(desc))
    query_iface = ctypes.cast(
        avt.contents[0],
        ctypes.CFUNCTYPE(
            ctypes.HRESULT, ctypes.c_void_p, ctypes.POINTER(_CapGUID),
            ctypes.POINTER(ctypes.c_void_p)),
    )
    ad3 = ctypes.c_void_p()
    query_iface(adapter, ctypes.byref(_IID_IDXGIAdapter3), ctypes.byref(ad3))
    try:
        if not ad3.value:
            return None
        ad3_ptr = ctypes.cast(ad3, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
        qvmi = ctypes.cast(
            ad3_ptr.contents[_DXGI_QVMI_VTABLE_ORDINAL],
            ctypes.CFUNCTYPE(
                ctypes.HRESULT, ctypes.c_void_p, ctypes.c_uint,
                ctypes.c_uint, ctypes.POINTER(_DXGI_QUERY_VIDEO_MEMORY_INFO)),
        )
        info = _DXGI_QUERY_VIDEO_MEMORY_INFO()
        # Clear explicitly: the budget<=0 drift defence must not depend on a
        # freshly built Structure happening to be zero-initialised.
        ctypes.memset(ctypes.byref(info), 0, ctypes.sizeof(info))
        qvmi(ad3, 0, 0, ctypes.byref(info))  # node 0, LOCAL segment
        budget = int(info.Budget)
        if budget <= 0:
            # Nothing was written (e.g. a future vtable drift landed on an
            # ordinal that does not touch the struct); never present it as a
            # real reading.
            return None
        return {
            "description": str(desc.Description),
            "vendor_id": int(desc.VendorId),
            "device_id": int(desc.DeviceId),
            "dedicated": int(desc.DedicatedVideoMemory),
            "luid": (int(desc.AdapterLuid.LowPart), int(desc.AdapterLuid.HighPart)),
            "budget": budget,
            "usage": int(info.CurrentUsage),
        }
    finally:
        _com_release(ad3)


def _dxgi_local_budget_bytes():
    """Return (budget_bytes, current_usage_bytes) from the DXGI LOCAL segment
    of the adapter that matches the torch XPU device.

    Best-effort Windows-only. Taking the first IDXGIAdapter3 is NOT safe: this
    machine also exposes "Microsoft Basic Render Driver" (Budget 15.223 GiB,
    DedicatedVideoMemory 0), and if it were picked the fraction would be
    clamped to the 0.90 ceiling (10.44 GiB) instead of the correct ~7.08 GiB.

    Matching order: PCI vendor+device id -> normalized device name -> explicit
    loud fallback (never a silent index-0 pick). Returns None when DXGI is
    unavailable or no usable adapter is found, so the caller can fail soft.

    Not cached: every call builds a new DXGI factory and walks the full
    adapter enumeration (one complete DXGI traversal). The COM reference leak
    is fixed, so calling this in a loop no longer leaks -- but it still costs
    a full traversal each time, so keep it off hot paths and out of frequent
    loops.
    """
    if platform.system() != "Windows":
        return None
    try:
        dxgi = ctypes.windll.dxgi
    except Exception:
        return None
    log = logging.getLogger("comfy_aimdo.xpu")
    ident = _xpu_device_identity()
    want_vendor = int(ident.get("vendor_id") or 0)
    want_device = int(ident.get("device_id") or 0)
    want_name = _normalize_adapter_name(ident.get("name"))
    candidates = []
    try:
        create_factory = dxgi.CreateDXGIFactory1
        create_factory.argtypes = [ctypes.POINTER(_CapGUID), ctypes.c_void_p]
        create_factory.restype = ctypes.HRESULT
        factory = ctypes.c_void_p()
        hr = create_factory(ctypes.byref(_IID_IDXGIFactory1), ctypes.byref(factory))
        if hr < 0 or not factory.value:
            return None
        try:
            vt = ctypes.cast(factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
            enum_adapters = ctypes.cast(
                vt.contents[7],
                ctypes.CFUNCTYPE(
                    ctypes.HRESULT, ctypes.c_void_p, ctypes.c_uint,
                    ctypes.POINTER(ctypes.c_void_p)),
            )
            i = 0
            while i <= _DXGI_ADAPTER_ENUM_LIMIT:
                adapter = ctypes.c_void_p()
                try:
                    hr = enum_adapters(factory, i, ctypes.byref(adapter))
                except OSError:
                    break  # DXGI_ERROR_NOT_FOUND: end of the adapter list
                if hr < 0 or not adapter.value:
                    break
                try:
                    probe = _dxgi_probe_adapter(adapter)
                    if probe is None:
                        log.debug("comfy-aimdo XPU: DXGI adapter #%d skipped "
                                  "(no usable LOCAL budget)", i)
                    else:
                        probe["index"] = i
                        candidates.append(probe)
                except Exception as exc:
                    log.debug("comfy-aimdo XPU: DXGI adapter #%d unusable: %s",
                              i, exc)
                finally:
                    # Every exit path (break / probe failure / success) drops
                    # the reference; the enclosing finally drops the factory.
                    _com_release(adapter)
                i += 1
        finally:
            _com_release(factory)
    except Exception:
        return None

    if not candidates:
        log.warning(
            "comfy-aimdo XPU: no DXGI adapter reported a usable LOCAL budget; "
            "allocator cap skipped (OmniXPU 0.99 retained)")
        return None

    def _fmt(c):
        return "#%d %s vendor=0x%04x device=0x%04x budget=%.3f GiB " \
               "dedicated=%.3f GiB" % (
                   c["index"], c["description"], c["vendor_id"], c["device_id"],
                   c["budget"] / 1024.0 ** 3, c["dedicated"] / 1024.0 ** 3)

    picked = None
    reason = ""
    if want_device > 0:
        for cand in candidates:
            if cand["device_id"] == want_device and (
                    want_vendor == 0 or cand["vendor_id"] == want_vendor):
                picked, reason = cand, "pci-id"
                break
    if picked is None and want_name:
        # Exact match first...
        for cand in candidates:
            if _normalize_adapter_name(cand["description"]) == want_name:
                picked, reason = cand, "name"
                break
    if picked is None and want_name:
        # ...then containment, which _adapter_names_match only allows when
        # both names carry >= 2 tokens (guards against "arc" in "arc b580").
        for cand in candidates:
            if _adapter_names_match(
                    want_name, _normalize_adapter_name(cand["description"])):
                picked, reason = cand, "name-substring"
                break
    if picked is None:
        fallback = next((c for c in candidates if c["dedicated"] > 0), None)
        reason = "fallback-dedicated-vram" if fallback is not None else "fallback-first"
        picked = fallback if fallback is not None else candidates[0]
        log.warning(
            "comfy-aimdo XPU: no DXGI adapter matched the torch XPU device "
            "(wanted name=%r vendor=0x%04x device=0x%04x); candidates=[%s]; "
            "picked #%d %r (%s, budget=%.3f GiB) -- the allocator cap may be "
            "based on the wrong adapter",
            ident.get("name"), want_vendor, want_device,
            "; ".join(_fmt(c) for c in candidates),
            picked["index"], picked["description"], reason,
            picked["budget"] / 1024.0 ** 3)
    else:
        log.info(
            "comfy-aimdo XPU: DXGI adapter selected by %s: #%d %r "
            "(vendor=0x%04x device=0x%04x, budget=%.3f GiB, dedicated=%.3f GiB)",
            reason, picked["index"], picked["description"], picked["vendor_id"],
            picked["device_id"], picked["budget"] / 1024.0 ** 3,
            picked["dedicated"] / 1024.0 ** 3)
    return (picked["budget"], picked["usage"])


def recommend_xpu_allocator_fraction(
    headroom_gib: float = _XPU_ALLOCATOR_CAP_HEADROOM_GIB,
    floor: float = _XPU_ALLOCATOR_CAP_FLOOR,
    ceil: float = _XPU_ALLOCATOR_CAP_CEIL,
):
    """Compute the PyTorch per-process memory fraction from DXGI real-free.

    PyTorch's caching-allocator upper bound = fraction * torch_total. We want
    that bound = DXGI Budget - headroom (leave driver/system headroom, force
    early ComfyUI RAM offload), so::

        fraction = (budget_bytes - headroom_bytes) / torch_total_bytes

    clamped to [floor, ceil]. Returns None when XPU / DXGI is unavailable so the
    caller keeps OmniXPU's 0.99 (fail-soft, never worse than before). This is a
    MEASURED value, not a hardcoded fraction.

    Not cached: each call re-reads the DXGI budget through
    _dxgi_local_budget_bytes(), i.e. a full factory + adapter enumeration. See
    that function's docstring -- do not call this from a hot path or a
    frequent loop.
    """
    try:
        import torch
    except Exception:
        return None
    xpu = getattr(torch, "xpu", None)
    if xpu is None or not xpu.is_available():
        return None
    dxgi = _dxgi_local_budget_bytes()
    if dxgi is None:
        return None
    budget_bytes, _usage_bytes = dxgi
    try:
        idx = xpu.current_device()
        total_bytes = int(xpu.get_device_properties(idx).total_memory)
    except Exception:
        return None
    if budget_bytes <= 0 or total_bytes <= 0:
        return None
    headroom_bytes = int(headroom_gib * 1024 ** 3)
    allowed_bytes = budget_bytes - headroom_bytes
    if allowed_bytes <= 0:
        return None
    fraction = allowed_bytes / total_bytes
    if fraction > 1.0:
        fraction = 1.0
    if fraction < floor:
        fraction = floor
    if fraction > ceil:
        fraction = ceil
    return fraction


def apply_xpu_allocator_cap(
    headroom_gib: float = _XPU_ALLOCATOR_CAP_HEADROOM_GIB,
) -> None:
    """Override OmniXPU's 0.99 XPU allocator cap with a DXGI real-free fraction.

    Runs EARLY in control.init() (ComfyUI main.py:74-84), BEFORE OmniXPU's
    prestartup (custom-node load). Two channels:
      1. Set OMNIXPU_XPU_MEMORY_FRACTION -- OmniXPU reads this env var first and
         only falls back to 0.99 when unset, so our value wins at ITS OWN
         prestartup (correct timing; a plain setter in setup_backend is
         clobbered -- see the abandoned xpu-allocator-cap branch 06cd2d5).
      2. Call torch.xpu.set_per_process_memory_fraction directly as a
         belt-and-suspenders measure (covers the OmniXPU-absent case and
         reinforces when OmniXPU applies the same value from the env).
    Fail-soft: any failure is logged, never blocks ComfyUI startup.
    """
    global _xpu_allocator_cap_fraction
    log = logging.getLogger("comfy_aimdo.xpu")
    try:
        import torch
        xpu = getattr(torch, "xpu", None)
        if xpu is None or not xpu.is_available():
            _xpu_allocator_cap_fraction = None
            return
        fraction = recommend_xpu_allocator_fraction(headroom_gib=headroom_gib)
        if fraction is None or fraction <= 0.0:
            log.info(
                "comfy-aimdo XPU: allocator cap skipped (DXGI real-free "
                "unavailable); OmniXPU 0.99 retained")
            _xpu_allocator_cap_fraction = None
            return
        # Channel 1: env var OmniXPU reads at its prestartup (correct timing).
        os.environ[_XPU_ALLOCATOR_CAP_ENV] = "%.6f" % fraction
        # Channel 2: direct setter (fail-soft; OmniXPU may re-apply same value).
        try:
            setter = getattr(xpu, "set_per_process_memory_fraction", None)
            if callable(setter):
                setter(fraction)
        except Exception as _exc:
            log.warning(
                "comfy-aimdo XPU: direct allocator cap setter skipped "
                "(non-fatal): %s" % _exc)
        total_gib = (
            xpu.get_device_properties(xpu.current_device()).total_memory
            / (1024 ** 3))
        allowed_gib = fraction * total_gib
        log.info(
            "comfy-aimdo XPU: allocator cap -> fraction=%.4f (allowed=%.2f GiB), "
            "headroom=%.2f GiB, torch_total=%.2f GiB, env=%s set for OmniXPU "
            "prestartup" % (fraction, allowed_gib, headroom_gib, total_gib,
                            _XPU_ALLOCATOR_CAP_ENV))
        _xpu_allocator_cap_fraction = fraction
    except Exception as _exc:
        log.warning(
            "comfy-aimdo XPU: allocator cap skipped (non-fatal): %s" % _exc)
        _xpu_allocator_cap_fraction = None


def get_xpu_allocator_cap_fraction():
    """Return the fraction applied by apply_xpu_allocator_cap(), or None."""
    return _xpu_allocator_cap_fraction
