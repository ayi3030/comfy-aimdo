import os
import atexit
import ctypes
import platform
import struct
import sys
import time
from pathlib import Path
import logging
import importlib.util

lib = None
devctxs = []
_log_callback = None

# --- XPU 诊断 / 卸载协同状态 -------------------------------------------------
#
# 这些状态只服务 XPU 后端的三层卸载与 OOM 诊断，不参与 CUDA / ROCm 路径。
# XPU 的 allocator 模式与就绪标志仍然由 xpu.py 持有（那是本仓库独有的模块，
# 比社区 fork 更完整），这里只做一层薄转发，避免出现两个互不同步的真值源。
_xpu_oom_history = []
_XPU_OOM_HISTORY_LIMIT = 4
_XPU_OOM_SNAPSHOT_INTERVAL_SECONDS = 2.0
_xpu_oom_last_snapshot_monotonic = {}
# Windows 把 SYCL 运行库放在 torch/xpu 的 bin 目录下；CDLL 之前必须让
# Windows 知道去哪里找 syclN.dll，否则 aimdo_xpu.dll 加载失败。
_windows_dll_directories = []
_windows_dll_directory_paths = set()

# Backend actually in use ("cuda" / "rocm" / "xpu"), resolved by init().
# This must be module-level: torch.get_torch_allocator() and xpu.py read it
# through the module. When it lived only as an init() local, every such read
# raised AttributeError -- which xpu.py swallowed, silently degrading the
# "global" allocator mode instead of reporting the failure.
implementation = None

_LOG_CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p)
_LOG_LEVELS = {
    1: logging.CRITICAL,
    2: logging.ERROR,
    3: logging.WARNING,
    4: logging.INFO,
    5: logging.DEBUG,
    6: logging.DEBUG,
    7: logging.DEBUG,
}


def _native_log(level, message):
    logging.log(_LOG_LEVELS.get(level, logging.DEBUG),
                message.decode("utf-8", errors="replace").rstrip())


# --- SYCL runtime ABI diagnostics (Intel XPU) ---------------------------------
#
# aimdo_xpu.dll links against the Intel SYCL runtime, whose DLL name is versioned
# per oneAPI release (sycl7 = 2024.x, sycl8 = 2025.x, sycl9 = 2026.x). Because
# src-xpu/dispatch.cpp receives torch's `sycl::queue*` directly, the DLL's SYCL
# ABI MUST match the runtime torch.xpu itself was built against. A mismatch is
# not cosmetic: the DLL either fails to load (the required syclN.dll is absent)
# or, worse, silently marshals sycl::queue objects across incompatible ABIs.
#
# These helpers turn the opaque ctypes "Could not find module ... (or one of its
# dependencies)" into an actionable message naming the exact mismatch.

_SYCL_ABI = {7: "2024.x", 8: "2025.x", 9: "2026.x", 10: "2027.x"}


def _pe_import_dlls(dll_path):
    """Return the DLL names in a PE file's import table, or [] if unparsable."""
    try:
        data = Path(dll_path).read_bytes()
        if data[:2] != b"MZ":
            return []
        e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
            return []
        opt = e_lfanew + 4 + 20
        magic = struct.unpack_from("<H", data, opt)[0]
        imp_rva = struct.unpack_from("<I", data, opt + (120 if magic == 0x20B else 104))[0]
        opt_size = struct.unpack_from("<H", data, e_lfanew + 4 + 16)[0]
        sec_off = e_lfanew + 4 + 20 + opt_size
        nsec = struct.unpack_from("<H", data, e_lfanew + 4 + 2)[0]

        def rva_to_off(rva):
            for i in range(nsec):
                so = sec_off + i * 40
                vsize = struct.unpack_from("<I", data, so + 8)[0]
                vaddr = struct.unpack_from("<I", data, so + 12)[0]
                rawptr = struct.unpack_from("<I", data, so + 20)[0]
                if vaddr <= rva < vaddr + vsize:
                    return rawptr + (rva - vaddr)
            return rva

        table = rva_to_off(imp_rva)
        names = []
        i = 0
        while struct.unpack_from("<I", data, table + i * 20)[0] != 0:
            name_rva = struct.unpack_from("<I", data, table + i * 20 + 12)[0]
            off = rva_to_off(name_rva)
            names.append(data[off:data.index(b"\x00", off)].decode("utf-8", "replace"))
            i += 1
        return names
    except Exception:
        return []


def _available_sycl_runtimes():
    """Map lowercased sycl*.dll filenames found on this system to their path."""
    dirs = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    dirs.append(os.path.join(sys.prefix, "Library", "bin"))
    try:
        import torch  # noqa: PLC0415
        torch_lib = Path(torch.__file__).parent / "lib"
        dirs.append(str(torch_lib))
        dirs.append(str(torch_lib.parent.parent / "Library" / "bin"))
    except Exception:
        pass
    found = {}
    for directory in dirs:
        try:
            for name in os.listdir(directory):
                low = name.lower()
                if low.startswith("sycl") and low.endswith(".dll"):
                    found.setdefault(low, os.path.join(directory, name))
        except Exception:
            continue
    return found


