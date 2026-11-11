"""Real GameCube hardware platform.

This wraps a registered :class:`HardwareInstance` (a physical console) behind
the same unified ``gc_`` interface that :class:`DolphinPlatform` uses for an
emulator, so the server tools do not need to know which kind of target they are
driving.

Implemented so far:

* **Screen capture** via the ``v4l2`` capture backend.
* **ROM loading** via Swiss's netboot listener (a ``BOOT`` command over TCP 4242
  on the console's ``bba_ip``).

Controller, memory, log and emulator-control operations raise
:class:`UnsupportedError` with a clear message until their hardware backends are
built out.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import UnsupportedError
from ..instances.hardware_instance import HardwareInstance
from .base import Platform


class GameCubeHWPlatform(Platform):
    """Wraps a :class:`HardwareInstance` as a ``gc_`` platform."""

    kind = "gamecube-hw"

    def __init__(self, instance: HardwareInstance | None = None,
                 hardware_id: str | None = None) -> None:
        self.instance = instance
        self.hardware_id = (
            instance.spec.instance_id if instance is not None else (hardware_id or "hw-001")
        )

    def _unsupported(self, op: str) -> None:
        raise UnsupportedError(
            f"operation {op!r} is not implemented for real GameCube hardware yet",
            details={
                "platform": self.kind,
                "instance_id": self.hardware_id,
                "note": "Only screen capture is currently implemented for real "
                "GameCube hardware.",
            },
        )

    # ------------------------------------------------------------------
    def describe(self) -> dict:
        if self.instance is None:
            return {
                "kind": self.kind,
                "hardware_id": self.hardware_id,
                "registered": False,
                "note": "No real GameCube hardware instance is registered with "
                "this id. Use gc_hardware_register to create one.",
            }
        status = self.instance.status()
        status["registered"] = True
        return status

    # ------------------------------------------------------------------
    # Controllers (serial devices - a stub until the protocol is defined)
    # ------------------------------------------------------------------
    def _controller(self):
        if self.instance is None:
            self._unsupported("controller")
        return self.instance.controller

    def controller_press(self, port: int, button: str) -> None:
        self._controller().press(port, button)

    def controller_release(self, port: int, button: str) -> None:
        self._controller().release(port, button)

    def controller_press_once(self, port: int, button: str, hold_duration: float = 200.0) -> None:
        self._controller().press_once(port, button, hold_duration)

    def controller_flick(self, port: int, stick: str, x: float, y: float,
                         hold_duration: float = 200.0) -> None:
        self._controller().stick_flick(port, stick, x, y, hold_duration)

    def controller_set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        self._controller().set_stick(port, stick, x, y)

    def controller_set_trigger(self, port: int, trigger: str, value: float) -> None:
        self._controller().set_trigger(port, trigger, value)

    def controller_sequence(self, port: int, steps: list[dict], loop: bool = False) -> dict:
        return self._controller().sequence(port, steps, loop=loop)

    def controller_sequence_cancel(self, sequence_id: str) -> bool:
        return self._controller().sequence_cancel(sequence_id)

    def controller_get_state(self, port: int) -> dict:
        return self._controller().get_state(port).to_dict()

    def controller_reset(self, port: int) -> None:
        self._controller().reset(port)

    # ------------------------------------------------------------------
    # Logging (no emulator log for real hardware)
    # ------------------------------------------------------------------
    def read_log(self, tail: int | None = None, grep: str | None = None) -> dict:
        self._unsupported("read_log")

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------
    def capture_screenshot(self, dest_path: Path | str) -> Path:
        if self.instance is None:
            self._unsupported("capture_screenshot")
        return self.instance.capture_screenshot(Path(dest_path))

    def capture_configure(self, **opts: object) -> dict:
        if self.instance is None:
            self._unsupported("capture_configure")
        return self.instance.capture_configure(**opts)

    # ------------------------------------------------------------------
    # ROM loading (Swiss netboot)
    # ------------------------------------------------------------------
    def rom_load(self, rom_path: str) -> dict:
        """Boot a ROM on the console via Swiss's netboot listener.

        ``rom_path`` must be a console-side path (``sdc:/`` or ``fsp:/``).
        Requires the instance to have a ``bba_ip`` configured.
        """
        if self.instance is None:
            self._unsupported("rom_load")
        return self.instance.rom_load(rom_path)

    # ------------------------------------------------------------------
    # Memory (no emulated RAM for real hardware)
    # ------------------------------------------------------------------
    def memory_read(self, address: int, length: int) -> bytes:
        self._unsupported("memory_read")

    def memory_write(self, address: int, data: bytes) -> None:
        self._unsupported("memory_write")

    # ------------------------------------------------------------------
    # Emulator control (pause / step / save / load) - not applicable
    # ------------------------------------------------------------------
    def emulator_toggle_pause(self) -> dict:
        self._unsupported("emulator_toggle_pause")

    def emulator_step_frame(self) -> dict:
        self._unsupported("emulator_step_frame")

    def emulator_save_state(self, slot: int, copy_dir: Path | None = None) -> dict:
        self._unsupported("emulator_save_state")

    def emulator_load_state(self, slot: int) -> dict:
        self._unsupported("emulator_load_state")

    def emulator_freeze(self) -> dict:
        self._unsupported("emulator_freeze")

    def emulator_unfreeze(self) -> dict:
        self._unsupported("emulator_unfreeze")
