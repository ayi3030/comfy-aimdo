"""Runtime contract test for the XPU backend (no GPU required).

Validates that the packaged provider.json declares a coherent XPU contract and
that the device-name -> silicon-family mapping is correct. This is the runnable
half of closing the "zero XPU tests" gap.
"""
import importlib.util
import pathlib

try:
    from comfy_aimdo import provider
except Exception:
    _p = pathlib.Path(__file__).resolve().parent.parent / "comfy_aimdo" / "provider.py"
    _spec = importlib.util.spec_from_file_location("comfy_aimdo_provider", _p)
    provider = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(provider)


def main() -> None:
    contract = provider.load_provider_contract()
    assert contract, "provider.json must load"
    rt = contract["runtime"]
    assert rt["torch_version"].endswith("+xpu"), rt["torch_version"]
    assert rt["sycl_abi"] == "sycl9", rt["sycl_abi"]
    targets = rt["xpu_targets"]
    assert isinstance(targets, list) and "bmg" in targets and "dg2" in targets, targets
    # name -> family mapping
    assert provider.map_xpu_target_name("Intel Arc B580 Graphics") == "bmg"
    assert provider.map_xpu_target_name("Intel Arc A770 Graphics") == "dg2"
    assert provider.map_xpu_target_name("Some Unknown GPU") is None
    assert provider.map_xpu_target_name(None) is None
    # get_xpu_targets round-trips the pin
    assert "bmg" in provider.get_xpu_targets()
    print("provider-contract-xpu test passed")


if __name__ == "__main__":
    main()