def _sycl_abi_hint(dll_path):
    """Explain a likely SYCL runtime ABI mismatch for the given DLL."""
    required = [n for n in _pe_import_dlls(dll_path) if n.lower().startswith("sycl")]
    if not required:
        return []
    available = _available_sycl_runtimes()
    lines = []
    for name in required:
        if name.lower() in available:
            continue
        stem = name.lower()[len("sycl"):].split(".")[0]
        want = _SYCL_ABI.get(int(stem), "unknown") if stem.isdigit() else "unknown"
        lines.append(
            f"  - requires {name} (Intel SYCL runtime, oneAPI {want}) "
            f"but it was NOT found on this system"
        )
    if not lines:
        return []
    have = ", ".join(sorted(available)) or "(none)"
    lines.append(f"  SYCL runtimes present on this system: {have}")
    lines.append(
        "  torch+xpu ships its own pinned SYCL runtime; aimdo_xpu.dll must be built "
        "with the SAME oneAPI major version as that torch build, because "
        "src-xpu/dispatch.cpp passes sycl::queue objects across that boundary."
    )
    lines.append(
        "  Fix: rebuild aimdo_xpu.dll against the oneAPI version matching your "
        "torch.*+xpu install (e.g. sycl9.dll = oneAPI 2026.x), or install the "
        "matching Intel oneAPI DPC++ runtime."
    )
    return lines


def detect_vendor():
    version = ""
    hip = None
    cuda = None
    try:
        torch_spec = importlib.util.find_spec("torch")
        for folder in torch_spec.submodule_search_locations:
            ver_file = Path(folder) / "version.py"
            if ver_file.is_file():
                spec = importlib.util.spec_from_file_location("torch_version_import", ver_file)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                version = module.__version__
                hip = getattr(module, "hip", None)
                cuda = getattr(module, "cuda", None)
    except Exception as e:
        logging.warning("Failed to detect Torch version")
        pass

    # torch.version.hip/cuda are authoritative. The local version segment is only
    # a fallback: ROCm nightlies do not always carry a +rocm suffix.
    if hip:
        return "rocm"
    if cuda:
        return "cuda"

    if '+cu' in version:
        return "cuda"
    if '+rocm' in version:
        return "rocm"
    if '+xpu' in version:
        return "xpu"
    return None


