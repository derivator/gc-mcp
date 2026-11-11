"""Hardware controller backend (serial devices) — not implemented yet.

A real GameCube's controller ports will eventually be driven over the serial
devices configured on a :class:`HardwareInstance` (up to four, one per port).
Until the serial protocol is defined, the primitive actions below raise
:class:`UnsupportedError`.

The ``press_once`` / ``stick_flick`` / ``sequence`` machinery is inherited from
:class:`ControllerBackend`, so once the primitives are implemented the high-level
timed-input API works for real hardware for free.
"""

from __future__ import annotations

from ..errors import UnsupportedError
from . import pad_state as ps
from .base import ControllerBackend


class HardwareController(ControllerBackend):
    """Stub controller backend for a real GameCube (raises ``UnsupportedError``).

    The serial protocol for driving GameCube controller ports is not defined yet,
    so every primitive raises. This exists so the unified ``gc_*`` controller
    tools can be wired to real hardware without changing the platform surface.
    """

    def press(self, port: int, button: str) -> None:
        raise UnsupportedError(
            f"hardware controller press is not implemented yet (port={port}, "
            f"button={button!r})",
            details={"port": port, "button": button,
                     "note": "The serial controller protocol is not defined yet."},
        )

    def release(self, port: int, button: str) -> None:
        raise UnsupportedError(
            f"hardware controller release is not implemented yet (port={port}, "
            f"button={button!r})",
            details={"port": port, "button": button,
                     "note": "The serial controller protocol is not defined yet."},
        )

    def set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        raise UnsupportedError(
            f"hardware controller stick is not implemented yet (port={port}, "
            f"stick={stick!r})",
            details={"port": port, "stick": stick, "x": x, "y": y,
                     "note": "The serial controller protocol is not defined yet."},
        )

    def set_trigger(self, port: int, trigger: str, value: float) -> None:
        raise UnsupportedError(
            f"hardware controller trigger is not implemented yet (port={port}, "
            f"trigger={trigger!r})",
            details={"port": port, "trigger": trigger, "value": value,
                     "note": "The serial controller protocol is not defined yet."},
        )

    def get_state(self, port: int) -> ps.PadState:
        raise UnsupportedError(
            f"hardware controller state is not implemented yet (port={port})",
            details={"port": port,
                     "note": "The serial controller protocol is not defined yet."},
        )

    def reset(self, port: int) -> None:
        raise UnsupportedError(
            f"hardware controller reset is not implemented yet (port={port})",
            details={"port": port,
                     "note": "The serial controller protocol is not defined yet."},
        )
