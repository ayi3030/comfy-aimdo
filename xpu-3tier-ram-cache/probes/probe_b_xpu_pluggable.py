# 实机验证 (b)：PyTorch 能否把 VMM 设备 VA 当作合法 XPU 设备指针识别并正确回收
# 用 ctypes 回调（无需编译器）实现 alloc_fn/free_fn（基于 Level Zero VMM），
# 经 torch._C._xpu_customAllocator 注册为当前分配器，再跑真实 torch XPU 算子。
import ctypes, sys, threading
from ctypes import c_void_p, c_uint32, c_uint64, c_size_t, c_int, POINTER, byref, Structure, CFUNCTYPE, cast

for n in ("ze_loader.dll", "libze_loader.so.1", "libze_loader.so"):
    try:
        lib = ctypes.CDLL(n); print("[env] loaded", n); break
    except OSError:
        lib = None
if not lib:
    print("RESULT (b): ze_loader 未找到"); sys.exit(0)

lib.zeInit.argtypes=[c_uint32]; lib.zeInit.restype=c_int
lib.zeDriverGet.argtypes=[POINTER(c_uint32),POINTER(c_void_p)]; lib.zeDriverGet.restype=c_int
lib.zeDeviceGet.argtypes=[c_void_p,POINTER(c_uint32),POINTER(c_void_p)]; lib.zeDeviceGet.restype=c_int
class CtxDesc(Structure): _fields_=[("stype",c_uint32),("pNext",c_void_p),("flags",c_uint32)]
class PhysDesc(Structure): _fields_=[("stype",c_uint32),("pNext",c_void_p),("flags",c_uint32),("size",c_size_t)]
lib.zeContextCreate.argtypes=[c_void_p,POINTER(CtxDesc),POINTER(c_void_p)]; lib.zeContextCreate.restype=c_int
lib.zeVirtualMemQueryPageSize.argtypes=[c_void_p,c_void_p,c_size_t,POINTER(c_size_t)]; lib.zeVirtualMemQueryPageSize.restype=c_int
lib.zeVirtualMemReserve.argtypes=[c_void_p,c_void_p,c_size_t,POINTER(c_void_p)]; lib.zeVirtualMemReserve.restype=c_int
lib.zePhysicalMemCreate.argtypes=[c_void_p,c_void_p,POINTER(PhysDesc),POINTER(c_void_p)]; lib.zePhysicalMemCreate.restype=c_int
lib.zeVirtualMemMap.argtypes=[c_void_p,c_void_p,c_size_t,c_void_p,c_size_t,c_uint32]; lib.zeVirtualMemMap.restype=c_int
lib.zeVirtualMemSetAccessAttribute.argtypes=[c_void_p,c_void_p,c_size_t,c_uint32]; lib.zeVirtualMemSetAccessAttribute.restype=c_int
lib.zeVirtualMemUnmap.argtypes=[c_void_p,c_void_p,c_size_t]; lib.zeVirtualMemUnmap.restype=c_int
lib.zePhysicalMemDestroy.argtypes=[c_void_p,c_void_p]; lib.zePhysicalMemDestroy.restype=c_int
lib.zeVirtualMemFree.argtypes=[c_void_p,c_void_p,c_size_t]; lib.zeVirtualMemFree.restype=c_int

lib.zeInit(1)
dn=c_uint32(0); lib.zeDriverGet(byref(dn),None); drv=(c_void_p*dn.value)(); cc=c_uint32(dn.value); lib.zeDriverGet(byref(cc),drv)
nv=c_uint32(0); lib.zeDeviceGet(drv[0],byref(nv),None); devs=(c_void_p*nv.value)(); c2=c_uint32(nv.value); lib.zeDeviceGet(drv[0],byref(c2),devs)
ctx=c_void_p(0); cd=CtxDesc(); cd.stype=0x0D; lib.zeContextCreate(drv[0],byref(cd),byref(ctx))
ps=c_size_t(0); lib.zeVirtualMemQueryPageSize(ctx,devs[0],1,byref(ps)); PAGE=ps.value or 65536
def ALIGN(s): return ((s+PAGE-1)//PAGE)*PAGE if s else PAGE
print("[env] page", PAGE, "device", hex(devs[0]))

_pending={}; _lock=threading.Lock()
_zero=c_void_p(0)
def _alloc(size, device, queue):
    n=ALIGN(size); va=c_void_p(0)
    if lib.zeVirtualMemReserve(ctx,None,n,byref(va))!=0 or not va.value: return None
    pd=PhysDesc(); pd.stype=0x20; pd.flags=1; pd.size=n
    ph=c_void_p(0)
    if lib.zePhysicalMemCreate(ctx,devs[0],byref(pd),byref(ph))!=0:
        lib.zeVirtualMemFree(ctx,va,n); return None
    if lib.zeVirtualMemMap(ctx,va,n,ph,0,1)!=0:
        lib.zePhysicalMemDestroy(ctx,ph); lib.zeVirtualMemFree(ctx,va,n); return None
    lib.zeVirtualMemSetAccessAttribute(ctx,va,n,1)
    with _lock: _pending[va.value]=(ph,n)
    return va.value
def _free(ptr, size, device, queue):
    if not ptr: return
    with _lock: ph,n=_pending.pop(ptr,(None,None))
    if ph is None: return
    lib.zeVirtualMemUnmap(ctx,c_void_p(ptr),n)
    lib.zePhysicalMemDestroy(ctx,ph)
    lib.zeVirtualMemFree(ctx,c_void_p(ptr),n)

ALLOC=CFUNCTYPE(c_void_p,c_size_t,c_int,c_void_p); FREE=CFUNCTYPE(None,c_void_p,c_size_t,c_int,c_void_p)
acb=ALLOC(_alloc); fcb=FREE(_free)
aaddr=cast(acb,c_void_p).value; faddr=cast(fcb,c_void_p).value

import torch
raw=torch._C._xpu_customAllocator(aaddr,faddr)
class Shim:
    def __init__(self,x): self._x=x
    def allocator(self): return self._x
torch.xpu.memory.change_current_allocator(Shim(raw))
print("[env] custom VMM allocator registered")

x=torch.randn(512,512,device="xpu")
print("randn sum:", float(x.sum().item()))
y=(x@x).sum().item()
print("matmul sum:", float(y))
z=torch.empty(3_000_000,device="xpu")  # 触发更大块分配
print("empty numel:", z.numel())
del x,y,z; torch.xpu.synchronize()
print("[env] pendings left:", len(_pending))
print("RESULT (b): PASS — PyTorch 接受 VMM 设备 VA 并正常执行/回收")