def init(implementation: str | None = None, simple_vram_headroom: int | None = None, nvml_pressure: bool = False, xpu_allocator_mode: str | None = None):
    global lib, _log_callback
    # NOTE: `implementation` is intentionally absent here -- it is a parameter
    # of this function, and Python forbids a parameter from also being declared
    # global. It is published to the module via globals() further down.

    if lib is not None:
        if simple_vram_headroom is not None:
            lib.set_simple_vram_headroom(int(simple_vram_headroom))
        lib.set_nvml_pressure(bool(nvml_pressure))
        return True

    # Remember whether the caller named the backend explicitly. This is read
    # later when handing control to the XPU backend: an explicit "xpu" request
    # must bypass the opt-in environment gate, whereas an auto-detected one
    # must not. Must be captured before `implementation` is defaulted below.
    implementation_was_explicit = implementation is not None

    if implementation is None:
        implementation = detect_vendor()

    if implementation is None:
        logging.warning("Could not autodetect AIMDO implementation, assuming Nvidia")
        implementation = "cuda"

    # Publish the resolved backend at module scope. The parameter shadows the
    # module-level name inside this function, so globals() is required for the
    # assignment to actually stick -- torch.py and xpu.py read it off the module.
    globals()["implementation"] = implementation

    impl = {
        "cuda": "aimdo",
        "rocm": "aimdo_rocm",
        "xpu": "aimdo_xpu",
    }[implementation]

    if implementation == "xpu":
        _preload_torch_runtime()

    try:
        base_path = Path(__file__).parent.resolve()
        system = platform.system()
        if system == "Windows":
            ext = "dll"
            mode = 0
        elif system == "Linux":
            ext = "so"
            mode = 258
        else:
            logging.info(f"comfy-aimdo unsupported operating system: {system}")
            logging.info(f"NOTE: comfy-aimdo currently only supports Windows and Linux")
            return False
        lib = ctypes.CDLL(str(base_path / f"{impl}.{ext}"), mode=mode)
    except Exception as e:
        logging.info(f"comfy-aimdo failed to load: {e}")
        if implementation == "xpu":
            hints = _sycl_abi_hint(base_path / f"{impl}.{ext}")
            if hints:
                logging.info(
                    "comfy-aimdo XPU: SYCL runtime ABI mismatch -- the bundled "
                    "native backend cannot be loaded on this system:"
                )
                for line in hints:
                    logging.info(line)
                return False
        logging.info(f"NOTE: comfy-aimdo currently only supports Nvidia and AMD GPUs")
        return False

    lib.set_log_callback.argtypes = [_LOG_CALLBACK]
    lib.set_log_callback.restype = None
    _log_callback = _LOG_CALLBACK(_native_log)
    lib.set_log_callback(_log_callback)

    lib.get_total_vram_usage.argtypes = [ctypes.c_void_p]
    lib.get_total_vram_usage.restype = ctypes.c_uint64

    # 诊断导出 get_vram_capacity(devctx)：读回 g_devctx->_vram_capacity，
    # 即 init 时 cuDeviceTotalMem 写入的物理显存上限（Arc B580 约 11875MB）。
    # budget_deficit() 用它算预算赤字，排查驻留超限时必须能直接读到真值。
    #
    # 刻意按存在性绑定：这些绑定位于 CDLL try 块之外，无条件绑定缺失符号会抛
    # AttributeError 并中断 init()，让整个 XPU 后端起不来（与 malloc_graph_*
    # 同理）。src/control.c 来自社区 fork 且不参与本仓库的 XPU 编译，符号可能
    # 缺失；缺失时降级为不可用，而不是让 ComfyUI 起不来。
    if hasattr(lib, "get_vram_capacity"):
        lib.get_vram_capacity.argtypes = [ctypes.c_void_p]
        lib.get_vram_capacity.restype = ctypes.c_uint64
    elif hasattr(lib, "xpu_get_vram_capacity"):
        lib.xpu_get_vram_capacity.argtypes = [ctypes.c_int]
        lib.xpu_get_vram_capacity.restype = ctypes.c_uint64
    else:
        logging.info("NOTE: get_vram_capacity not exported by this backend; "
                     "VRAM capacity readback unavailable")

    lib.aimdo_analyze.argtypes = [ctypes.c_void_p]

    lib.set_simple_vram_headroom.argtypes = [ctypes.c_int64]
    lib.set_simple_vram_headroom.restype = None

    lib.get_simple_vram_headroom.argtypes = []
    lib.get_simple_vram_headroom.restype = ctypes.c_int64

    lib.set_nvml_pressure.argtypes = [ctypes.c_bool]
    lib.set_nvml_pressure.restype = None

    lib.init.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint64), ctypes.c_size_t]
    lib.init.restype = ctypes.c_bool

    lib.get_devctx.argtypes = [ctypes.c_int]
    lib.get_devctx.restype = ctypes.c_void_p

    # malloc-graph capture is a CUDA-only capability: the XPU native backend
    # (aimdo_xpu.dll) does not export the malloc_graph_* family at all. Binding
    # argtypes on a missing export raises AttributeError and aborts init() before
    # the XPU backend ever gets wired up. Bind them only when present, and
    # degrade fail-soft (ComfyUI then simply runs without graph capture) instead
    # of taking down the whole allocator initialization.
    _malloc_graph_symbols = (
        ("malloc_graph_create", [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool], ctypes.c_void_p),
        ("malloc_graph_push", [ctypes.c_void_p, ctypes.c_char_p], ctypes.c_bool),
        ("malloc_graph_pause", [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool], ctypes.c_bool),
        ("malloc_graph_set_stream", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_bool),
        ("malloc_graph_pop", [ctypes.c_void_p], ctypes.c_int),
        ("malloc_graph_abort", [ctypes.c_void_p], ctypes.c_bool),
        ("malloc_graph_stat", [ctypes.c_void_p, ctypes.c_int], ctypes.c_uint64),
        ("malloc_graph_destroy", [ctypes.c_void_p], None),
    )
    if all(hasattr(lib, name) for name, _, _ in _malloc_graph_symbols):
        for _name, _argtypes, _restype in _malloc_graph_symbols:
            getattr(lib, _name).argtypes = _argtypes
            getattr(lib, _name).restype = _restype
    else:
        _missing = [name for name, _, _ in _malloc_graph_symbols if not hasattr(lib, name)]
        logging.info(
            "comfy-aimdo: native backend does not provide malloc-graph capture "
            f"({', '.join(_missing)}); continuing without graph capture"
        )

    if simple_vram_headroom is not None:
        lib.set_simple_vram_headroom(int(simple_vram_headroom))
    lib.set_nvml_pressure(bool(nvml_pressure))

    if implementation == "xpu":
        # Delegate all XPU-specific wiring (argtypes, allocator install,
        # capability gate) to the dedicated backend. It returns False when the
        # AIMDO XPU runtime or the UR-USM hook is unusable, which makes
        # ComfyUI transparently keep PyTorch's native XPU allocator.
        from . import xpu as _xpu_backend
        if not _xpu_backend.setup_backend(
            lib, xpu_allocator_mode, system,
            explicitly_requested=implementation_was_explicit,
        ):
            return False

    return True

def init_devices(device_ids):
    global devctxs

    if lib is None:
        return False

    if devctxs:
        logging.warning("comfy-aimdo devices are already initialized, call deinit() first")
        return False

    requested = []
    headrooms = []
    for device_id in device_ids:
        if isinstance(device_id, tuple):
            if len(device_id) != 2:
                raise ValueError("device tuple must be (device_id, extra_vram_headroom)")
            device_id, headroom = device_id
        else:
            headroom = 0

        headroom = int(headroom)
        if headroom < 0:
            raise ValueError("extra_vram_headroom must be non-negative")
        requested.append(int(device_id))
        headrooms.append(headroom)

    if not requested:
        return False

    if not lib.plat_init():
        return False

    device_array = (ctypes.c_int * len(requested))(*requested)
    headroom_array = (ctypes.c_uint64 * len(headrooms))(*headrooms)
    if lib.init(device_array, headroom_array, len(requested)):
        devctxs = [get_devctx(device_id) for device_id in requested]
        _register_atexit()
        return True

    devctxs = []
    lib.plat_cleanup()
    return False

