"""comfy-aimdo XPU runtime provider-contract loader.

The XPU runtime contract lives in ``provider.json`` (packaged via
``pyproject.toml`` ``package-data``). CI consumes it as a build-vs-declaration
gate. This module makes the contract *runtime-real*: the pinned ``xpu_targets``
(bmg/dg2) can now be read by the live backend to validate the actual silicon.

Intentionally torch-free so it imports and unit-tests on any machine (including
CI runners without an Intel GPU).
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Iterable

_LOGGER = logging.getLogger("comfy-aimdo")
_PROVIDER_JSON = "provider.json"


@lru_cache(maxsize=1)
def load_provider_contract() -> dict:
    """Load and parse ``provider.json`` shipped next to this module.

    Uses ``Path(__file__).parent`` so it resolves in BOTH layouts:
      * installed wheel (package-data ``*.json`` copies it into the package dir),
      * run-from-source dev tree (no ``pip install`` needed for tests/CI).

    Fail-soft: returns ``{}`` on any error so callers never take down init().
    """
    try:
        here = Path(__file__).resolve().parent
        raw = (here / _PROVIDER_JSON).read_text(encoding="utf-8")
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001 - fail-soft contract read
        _LOGGER.warning("comfy-aimdo: could not load provider.json: %r", exc)
        return {}


def get_xpu_targets() -> list:
    """Return the pinned XPU silicon targets (e.g. ``["bmg", "dg2"]``).

    Empty list when the contract is missing/unreadable.
    """
    runtime = load_provider_contract().get("runtime", {})
    targets = runtime.get("xpu_targets", [])
    return list(targets) if isinstance(targets, Iterable) else []


# Device-name -> XPU silicon family mapping. Pure string logic (torch-free).
# Battlemage = Arc B-series (e.g. B580); Alchemist = Arc A-series (e.g. A770).
_XPU_NAME_FAMILY = (
    (("b580", "b570", "battlemage", "arc b"), "bmg"),
    (("a770", "a750", "a580", "a380", "alchemist", "arc a", "dg2"), "dg2"),
)


def map_xpu_target_name(name: str | None) -> str | None:
    """Map an Intel XPU device name to a pinned family (``"bmg"``/``"dg2"``).

    Returns ``None`` for unrecognized / ``None`` names so callers can degrade
    gracefully instead of failing closed.
    """
    if not name:
        return None
    lowered = name.lower()
    for keys, family in _XPU_NAME_FAMILY:
        if any(k in lowered for k in keys):
            return family
    return None
