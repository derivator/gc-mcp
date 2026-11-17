"""Dolphin platform: a running emulator instance exposed via the unified API."""

from __future__ import annotations

from pathlib import Path

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

    def controller_press_once(self, port: int, button: str, hold_duration: float = 200.0) -> None:
        self.instance.controller.press_once(port, button, hold_duration)

    def controller_set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        self.instance.controller.set_stick(port, stick, x, y)

    def controller_set_trigger(self, port: int, trigger: str, value: float) -> None:
        self.instance.controller.set_trigger(port, trigger, value)

    def controller_flick(self, port: int, stick: str, x: float, y: float,
                         hold_duration: float = 200.0) -> None:
        self.instance.controller.stick_flick(port, stick, x, y, hold_duration)

    def controller_sequence(self, port: int, steps: list[dict], loop: bool = False) -> dict:
        return self.instance.controller.sequence(port, steps, loop=loop)

    def controller_sequence_cancel(self, sequence_id: str) -> bool:
        return self.instance.controller.sequence_cancel(sequence_id)

    def controller_get_state(self, port: int) -> dict:
        return self.instance.controller.get_state(port).to_dict()

    def controller_reset(self, port: int) -> None:
        self.instance.controller.reset(port)

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def read_log(self, tail: int | None = None, grep: str | None = None) -> dict:
        return self.instance.read_log(tail=tail, grep=grep)

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def capture_screenshot(self, dest_path: Path | str) -> Path:
        return self.instance.capture.capture(Path(dest_path))

    def capture_configure(self, **opts: object) -> dict:
        return self.instance.capture.configure(**opts)

    # ------------------------------------------------------------------
    # ROM loading
    # ------------------------------------------------------------------
    def rom_load(self, rom_path: str) -> dict:
        """Stop this instance and relaunch it booting the given host ROM path."""
        return self.instance.relaunch(rom_path)

    # ------------------------------------------------------------------
    # Memory
    # ------------------------------------------------------------------
    def memory_read(self, address: int, length: int) -> bytes:
        return self.instance.memory.read(address, length)

    def memory_write(self, address: int, data: bytes) -> None:
        self.instance.memory.write(address, data)

    # ------------------------------------------------------------------
    # Emulator control (pause / step / save / load)
    # ------------------------------------------------------------------
    def emulator_toggle_pause(self) -> dict:
        return self.instance.emulator_toggle_pause()

    def emulator_step_frame(self) -> dict:
        return self.instance.emulator_step_frame()

    def emulator_save_state(self, slot: int, copy_dir: Path | None = None) -> dict:
        return self.instance.emulator_save_state(slot, copy_dir)

    def emulator_load_state(self, slot: int) -> dict:
        return self.instance.emulator_load_state(slot)

    def emulator_freeze(self) -> dict:
        return self.instance.emulator_freeze()

    def emulator_unfreeze(self) -> dict:
        return self.instance.emulator_unfreeze()