def init_device(device_id, extra_vram_headroom: int = 0):
    if extra_vram_headroom:
        device_id = (device_id, extra_vram_headroom)
    return init_devices([device_id])


def record(stream, assert_graph_breaks=False):
    from .malloc_graph import record as malloc_graph_record
    return malloc_graph_record(stream, assert_graph_breaks)

def get_devctx(device_id: int):
    devctx = lib.get_devctx(int(device_id))
    if devctx:
        return devctx
    raise RuntimeError(f"comfy-aimdo device {device_id} is not initialized")

def set_simple_vram_headroom(headroom: int):
    """Set the VRAM the simple budget keeps free, in bytes.

    One process wide value, compared against each device's own capacity. It
    is separate from the per device extra_vram_headroom given to
    init_devices(). Only the simple budget term reads it; the measured poll
    term keeps its own compile time floor of 256 MB (VRAM_HEADROOM) and the
    budget takes the larger of the two, so raising this above 256 MB is
    honoured but lowering it below 256 MB changes nothing. Raising it takes
    effect at the next VBAR fault or hooked device allocation and is honoured
    by evicting VBAR pages only; torch allocations are counted against it
    but never refused. Lowering it does not refill anything by itself: pages
    come back when a VBAR is next prioritized, which ComfyUI does when it
    loads a model.
    """
    headroom = int(headroom)
    if headroom < 0 or headroom > (1 << 60):
        raise ValueError("simple_vram_headroom must be between 0 and 2**60 bytes")
    if lib is None:
        raise RuntimeError("comfy-aimdo is not initialized")
    lib.set_simple_vram_headroom(headroom)

def get_simple_vram_headroom():
    if lib is None:
        raise RuntimeError("comfy-aimdo is not initialized")
    return int(lib.get_simple_vram_headroom())

_atexit_registered = False


def _preload_torch_runtime():
    """Make aimdo_xpu.dll loadable before torch is imported.

    aimdo_xpu.dll imports the SYCL runtime directly (syclN.dll), and Windows
    resolves a DLL's imports against modules already present in the process.
    ComfyUI calls comfy_aimdo.control.init() from main.py long before it
    imports torch, so on a real launch the load fails with

        Could not find module 'aimdo_xpu.dll' (or one of its dependencies)

    even though the file sits right there -- measured on Arc B580, this is
    exactly what stock ComfyUI did before this was added.

    Only `import torch` reliably fixes it. Loading torch/lib/torch_xpu.dll on
    its own was measured and does NOT work: it depends on the same oneAPI
    runtime that is not set up yet, so the load fails and aimdo_xpu.dll stays
    unloadable. Importing torch performs that runtime setup.

    Consequence: ComfyUI logs, at main.py:249,
        WARNING: Potential Error in code: Torch already imported,
                 torch should never be imported before this point.
    That warning is advisory, and the alternative is the XPU backend silently
    not loading at all. torch is imported by ComfyUI a few lines later anyway
    and the module is cached, so this only moves the import earlier; no XPU
    context is created and nothing is allocated until init_devices().

    Failure is silent: if torch is genuinely unavailable, the CDLL() call
    immediately after reports it with full detail.
    """
    if "torch" in sys.modules:
        return  # already imported; the runtime is present
    try:
        import torch  # noqa: F401
    except Exception as _error:
        logging.info(
            f"comfy-aimdo XPU: could not preload the torch runtime "
            f"({_error!r}); the SYCL runtime may be missing"
        )


def _register_atexit():
    """Run deinit() on interpreter shutdown (teardown-review F-06).

    ComfyUI only ever calls init() and init_devices(); nothing in the host ever
    calls deinit(). Without this hook the Level Zero / Unified Runtime detours
    and the accounting table simply stay live until the OS reclaims the
    process, which matters for the prestartup-injection and plugin-reload
    setups the XPU backend targets.

    Registered only on a successful init_devices(), and wrapped because
    interpreter shutdown is an hostile place to raise: modules may already be
    torn down and stdout may be gone.
    """
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True

    def _safe_deinit():
        try:
            if lib is not None:
                deinit()
        except Exception:
            pass  # nothing useful can be reported this late

    atexit.register(_safe_deinit)


