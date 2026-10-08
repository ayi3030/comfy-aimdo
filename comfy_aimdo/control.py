import os
import atexit
import ctypes
import platform
import struct
import sys
from pathlib import Path
import logging
import importlib.util

lib = None
devctxs = []
_log_callback = None

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
