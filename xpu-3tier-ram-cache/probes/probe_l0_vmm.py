# 实机验证 (a)：Intel Arc B580 上 Level Zero 虚拟内存管理(VMM)可用性探测
# 纯 ctypes + dlsym，不 include 任何 SDK 头；逐字对照 oneapi-src/level-zero ze_api.h 的签名。
import ctypes, sys, glob, os, site
from ctypes import c_void_p, c_uint32, c_uint64, c_size_t, c_int, POINTER, byref, Structure

try:
    import torch
    ok = torch.xpu.is_available()
    print("[env] torch", torch.__version__, "| xpu_available", ok,
          "| dev", (torch.xpu.get_device_name(0) if ok else "n/a"))
except Exception as e:
    print("[env] torch import failed:", e)

def load_lib():
    for n in ("ze_loader.dll", "libze_loader.so.1", "libze_loader.so"):
        try:
            return ctypes.CDLL(n), n
        except OSError:
            pass
    for sp in (site.getsitepackages() + [site.getusersitepackages()]):
        for pat in ("**/ze_loader.dll", "**/libze_loader.so*"):
            hits = glob.glob(os.path.join(sp, pat), recursive=True)
            if hits:
                return ctypes.CDLL(hits[0]), hits[0]
    return None, None

lib, name = load_lib()
if not lib:
    print("RESULT: ze_loader 未找到 -> (a) 无法执行（需 oneAPI Level Zero runtime）")
    sys.exit(0)
print("[env] loaded", name)

ZE_SUCCESS = 0
lib.zeInit.argtypes = [c_uint32]; lib.zeInit.restype = c_int
lib.zeDriverGet.argtypes = [POINTER(c_uint32), POINTER(c_void_p)]; lib.zeDriverGet.restype = c_int
lib.zeDeviceGet.argtypes = [c_void_p, POINTER(c_uint32), POINTER(c_void_p)]; lib.zeDeviceGet.restype = c_int

class CtxDesc(Structure):
    _fields_ = [("stype", c_uint32), ("pNext", c_void_p), ("flags", c_uint32)]
class PhysDesc(Structure):
    _fields_ = [("stype", c_uint32), ("pNext", c_void_p), ("flags", c_uint32), ("size", c_size_t)]
class MemProps(Structure):
    _fields_ = [("stype", c_uint32), ("pNext", c_void_p), ("flags", c_uint32),
                ("maxClockRate", c_uint32), ("maxBusWidth", c_uint32),
                ("totalSize", c_uint64), ("name", ctypes.c_char * 256)]
class MemAllocProps(Structure):  # ze_memory_allocation_properties_t
    _fields_ = [("stype", c_uint32), ("pNext", c_void_p), ("type", c_uint32),
                ("id", c_uint64), ("pageSize", c_uint64)]

lib.zeContextCreate.argtypes = [c_void_p, POINTER(CtxDesc), POINTER(c_void_p)]; lib.zeContextCreate.restype = c_int
lib.zeVirtualMemQueryPageSize.argtypes = [c_void_p, c_void_p, c_size_t, POINTER(c_size_t)]; lib.zeVirtualMemQueryPageSize.restype = c_int
lib.zeVirtualMemReserve.argtypes = [c_void_p, c_void_p, c_size_t, POINTER(c_void_p)]; lib.zeVirtualMemReserve.restype = c_int
lib.zePhysicalMemCreate.argtypes = [c_void_p, c_void_p, POINTER(PhysDesc), POINTER(c_void_p)]; lib.zePhysicalMemCreate.restype = c_int
lib.zeVirtualMemMap.argtypes = [c_void_p, c_void_p, c_size_t, c_void_p, c_size_t, c_uint32]; lib.zeVirtualMemMap.restype = c_int
lib.zeVirtualMemSetAccessAttribute.argtypes = [c_void_p, c_void_p, c_size_t, c_uint32]; lib.zeVirtualMemSetAccessAttribute.restype = c_int
lib.zeVirtualMemUnmap.argtypes = [c_void_p, c_void_p, c_size_t]; lib.zeVirtualMemUnmap.restype = c_int
lib.zePhysicalMemDestroy.argtypes = [c_void_p, c_void_p]; lib.zePhysicalMemDestroy.restype = c_int
lib.zeVirtualMemFree.argtypes = [c_void_p, c_void_p, c_size_t]; lib.zeVirtualMemFree.restype = c_int
lib.zeDeviceGetMemoryProperties.argtypes = [c_void_p, POINTER(c_uint32), POINTER(MemProps)]; lib.zeDeviceGetMemoryProperties.restype = c_int
lib.zeMemGetAllocProperties.argtypes = [c_void_p, c_void_p, POINTER(MemAllocProps), POINTER(c_void_p)]; lib.zeMemGetAllocProperties.restype = c_int