def deinit():
    global lib, devctxs, _log_callback, _atexit_registered
    if lib is None:
        return
    # Allow the hook to be re-armed if the backend is loaded again.
    _atexit_registered = False

    # Restore the torch.xpu entry points and reset the backend state BEFORE
    # releasing the native library. The AIMDO wrappers installed by
    # xpu.setup_backend() close over `lib`; leaving them attached after it is
    # unloaded leaves torch calling into a half-torn-down backend (verified on
    # Arc B580: torch.xpu.empty_cache stayed bound to comfy_aimdo.xpu after
    # deinit(), and every state flag kept its loaded value).
    try:
        from . import xpu as _xpu_backend
        _xpu_backend.teardown_backend()
    except Exception as _error:  # teardown must never block unloading
        logging.info(f"comfy-aimdo: XPU backend teardown skipped: {_error!r}")

    # Detach the native log callback while the library is still alive, and
    # always clear the Python-side reference. Doing this first removes the
    # window in which native code could call a CFUNCTYPE that Python has
    # already dropped -- a dangling function pointer.
    try:
        lib.set_log_callback(ctypes.cast(None, _LOG_CALLBACK))
    finally:
        _log_callback = None

    # From here on the platform teardown must not abort the remaining steps.
    # cleanup() releases device resources, plat_cleanup() detaches the
    # Level Zero / Unified Runtime hooks; neither depends on the other's
    # bookkeeping being visible afterwards.
    try:
        lib.cleanup()
        devctxs = []
    finally:
        try:
            lib.plat_cleanup()
        finally:
            lib = None


def set_log_none(): lib.set_log_level_none()
def set_log_critical(): lib.set_log_level_critical()
def set_log_error(): lib.set_log_level_error()
def set_log_warning(): lib.set_log_level_warning()
def set_log_info(): lib.set_log_level_info()
def set_log_debug(): lib.set_log_level_debug()
def set_log_verbose(): lib.set_log_level_verbose()
def set_log_vverbose(): lib.set_log_level_vverbose()

def analyze():
    if lib is None:
        return
    for devctx in devctxs:
        lib.aimdo_analyze(devctx)

def get_total_vram_usage():
    if lib is None:
        return 0
    return sum(lib.get_total_vram_usage(devctx) for devctx in devctxs)


def get_vram_capacity(device=0):
    """读回物理显存上限（g_devctx->_vram_capacity），单位字节。

    Book A 的合理上界就是它：驻留超过该值说明预算回收没跟上，而不是模型
    真的需要那么多显存。诊断用——后端未导出该符号时返回 0 表示不可用。
    """
    if lib is None:
        return 0
    if hasattr(lib, "get_vram_capacity"):
        devctx = get_devctx(device)
        return int(lib.get_vram_capacity(devctx)) if devctx else 0
    if hasattr(lib, "xpu_get_vram_capacity"):
        return int(lib.xpu_get_vram_capacity(int(device)))
    return 0


# ---------------------------------------------------------------------------
# 以下为社区 fork 的三层卸载 / OOM 诊断能力移植。
#
# 分工约定（重要）：
#   * xpu.py 是本仓库独有的 XPU 后端模块，持有 allocator 模式与就绪状态的
#     唯一真值源。control.py 这里只做「防御性转发」，不重复维护一份状态，
#     否则两处状态各说各话，卸载决策就会基于过期的 allocator 模式。
#   * 所有 lib.<symbol> 一律 hasattr 守卫。老 DLL 缺符号时降级为「该诊断
#     不可用」，绝不抛 AttributeError —— 那会中断调用方（fault 路径）。
#   * CUDA / ROCm 路径：以下函数全部以 implementation != "xpu" 早退，
#     与改动前行为完全一致。
# ---------------------------------------------------------------------------


def _xpu_device_index(device=None):
    """把 torch.device / int / None 统一成 XPU 设备序号。

    torch 只在 XPU 后端激活时才有 xpu 子模块，所以整个函数包在 try 里：
    拿不到就返回 None，由调用方决定降级行为，绝不让诊断代码炸掉采样。
    """
    try:
        import torch
    except Exception:
        return None

    if not hasattr(torch, "xpu"):
        return None
    try:
        if device is None:
            return int(torch.xpu.current_device())
        if isinstance(device, int):
            return int(device)
        # torch.device 可能没有 index（例如 torch.device("xpu")）
        index = getattr(device, "index", None)
        return int(torch.xpu.current_device() if index is None else index)
    except Exception:
        return None


def get_xpu_allocator_mode():
    """当前 XPU allocator 模式（"native_hook" / "global" / None）。

    转发到 xpu.py —— 那里才是这个状态的唯一真值源。未激活时返回 None。
    """
    try:
        from . import xpu as _xpu
        return _xpu.get_allocator_mode()
    except Exception:
        return None


def get_xpu_allocator_memory_stats(device=None):
    """返回 (active, reserved, peak_active, peak_reserved)，单位字节。

    与社区 fork 的同名函数语义一致，但实现转发到 xpu.py：xpu.py 优先读
    Book A（xpu_get_total_vram_usage），避免与 torch 自记账副本双记账。
    不可用时返回 (0, 0, 0, 0) 而不是 None —— 调用方（prioritize /
    _release_native_cache）会直接解包四个值，返回 None 会炸。
    """
    try:
        from . import xpu as _xpu
        stats = _xpu.get_xpu_allocator_memory_stats(device)
    except Exception:
        return (0, 0, 0, 0)
    if stats is None:
        return (0, 0, 0, 0)
    return tuple(int(value) for value in stats)


