# AI Model Dynamic Offloader — Intel Arc (XPU) fork

This project is a pytorch VRAM allocator that implements on-demand offloading of model weights when the primary pytorch VRAM allocator comes under pressure.

> ### 🍴 This is a fork of [Comfy-Org/comfy-aimdo](https://github.com/Comfy-Org/comfy-aimdo)
>
> It keeps the upstream CUDA/ROCm dynamic-offload engine intact and **adds an Intel Arc (XPU) backend** via oneAPI / Level-Zero / SYCL (`torch 2.14.0+xpu`), together with root-cause fixes for the cold-start OOM that XPU users hit on ComfyUI.
>
> **What this fork adds on top of upstream:**
> * **Intel Arc XPU backend** (Battlemage **B580** / **DG2**) through Level-Zero / SYCL — `src-xpu/` builds `aimdo_xpu.dll`; on Windows the backend runs in `native_hook` mode.
> * **Cold-start OOM root-cause fixes** (verified 12/12 end-to-end on a real Intel Arc B580 with the MiniMax H3 workflow):
>   * **Real-free-VRAM admission gate** — `get_free_memory()`'s XPU branch now reads true device free via `torch.xpu.mem_get_info()` instead of the torch-blind `device_total − torch_reserved` (which over-reported free VRAM by ~7 GiB, let the offloader keep too many weights resident, and triggered caching-allocator fragmentation OOM). On by default; disable with `AIMDO_XPU_REAL_FREE_VRAM=0`.
>   * **USM-fragmentation recovery hook + admission FRESH free-read** — removes transient-dip false OOMs.
> * **3-tier VRAM/RAM/disk offload on XPU** — the RAM middle tier is enabled (upstream gated it to Nvidia/AMD only); tune the RAM-cache budget with `AIMDO_XPU_RAM_CACHE_GB`. Files live in `deliver/comfy/`.
> * **Pinned runtime contract** — `comfy_aimdo/provider.json` records the exact tested stack (`torch 2.14.0+xpu`, `sycl_abi=sycl9`, `oneapi 2026.1`, `xpu_targets=[bmg, dg2]`), enforced by a CI contract gate.
>
> Enable the backend with `AIMDO_XPU_ENABLED=1` (set by the launch script).

## Support:

* **Nvidia GPUs** (CUDA), **AMD GPUs** (ROCm/HIP), **and Intel Arc GPUs** (XPU / oneAPI / Level-Zero) — *XPU added by this fork*
* **PyTorch 2.8+** (upstream) / **PyTorch 2.14.0+xpu** (Intel Arc, this fork)
* **CUDA 12.8+** (Nvidia) / **ROCm 7+** (AMD) / **oneAPI 2026.1 + SYCL (sycl9)** (Intel Arc)
* **Windows 11+** / **Linux** as per python ManyLinux support

---

## How it works:

* The pytorch application creates a Virtual Base Address Register (**VBAR**) for a model. Creating a VBAR doesn't cost any VRAM, only GPU virtual address space (which is pretty much free).
* The pytorch application allocates tensors for model weights within the VBAR. These tensors are initially un-allocated and will segfault if touched.
* The pytorch application faults in the tensors using the `fault()` API at the time the tensor is needed. This is where VRAM actually gets allocated.

##### If the `fault()` is successful (sufficient VRAM for this tensor):
1.  **If the fault() resultant signature is changed or unknown:**
    * The application uses `tensor::_copy()` to populate the weight data on the GPU.
    * The application saves the returned signature against this weight for future comparison
2.  The layer uses the weight tensor.
3.  The application calls `unpin()` on the tensor to allow it to be freed under pressure later if needed.

##### If the `fault()` is unsuccessful (offloaded weight):
1.  The application allocates a temporary regular GPU tensor.
2.  Uses `_copy` to populate weight data on the GPU.
3.  The layer uses the temporary as the weight.
4.  Pytorch garbage collects the temp when the layer is finished.

see examples/example.py

---

## Priorities:

* The most recent VBARs are the highest priority and lower addresses in the VBAR take priority over higher addresses.
* Applications should order their tensor allocations in the VBAR in load-priority order with the lowest addresses for the highest priority weights.
* Calling `fault()` on a weight that is higher priority than other weights will cause those lower priority weights to get freed to make space.
* Having a weight evicted sets that VBAR's watermark to that weight's level. Any weights in the same VBAR above the watermark automatically fail the `fault()` API. This avoids constantly faulting in all weights each model iteration while allowing the application to just blindly call `fault()` every layer and check the results. There is no need for the application to manage any VRAM quotas or watermarks.
* Existing VBARs can be pushed to top priority with the `prioritize()` API. This allows use of an already loaded or partially model (e.g. using the same model twice in a complex workflow). Using `prioritize` resets the offload watermark of that model to no offloading, giving its weights priority over any other currently loaded models.

---

## Backend:

* VBAR allocation is done with `cuMemAddressReserve()`, faulting with `cuMemCreate()` and `cuMemMap()` and all frees done with appropriate converse APIs.
* For consistency with VBAR memory management, main pytorch allocator plugin is also implemented with `cuMemAddressReserve` -> `cuMemCreate` -> `cuMemMap`. This also behaves a lot better on Windows systems with System Memory fallback.

On AMD, the equivalent HIP APIs (`hipMemAddressReserve` -> `hipMemCreate` -> `hipMemMap`, and their converse calls) are used throughout via the same flow.

On **Intel Arc (XPU)**, this fork routes the RAM-layer host buffer and the torch XPU caching-allocator arbitration through Level-Zero / SYCL (`src-xpu/`, `aimdo_xpu.dll`). The XPU path is gated behind `AIMDO_XPU_ENABLED=1`; the real-free-VRAM admission fix is on by default.

## Caveats:

* There is no real way for this allocator to tell the difference between high usage and bad fragmentation in the pytorch caching allocator. As we always return success to the pytorch caching allocator it experiences no pressure while weights are being offloaded which means it can run in an extremely fragmented mode. The assumption is model weight access patterns are reasonably regular over blocks or iterations and it finds a good set of sizes to cache. What you should generally do though, is completely flush the pytorch caching allocator before each new model run, which avoids completely un-used reservations from taking priority over the next models weights.

* **Intel Arc (XPU) specific:** page-locked host memory (`zeMemAllocHost`) was evaluated and is **not** enabled by default — a real-hardware trial reverted it (a 64 MiB pageable→device copy's `OUT_OF_DEVICE_MEMORY` worsened to `DEVICE_LOST`). The XPU path instead uses pageable staging with an 8 MiB split-retry fallback for large host→device copies.
