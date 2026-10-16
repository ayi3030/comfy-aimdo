"""torch 原生口径读取失败的「原因 -> 排查方向」文案与限流告警。

从 control.py 与 model_vbar.py 抽出来的共享叶子模块。这两处原本各抄了一份
（以及各自的 _warn_once），纯粹是因为 control 与 model_vbar 互相 import 会成环。
本模块不 import 这两个模块中的任何一个，所以两边都能安全 import，消除重复与
两份文案随维护而漂移的隐患。
"""

import logging

_LOG = logging.getLogger("comfy-aimdo.xpu")

# 失败原因 -> 排查方向。三种原因若混成一句告警，真机排查会被引向错误方向
# （最常见的是把「torch 侧异常」误当成「M2 包装器没装」）。
_TORCH_STATS_REASON_TEXT = {
    "not_installed": (
        "M2 包装器未安装（_install_native_hook_wrappers 没跑过），"
        "拿不到包装前的 torch 原生函数"
    ),
    "call_failed": (
        "torch 原生 memory_stats 调用抛异常，请检查 XPU 上下文/驱动是否可用"
    ),
    "empty_stats": (
        "torch 原生 memory_stats 返回空读数，torch 侧未给出可用统计"
    ),
}

_warned = set()


def warn_once(reason, consequence):
    """拿不到 torch 原生口径时限流告警一次（进程内按 (reason, consequence) 去重）。

    返回 True，便于调用方直接 `if warn_once(...): return`。两条调用方原本各自的
    _warn_torch_stats_unavailable / _warn_torch_stats_once 现已统一到此。
    """
    detail = _TORCH_STATS_REASON_TEXT.get(reason, "未知原因(%r)" % (reason,))
    key = (reason, consequence)
    if key in _warned:
        return True
    _warned.add(key)
    _LOG.warning(
        "comfy-aimdo XPU: 无法读取 torch 原生预留统计（%s），%s。"
        "若未主动设置 AIMDO_XPU_NATIVE_CACHE_TRIM=0，请排查上述原因。",
        detail, consequence,
    )
    return True
