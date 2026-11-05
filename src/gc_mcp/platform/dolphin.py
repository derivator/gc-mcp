"""Dolphin platform: a running emulator instance exposed via the unified API."""

from __future__ import annotations

from pathlib import Path

from ..controllers import pad_state as ps
from ..instances.dolphin_instance import DolphinInstance
from .base import Platform


class DolphinPlatform(Platform):
    """Wraps a running :class:`DolphinInstance` as a ``gc_`` platform."""

    kind = "dolphin"

    def __init__(self, instance: DolphinInstance) -> None:
        self.instance = instance

    # ------------------------------------------------------------------
    def describe(self) -> dict:
        return {
            "kind": self.kind,
            "instance_id": self.instance.spec.instance_id,
            "rom": str(self.instance.spec.rom_path),
            "controller_ports": list(self.instance.spec.controller_ports),
            "bba_mode": self.instance.spec.bba_mode,
            "running": self.instance.running,
            "pid": self.instance.pid,
        }

    # ------------------------------------------------------------------
    # Controllers
    # ------------------------------------------------------------------
    def controller_press(self, port: int, button: str) -> None:
        self.instance.controller.press(port, button)

    def controller_release(self, port: int, button: str) -> None:
        self.instance.controller.release(port, button)

    def controller_set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        self.instance.controller.set_stick(port, stick, x, y)

    def controller_set_trigger(self, port: int, trigger: str, value: float) -> None:
        self.instance.controller.set_trigger(port, trigger, value)

    def controller_sequence(self, port: int, steps: list[dict], loop: bool = False) -> dict:
        return self.instance.controller.sequence(port, steps, loop=loop)

    def controller_sequence_cancel(self, sequence_id: str) -> bool:
        return self.instance.controller.sequence_cancel(sequence_id)

    def controller_get_state(self, port: int) -> dict:
        return self.instance.controller.get_state(port).to_dict()

    def controller_reset(self, port: int) -> None:
        self.instance.controller.reset(port)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def capture_screenshot(self, dest_path: Path | str) -> Path:
        return self.instance.capture.capture(Path(dest_path))

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    def memory_read(self, address: int, length: int) -> bytes:
        return self.instance.memory.read(address, length)

    def memory_write(self, address: int, data: bytes) -> None:
        self.instance.memory.write(address, data)