def get_xpu_torch_reserved_growth(device=None):
    """返回「torch 预留峰值超出当前预留的量」，单位字节；不可用时返回 0。

    这是一个**专用**口径，只为 vbars_prepare_allocation 的 anticipated_growth
    服务，不能用 get_xpu_allocator_memory_stats() 代替。原因是 native 侧
    budget_deficit() 会把这个值直接加到 Book A 上：

        deficit_simple = (total_vram_usage + size) + headroom - capacity

    size 的语义必须是「本次激活预计新增的驻留」，即一个**前瞻增量**，且与
    Book A 不同源。若误用 Book A 口径（peak - current），size 与
    total_vram_usage 就变成同源，相加后current 被代数抵消：

        current + (peak - current) = peak

    deficit 退化成只看历史高水位、与当前真实压力脱钩。实测中这会把驱逐
    目标压到 capacity - headroom - (peak - current)；当 peak 远大于 capacity
    时该目标为负，于是每次模型切换都要求"驱逐全部可驱逐页"，正是
    「采样极慢 + 反复重载」的形态。

    fork 的原始取值是 torch.xpu.memory_stats() 的
    reserved_bytes.all.peak - reserved_bytes.all.current —— torch 自己知道
    自己的预留历史，且这是与 Book A 不同源的前瞻量，语义正确。

    实测（Arc B580 / torch 2.14.0+xpu，绕过包装器后）：
        分配 4x256MiB     reserved=1024MiB allocated=1024MiB -> cached=   0MiB
        释放后（未trim）    reserved=1024MiB allocated=   0MiB -> cached=1024MiB
        empty_cache 后    reserved=   0MiB allocated=   0MiB -> cached=   0MiB
    即 torch 只在 empty_cache() 后才把 cached 降下来 —— 这正是 L3 存在的
    理由，也是 L2 必须与 L3 串联的原因（只有 trim 之后 peak/current 才拉开，
    growth 才有前瞻值）。
    """
    try:
        # 必须走 torch 原生口径。M2 安装的 aimdo_xpu_memory_stats 把 reserved
        # 与 allocated 双双映射成 Book A 当前值，经它算出的 peak/current 恒等，
        # growth 会恒为 0，L2 边界预回收从此拿不到任何前瞻量。
        from . import xpu as _xpu

        raw = _xpu.torch_reserved_stats(device) if _xpu is not None else None
        if raw is None:
            return 0
        reserved, _allocated, peak_reserved = raw
        # peak 低于 current 才是畸形：reset_peak_memory_stats() 把峰值清到了
        # 当前值以下，此时「历史峰值」这个前提失效，按 0 处理（不猜）。
        # 注意不能反过来判 growth > reserved —— L3 trim 成功之后 current
        # 骤降而 peak 保持高位，growth 远大于 reserved 是**正常且最重要**
        # 的信号（那正是刚归还给驱动、可以立刻预回收的量），误杀会让
        # L2 边界预回收永远拿不到值。
        if peak_reserved < reserved:
            return 0
        return peak_reserved - reserved
    except Exception:
        return 0


def reset_xpu_allocator_peak_stats(device=None):
    """重置峰值统计（转发 xpu.py，失败静默）。"""
    try:
        from . import xpu as _xpu
        _xpu.reset_xpu_allocator_peak_stats(device)
    except Exception:
        pass


def empty_xpu_allocator_cache(wait=False):
    """主动回收 XPU 侧缓存。Windows 下走 torch.xpu.empty_cache()。

    这是 L3 的对外入口：torch 的原生缓存只有 empty_cache() 能归还，
    而它不能在分配钩子里调用（torch 持有 allocator 锁）。
    """
    if lib is None or implementation != "xpu":
        return False
    try:
        from . import xpu as _xpu
        if not _xpu.is_ready():
            return False
    except Exception:
        return False
    try:
        import torch
        torch.xpu.empty_cache()
        return True
    except Exception:
        # 只有非 Windows 的 pluggable 模式才有 AIMDO 自己的块缓存可排空，
        # 且老 DLL 可能根本没导出这个入口 —— 两者都要降级而不是抛错。
        if not hasattr(lib, "xpu_allocator_empty_cache"):
            return False
        try:
            return bool(lib.xpu_allocator_empty_cache(bool(wait)))
        except Exception:
            return False


def publish_torch_cached_bytes(device, cached_bytes=None):
    """告诉 native 钩子 torch 自己缓存了多少显存。

    没有它，budget_deficit 看不到 L3：被缓存的块是 torch 释放了却没有
    还给驱动的，它根本不会走到 urUSMFree，所以从 native 侧完全不可见。

    这就是「torch.xpu.memory_stats 的 reserved - allocated」必须由 Python
    报上去的原因 —— native 侧没有任何钩子能自己推导出这个数。
    """
    if (
        lib is None
        or implementation != "xpu"
        or not hasattr(lib, "xpu_ur_hook_set_torch_cached_bytes")
    ):
        return None
    if cached_bytes is None:
        try:
            # 同上：经M2 包装器算出的差恒为 0，必须用 torch 原生口径兜底。
            from . import xpu as _xpu

            raw = _xpu.torch_reserved_stats(device) if _xpu is not None else None
            if raw is None:
                return None
            cached_bytes = max(0, raw[0] - raw[1])
        except Exception:
            return None
    try:
        cached_bytes = max(int(cached_bytes), 0)
        lib.xpu_ur_hook_set_torch_cached_bytes.argtypes = [
            ctypes.c_int,
            ctypes.c_uint64,
        ]
        lib.xpu_ur_hook_set_torch_cached_bytes.restype = None
        lib.xpu_ur_hook_set_torch_cached_bytes(int(device), cached_bytes)
    except Exception:
        return None
    return cached_bytes