print("zeInit:", lib.zeInit(1))  # ZE_INIT_FLAG_GPU_ONLY = ZE_BIT(0) = 1
dcnt = c_uint32(0); lib.zeDriverGet(byref(dcnt), None); print("drivers:", dcnt.value)
if dcnt.value == 0:
    print("RESULT (a): 无 Level Zero driver"); sys.exit(0)
drv = (c_void_p * dcnt.value)(); c = c_uint32(dcnt.value); lib.zeDriverGet(byref(c), drv)
ncnt = c_uint32(0); lib.zeDeviceGet(drv[0], byref(ncnt), None); print("devices:", ncnt.value)
if ncnt.value == 0:
    print("RESULT (a): 无设备"); sys.exit(0)
devs = (c_void_p * ncnt.value)(); c2 = c_uint32(ncnt.value); lib.zeDeviceGet(drv[0], byref(c2), devs)

mp = MemProps(); mp.stype = 0x07; mc = c_uint32(1)
if lib.zeDeviceGetMemoryProperties(devs[0], byref(mc), byref(mp)) == ZE_SUCCESS:
    print("device totalSize = %.2f GiB (name=%r)" % (mp.totalSize / (1 << 30), mp.name.decode(errors='replace')))

ctx = c_void_p(0); cd = CtxDesc(); cd.stype = 0x0D
rc = lib.zeContextCreate(drv[0], byref(cd), byref(ctx))
print("zeContextCreate:", rc, "ctx=", hex(ctx.value or 0))
if rc != ZE_SUCCESS:
    print("RESULT (a): zeContextCreate 失败 -> 不可用"); sys.exit(0)

ps = c_size_t(0); lib.zeVirtualMemQueryPageSize(ctx, devs[0], 1, byref(ps))
print("pageSize:", ps.value, "=", (ps.value / 1024), "KiB")
size = (64 << 20)
if ps.value:
    size = ((size + ps.value - 1) // ps.value) * ps.value

va = c_void_p(0)
r_res = lib.zeVirtualMemReserve(ctx, None, size, byref(va))
print("zeVirtualMemReserve:", r_res, "va=", hex(va.value or 0), "size=", size)
if r_res != ZE_SUCCESS or not va.value:
    print("RESULT (a): zeVirtualMemReserve 失败 -> VMM 不可用，需降级"); sys.exit(0)

pd = PhysDesc(); pd.stype = 0x20; pd.flags = 1; pd.size = size
ph = c_void_p(0)
r_phys = lib.zePhysicalMemCreate(ctx, devs[0], byref(pd), byref(ph))
print("zePhysicalMemCreate:", r_phys, "phys=", hex(ph.value or 0))
if r_phys == ZE_SUCCESS:
    rm = lib.zeVirtualMemMap(ctx, va, size, ph, 0, 1)
    print("zeVirtualMemMap:", rm)
    if rm == ZE_SUCCESS:
        # (b) 代理检查：PyTorch 会用 zeMemGetAllocProperties 判定指针类型；
        # 若 VMM VA 被判为 DEVICE(2) 且能定位到设备，则 PyTorch 接受它的可能性很高。
        ap = MemAllocProps(); ap.stype = 0x17; dph = c_void_p(0)
        ra = lib.zeMemGetAllocProperties(ctx, va, byref(ap), byref(dph))
        print("zeMemGetAllocProperties(VMM VA):", ra, "type=", ap.type,
              "(0=UNKNOWN,1=HOST,2=DEVICE,3=SHARED) pageSize=", ap.pageSize,
              "dev_match=", (dph.value == devs[0]) if devs[0] else "?")
    print("zeVirtualMemSetAccessAttribute:", lib.zeVirtualMemSetAccessAttribute(ctx, va, size, 1))
    print("zeVirtualMemUnmap:", lib.zeVirtualMemUnmap(ctx, va, size))
    print("zePhysicalMemDestroy:", lib.zePhysicalMemDestroy(ctx, ph))
print("zeVirtualMemFree:", lib.zeVirtualMemFree(ctx, va, size))

verdict = (r_res == ZE_SUCCESS and r_phys == ZE_SUCCESS)
print("RESULT (a):", "PASS — B580 上 Level Zero VMM 可用，可真移植" if verdict
      else "PARTIAL/FAIL — 见上（reserve/phys 未全成功）")
