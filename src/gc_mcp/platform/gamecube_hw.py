"""Real GameCube hardware platform (stub).

This is a placeholder that exists so the unified ``gc_`` interface can later be
pointed at actual hardware without changing the tool surface. Every operation
currently raises :class:`UnsupportedError`.

The class is intentionally minimal: it implements the same interface as
:class:`DolphinPlatform` but reports that hardware control is not yet
implemented.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import UnsupportedError
from .base import Platform


class GameCubeHWPlatform(Platform):
    """Stub for real GameCube hardware. All operations are unsupported."""

    kind = "gamecube-hw"

    def __init__(self, hardware_id: str = "hw-001") -> None:
        self.hardware_id = hardware_id

    def _unsupported(self, op: str):
        raise UnsupportedError(
            f"operation {op!r} is not supported for real GameCube hardware "
            "(hardware backend not implemented)",
            details={"platform": self.kind, "hardware_id": self.hardware_id},
        )

    # ------------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "kind": self.kind,
            "hardware_id": self.hardware_id,
            "supported": False,
            "note": "Real GameCube hardware control is not implemented yet.",
        }

    def controller_press(self, port: int, button: str) -> None:
        self._unsupported("controller_press")

    def controller_release(self, port: int, button: str) -> None:
        self._unsupported("controller_release")

    def controller_press_once(self, port: int, button: str, hold_duration: float = 200.0) -> None:
        self._unsupported("controller_press_once")

    def controller_set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        self._unsupported("controller_set_stick")

    def controller_set_trigger(self, port: int, trigger: str, value: float) -> None:
        self._unsupported("controller_set_trigger")

    def controller_sequence(self, port: int, steps: list[dict], loop: bool = False) -> dict:
        self._unsupported("controller_sequence")

    def controller_sequence_cancel(self, sequence_id: str) -> bool:
        self._unsupported("controller_sequence_cancel")

    def controller_get_state(self, port: int) -> dict:
        self._unsupported("controller_get_state")

    def controller_reset(self, port: int) -> None:
        self._unsupported("controller_reset")

    def capture_screenshot(self, dest_path: Path | str) -> Path:
        self._unsupported("capture_screenshot")

    def memory_read(self, address: int, length: int) -> bytes:
        self._unsupported("memory_read")

    def memory_write(self, address: int, data: bytes) -> None:
        self._unsupported("memory_write")