def get_xpu_vmm_stats():
    """XPU VMM 统计（map/unmap、retire fence 等），用于诊断回收是否跟上。"""
    if lib is None or implementation != "xpu":
        return {}
    if not hasattr(lib, "xpu_get_vmm_stats"):
        return {}
    # 名称顺序必须与 src-xpu/dispatch.cpp 的 enum XpuStat 严格一致：
    # native 按下标填充，这里按下标命名，错位会让诊断结论完全反过来。
    names = (
        "virtual_reserve_calls",
        "virtual_reserve_bytes",
        "physical_create_calls",
        "physical_create_bytes",
        "map_calls",
        "map_bytes",
        "unmap_calls",
        "unmap_bytes",
        "physical_release_calls",
        "host_to_device_bytes",
        "queue_rebind_calls",
        "context_sync_calls",
        "context_sync_completions",
        "event_sync_calls",
        "event_sync_completions",
        "synchronous_host_to_device_calls",
        "synchronous_host_to_device_completions",
        "host_to_device_split_retries",
        "torch_allocator_alloc_calls",
        "torch_allocator_free_calls",
        "torch_allocator_cache_hits",
        "torch_allocator_physical_alloc_calls",
        "torch_allocator_physical_alloc_bytes",
        "torch_allocator_physical_release_calls",
        "torch_allocator_physical_release_bytes",
        "small_vbar_copy_fallback_calls",
        "small_vbar_copy_fallback_bytes",
        "small_vbar_copy_fallback_failures",
        "retire_token_calls",
        "retire_fence_submit_calls",
        "retire_fence_complete_calls",
        "retire_fence_submit_failures",
        "retire_force_polls",
        "retire_tracked_queues",
        "retire_queue_registration_failures",
        "retire_queue_identity_mismatches",
        "retire_fence_query_failures",
        "retire_shutdown_wait_failures",
    )
    values = (ctypes.c_uint64 * len(names))()
    try:
        if not lib.xpu_get_vmm_stats(values, len(names)):
            return {}
    except Exception:
        return {}
    return dict(zip(names, map(int, values)))


def get_xpu_ur_hook_stats():
    """UR-USM 钩子统计。钩子不可用时返回 {}（调用方一律按空字典处理）。"""
    if (
        lib is None
        or implementation != "xpu"
        or not hasattr(lib, "xpu_ur_hook_get_stats")
    ):
        return {}
    names = (
        "alloc_calls",
        "free_calls",
        "pass_through_alloc_calls",
        "tracked_alloc_calls",
        "tracked_alloc_bytes",
        "tracked_free_calls",
        "tracked_free_bytes",
        "synthetic_oom_calls",
        "runtime_oom_calls",
        "native_reclaim_free_calls",
        "native_reclaim_free_bytes",
        "retry_eviction_calls",
        "retry_eviction_bytes",
        "unknown_device_calls",
        "unknown_free_calls",
        "dropped_metadata_calls",
        "direct_pressure_calls",
        "direct_pressure_bytes",
        "duplicate_pointer_calls",
    )
    if platform.system() == "Windows":
        # Windows 还统计 expandable_segments 开启时 torch 走的入口，
        # 所以盲区是可观测的（老 DLL 由 native 侧额外补一个计数器）。
        names = names + ("physical_mem_create_calls", "cache_lever_skipped_calls")
    values = (ctypes.c_uint64 * len(names))()
    try:
        if not lib.xpu_ur_hook_get_stats(values, len(names)):
            return {}
        result = dict(zip(names, map(int, values)))
        # 老 DLL 没有这个独立计数器，缺失时保留 names 里的 0 值即可。
        if hasattr(lib, "xpu_ur_hook_get_cache_lever_skipped_calls"):
            lib.xpu_ur_hook_get_cache_lever_skipped_calls.argtypes = []
            lib.xpu_ur_hook_get_cache_lever_skipped_calls.restype = ctypes.c_uint64
            result["cache_lever_skipped_calls"] = int(
                lib.xpu_ur_hook_get_cache_lever_skipped_calls()
            )
    except Exception:
        return {}
    return result


