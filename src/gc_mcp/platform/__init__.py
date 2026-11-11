"""Platforms: targets for the unified ``gc_`` interface."""

from ..errors import NotFoundError
from .base import Platform
from .dolphin import DolphinPlatform
from .gamecube_hw import GameCubeHWPlatform

__all__ = ["Platform", "DolphinPlatform", "GameCubeHWPlatform"]


def resolve_platform(target: str, manager, hardware=None) -> Platform:
    """Resolve a ``target`` string to a concrete platform.

    Accepted target forms:

    * ``dolphin:<instance_id>`` - a running Dolphin instance.
    * ``gamecube-hw`` or ``gamecube-hw:<id>`` - a registered real GameCube
      hardware instance.
    * A bare instance id - a Dolphin instance, falling back to a hardware
      instance of the same id.

    ``manager`` is the Dolphin :class:`InstanceManager`; ``hardware`` is the
    optional :class:`HardwareManager`.
    """
    if target.startswith("dolphin:"):
        instance_id = target.split(":", 1)[1]
        if manager is None:
            raise NotFoundError(f"unknown platform {target!r}")
        return DolphinPlatform(manager.get(instance_id))

    if target.startswith("gamecube-hw"):
        hw_id = target.split(":", 1)[1] if ":" in target else None
        if hardware is not None and hw_id is not None:
            inst = hardware.get_or_none(hw_id)
            if inst is not None:
                return GameCubeHWPlatform(inst)
        return GameCubeHWPlatform(hardware_id=hw_id)

    # Bare instance id: prefer a Dolphin instance, then a hardware instance.
    if manager is not None:
        dol = manager.get_or_none(target)
        if dol is not None:
            return DolphinPlatform(dol)
    if hardware is not None:
        hw = hardware.get_or_none(target)
        if hw is not None:
            return GameCubeHWPlatform(hw)
    if manager is not None:
        return DolphinPlatform(manager.get(target))
    raise NotFoundError(f"unknown platform {target!r}")
