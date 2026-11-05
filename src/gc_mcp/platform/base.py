"""Platform abstraction.

A *platform* is a target that can be controlled through the unified ``gc_``
interface. Two implementations exist:

* :class:`DolphinPlatform` — wraps a running Dolphin emulator instance.
* :class:`GameCubeHWPlatform` — a stub for real GameCube hardware (currently
  returns "unsupported" for every operation).

The point of this abstraction is that the ``gc_*`` tools do not need to know
whether they are talking to an emulator or to real hardware; they just call the
same methods on a platform. Later, a real hardware backend can be added without
changing the tool surface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Platform(ABC):
    """Abstract target that the ``gc_*`` tools control."""

    #: Machine-readable platform kind (e.g. "dolphin", "gamecube-hw").
    kind: str = "base"

    # ------------------------------------------------------------------
    @abstractmethod
    def describe(self) -> dict:
        """Return platform capabilities / metadata."""
        ...

    # ------------------------------------------------------------------
    # Controllers
    # ------------------------------------------------------------------
    @abstractmethod
    def controller_press(self, port: int, button: str) -> None:
        ...

    @abstractmethod
    def controller_release(self, port: int, button: str) -> None:
        ...

    @abstractmethod
    def controller_set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        ...

    @abstractmethod
    def controller_set_trigger(self, port: int, trigger: str, value: float) -> None:
        ...

    @abstractmethod
    def controller_sequence(self, port: int, steps: list[dict], loop: bool = False) -> dict:
        ...

    @abstractmethod
    def controller_sequence_cancel(self, sequence_id: str) -> bool:
        ...

    @abstractmethod
    def controller_get_state(self, port: int) -> dict:
        ...

    @abstractmethod
    def controller_reset(self, port: int) -> None:
        ...

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    @abstractmethod
    def capture_screenshot(self, dest_path: Path | str) -> Path:
        ...

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    @abstractmethod
    def memory_read(self, address: int, length: int) -> bytes:
        ...

    @abstractmethod
    def memory_write(self, address: int, data: bytes) -> None:
        ...