def get_xpu_ur_hook_timing():
    """钩子自身的耗时统计（Windows），区分「钩子慢」与「钩子的后果慢」。"""
    if (
        lib is None
        or implementation != "xpu"
        or not hasattr(lib, "xpu_ur_hook_get_hook_timing")
    ):
        return {}
    calls = ctypes.c_uint64()
    nanoseconds = ctypes.c_uint64()
    hits = ctypes.c_uint64()
    try:
        lib.xpu_ur_hook_get_hook_timing.argtypes = [
            ctypes.POINTER(ctypes.c_uint64),
            ctypes.POINTER(ctypes.c_uint64),
        ]
        lib.xpu_ur_hook_get_hook_timing.restype = ctypes.c_bool
        if not lib.xpu_ur_hook_get_hook_timing(
            ctypes.byref(calls), ctypes.byref(nanoseconds)
        ):
            return {}
        result = {"hook_calls": int(calls.value), "hook_ns": int(nanoseconds.value)}
        # classify 计时是可选的，老 DLL 没有就不报，而不是让整个诊断失败。
        if hasattr(lib, "xpu_ur_hook_get_classify_timing"):
            lib.xpu_ur_hook_get_classify_timing.argtypes = [
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
                ctypes.POINTER(ctypes.c_uint64),
            ]
            lib.xpu_ur_hook_get_classify_timing.restype = ctypes.c_bool
            if lib.xpu_ur_hook_get_classify_timing(
                ctypes.byref(calls), ctypes.byref(nanoseconds), ctypes.byref(hits)
            ):
                result["classify_calls"] = int(calls.value)
                result["classify_ns"] = int(nanoseconds.value)
                result["classify_torch_hits"] = int(hits.value)
    except Exception:
        return {}
    return result


def get_xpu_memory_snapshot(
    device=None, include_native_segments=False, include_vbar_pages=True
):
    """抓取 native allocator 与 AIMDO 侧状态，不改变任何所有权。

    Windows 的 native_hook 模式刻意把激活/工作区块留在 torch 的 XPU
    缓存分配器里；VBAR 页是独立的外来分配域，所以两边并排上报，
    不合并成一个虚构的 native 块列表。
    """
    if implementation != "xpu":
        return {}
    try:
        import torch
    except Exception:
        return {}

    device_index = _xpu_device_index(device)
    try:
        owner = (
            "torch_xpu_native"
            if get_xpu_allocator_mode() == "native_hook"
            else "aimdo_xpu_pluggable"
        )
    except Exception:
        owner = "unknown"
    try:
        native_stats = {
            str(key): int(value)
            for key, value in torch.xpu.memory_stats(device_index).items()
        }
    except Exception as error:
        native_stats = {"snapshot_error": str(error)}
    native_segments = None
    if include_native_segments:
        try:
            native_segments = torch.xpu.memory_snapshot()
        except Exception as error:
            native_segments = {"snapshot_error": str(error)}
    try:
        from .model_vbar import vbars_snapshot

        vbars = vbars_snapshot(
            device_index, include_pages=include_vbar_pages
        )
    except Exception as error:
        vbars = [{"snapshot_error": str(error)}]
    result = {
        "timestamp": time.time(),
        "device": device_index,
        "allocator_owner": owner,
        "native_allocator": {"stats": native_stats},
        "aimdo": {
            "vmm": get_xpu_vmm_stats(),
            "ur_hook": get_xpu_ur_hook_stats(),
            "vbars": vbars,
        },
    }
    if include_native_segments:
        result["native_allocator"]["segments"] = native_segments
    return result


def capture_xpu_oom_snapshot(
    device=None, *, stage, request_bytes=None, error=None,
    include_native_segments=False,
):
    """记录一次 owner 边界的 OOM 快照。绝不能从分配钩子里调用。

    从钩子里取快照会让 Python 重入 native 分配器，破坏所有权。
    """
    device_index = _xpu_device_index(device)
    now = time.monotonic()
    previous_time = _xpu_oom_last_snapshot_monotonic.get(device_index)
    # 限流：一次真实的短缺往往连续触发很多次 fault，逐次抓快照会把诊断
    # 本身变成性能问题（每份快照都要读 torch 的完整 memory_stats）。
    if (
        _xpu_oom_history and previous_time is not None and
        now - previous_time < _XPU_OOM_SNAPSHOT_INTERVAL_SECONDS and
        _xpu_oom_history[-1].get("device") == device_index
    ):
        snapshot = _xpu_oom_history[-1]
        snapshot["timestamp"] = time.time()
        snapshot["oom"] = {
            "stage": str(stage),
            "request_bytes": (
                None if request_bytes is None else int(request_bytes)
            ),
            "error": None if error is None else str(error),
            "coalesced_events": int(
                snapshot.get("oom", {}).get("coalesced_events", 1)
            ) + 1,
        }
        return snapshot
    try:
        snapshot = get_xpu_memory_snapshot(
            device_index,
            include_native_segments=include_native_segments,
            include_vbar_pages=False,
        )
    except Exception:
        # 诊断失败绝不能影响调用方（fault 路径）的真实结果。
        return None
    if not snapshot:
        return snapshot
    snapshot["oom"] = {
        "stage": str(stage),
        "request_bytes": (
            None if request_bytes is None else int(request_bytes)
        ),
        "error": None if error is None else str(error),
        "coalesced_events": 1,
    }
    _xpu_oom_history.append(snapshot)
    del _xpu_oom_history[:-_XPU_OOM_HISTORY_LIMIT]
    _xpu_oom_last_snapshot_monotonic[device_index] = now
    return snapshot


def get_last_xpu_oom_snapshot(device=None):
    """返回最近一次 OOM 快照；device=None 时返回全局最后一条。"""
    if device is None:
        return _xpu_oom_history[-1] if _xpu_oom_history else None
    device_index = _xpu_device_index(device)
    for snapshot in reversed(_xpu_oom_history):
        if snapshot.get("device") == device_index:
            return snapshot
    return None
