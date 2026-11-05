"""Platforms: targets for the unified ``gc_`` interface."""

from .base import Platform
from .dolphin import DolphinPlatform
from .gamecube_hw import GameCubeHWPlatform

__all__ = ["Platform", "DolphinPlatform", "GameCubeHWPlatform"]


def resolve_platform(target: str, manager) -> Platform:
    """Resolve a ``target`` string to a concrete platform.

    Accepted target forms:

    * ``dolphin:<instance_id>`` - a running Dolphin instance.
    * ``gamecube-hw`` or ``gamecube-hw:<id>`` - real hardware (stub).

    If ``target`` is a bare instance id (no prefix), it is treated as a Dolphin
    instance id.
    """
    if target.startswith("dolphin:"):
        instance_id = target.split(":", 1)[1]
        return DolphinPlatform(manager.get(instance_id))
    if target.startswith("gamecube-hw"):
        hw_id = target.split(":", 1)[1] if ":" in target else "hw-001"
        return GameCubeHWPlatform(hw_id)
    # Bare instance id.
    return DolphinPlatform(manager.get(target))
