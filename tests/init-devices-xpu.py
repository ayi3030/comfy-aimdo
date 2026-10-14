"""XPU backend init smoke test. Skips without an Intel XPU device.

Mirrors tests/init-devices-twice-cuda.py for the XPU path. Requires a real
Intel GPU (Arc B580 / dg2) to actually execute; on GPU-less runners it prints
SKIP and exits 0.
"""
import sys
import torch


def main() -> None:
    if not (hasattr(torch, "xpu") and torch.xpu.is_available()
            and torch.xpu.device_count() > 0):
        print("SKIP: no Intel XPU device available")
        return
    import comfy_aimdo.control as aimdo
    assert aimdo.init("xpu"), "comfy_aimdo.init('xpu') must succeed on XPU"
    device = torch.xpu.current_device()
    assert aimdo.init_device(device), "init_device must succeed"
    free, capacity = torch.xpu.mem_get_info(device)
    assert capacity > 0 and free <= capacity
    torch.xpu.synchronize()
    aimdo.deinit()
    print("init-devices-xpu test passed")


if __name__ == "__main__":
    main()
