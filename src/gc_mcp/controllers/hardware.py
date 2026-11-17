"""Real GameCube hardware controller backend (serial Arduino).

A real GameCube's controller ports are driven by a serial device (an Arduino
running the GC-controller emulation) for each port, up to four. Each port
``N`` maps to ``serial_devices[N]`` on the :class:`HardwareInstance`.

The wire protocol is defined by the Arduino firmware (see
``~/gc-controller-arduino/main.cpp``). A single *report* is 8 bytes:

    [buttons0, buttons1, mainX, mainY, cX, cY, left, right]

and is wrapped in a 10-byte frame: a sync byte ``0xA5``, the 8-byte report,
then an XOR checksum over the report. ``buttons0`` holds the face/Start bits
(A/B/X/Y/Start), ``buttons1`` holds the shoulder/D-pad/Z bits (D-Left/D-Right/
D-Down/D-Up/Z/R/L) plus the always-set ``high1`` bit. Analog sticks are centred
at ``0x80``; triggers are ``0x1F`` released and ``0xFF`` fully pressed.

Because the Arduino can drop serial bytes while it is busy servicing the
console's poll (the GC-controller library disables interrupts during the
GameCube transaction), a frame must be sent *repeatedly* while a button is
held. :class:`HardwareController` therefore runs a background repeater thread
that continuously writes the current report for every open port; the primitive
actions just update the report and return immediately, so they never block the
:class:`SequenceRunner` timing.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from ..errors import ControllerError
from . import pad_state as ps
from .base import ControllerBackend

#: Frame sync byte (see ``gc_button_test.py``).
SYNC = 0xA5
#: Serial baud rate for the Arduino.
BAUD = 115200
#: The 'high1' bit (bit 7 of buttons1) is always set in a normal report.
H1 = 0x80
#: Centre value for the analog sticks / C-stick.
CENTER = 0x80
#: Analog trigger released / fully-pressed values.
TRIG_RELEASED = 0x1F
TRIG_PRESSED = 0xFF

#: (buttons0 bit, buttons1 bit) for each API button token. ``buttons0`` bits
#: (LSB first) are A/B/X/Y/Start; ``buttons1`` bits (LSB first) are D-Left/
#: D-Right/D-Down/D-Up/Z/R/L.
_BUTTON_BITS: dict[str, tuple[int, int]] = {
    "A": (0x01, 0x00),
    "B": (0x02, 0x00),
    "X": (0x04, 0x00),
    "Y": (0x08, 0x00),
    "START": (0x10, 0x00),
    "D_LEFT": (0x00, 0x01),
    "D_RIGHT": (0x00, 0x02),
    "D_DOWN": (0x00, 0x04),
    "D_UP": (0x00, 0x08),
    "Z": (0x00, 0x10),
    "R": (0x00, 0x20),
    "L": (0x00, 0x40),
}

#: Default interval (seconds) between repeater writes to each open port.
DEFAULT_SEND_INTERVAL = 0.01

#: How many times the neutral report is written to each serial device on
#: shutdown. The Arduino can drop bytes while it is busy servicing a poll, so
#: the release frame is repeated to be sure the console sees it before the
#: device is closed.
_NEUTRAL_RETRIES = 3

#: The text command that asks the Arduino firmware to identify itself.
_ID_COMMAND = b"ID\n"
#: The prefix of the identify line the firmware replies with (see main.cpp).
_IDENTIFY_PREFIX = "GCCTRL:"
#: Extra fields that must be present for a reply to count as a valid identify.
_IDENTIFY_FIELDS = ("firmware=", "proto=", "report=", "frame=", "baud=")
#: How many times the ``ID`` command is re-sent before giving up on a device.
#: The Arduino can drop serial bytes while it is busy servicing a console poll,
#: so a single exchange can be lost.
_ID_ATTEMPTS = 2


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


def _stick_to_byte(value: float) -> int:
    """Convert an API stick axis (-1..1, centre 0) to a byte (0x00..0xFF)."""
    return round((_clamp(value, -1.0, 1.0) + 1.0) / 2.0 * 255.0)


def _byte_to_stick(byte: int) -> float:
    """Convert a stick byte (0x00..0xFF) back to an API axis (-1..1)."""
    return _clamp((byte / 255.0) * 2.0 - 1.0, -1.0, 1.0)


def _trigger_to_byte(value: float) -> int:
    """Convert an API trigger (0..1) to a byte (0x1F released .. 0xFF pressed)."""
    return round(TRIG_RELEASED + _clamp(value, 0.0, 1.0) * (TRIG_PRESSED - TRIG_RELEASED))


def _byte_to_trigger(byte: int) -> float:
    """Convert a trigger byte (0x1F..0xFF) back to an API trigger (0..1)."""
    return _clamp((byte - TRIG_RELEASED) / (TRIG_PRESSED - TRIG_RELEASED), 0.0, 1.0)


@dataclass
class _PortState:
    """The 8-byte report state for one controller port."""

    port: int
    buttons0: int = 0x00
    buttons1: int = H1
    main_x: int = CENTER
    main_y: int = CENTER
    c_x: int = CENTER
    c_y: int = CENTER
    left: int = TRIG_RELEASED
    right: int = TRIG_RELEASED


def _open_serial(device: str, baud: int, timeout: float):
    """Open a serial device, raising :class:`ControllerError` on failure."""
    try:
        import serial
    except ImportError as exc:  # pragma: no cover - depends on install
        raise ControllerError(
            "pyserial is not installed; install it with 'pip install pyserial' "
            "to drive a real GameCube's controller ports",
            details={"device": device},
        ) from exc
    try:
        return serial.Serial(device, baud, timeout=timeout)
    except Exception as exc:
        raise ControllerError(
            f"cannot open serial device {device!r}: {exc}",
            details={"device": device},
        ) from exc


class HardwareController(ControllerBackend):
    """Serial Arduino controller backend for a real GameCube.

    ``serial_devices`` is the list of serial device paths, one per controller
    port (port ``N`` uses ``serial_devices[N]``). Ports are opened lazily on
    first use; a background repeater thread then writes the current report to
    every open port at ``send_interval`` so that held buttons are seen by the
    console even though the Arduino can drop bytes during a poll.
    """

    def __init__(self, serial_devices: list[str] | None = None, *,
                 baud: int = BAUD, send_interval: float = DEFAULT_SEND_INTERVAL,
                 open_timeout: float = 1.0) -> None:
        super().__init__()
        self.serial_devices = [str(d) for d in (serial_devices or [])]
        self.baud = baud
        self.send_interval = send_interval
        self.open_timeout = open_timeout
        self._states: dict[int, _PortState] = {}
        self._serials: dict[int, object] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        for port in range(len(self.serial_devices)):
            self._states[port] = _PortState(port=port)

    # ------------------------------------------------------------------
    # Serial plumbing
    # ------------------------------------------------------------------
    def _device_for_port(self, port: int) -> str:
        if port >= len(self.serial_devices) or not self.serial_devices[port]:
            raise ControllerError(
                f"hardware instance has no serial device configured for "
                f"controller port {port}",
                details={"port": port, "serial_devices": list(self.serial_devices)},
            )
        return self.serial_devices[port]

    def _open_device(self, port: int) -> object:
        """Open and register the serial device for ``port`` (idempotent).

        Unlike :meth:`_ensure_open` this does *not* start the repeater, so a
        caller can exchange text commands (e.g. the ``ID`` probe) before any
        binary frames are written on the wire. The caller must hold
        :attr:`_lock`.
        """
        ser = self._serials.get(port)
        if ser is not None and getattr(ser, "is_open", True):
            return ser
        device = self._device_for_port(port)
        if ser is not None:
            try:
                ser.close()  # type: ignore[attr-defined]
            except OSError:
                pass
            self._serials.pop(port, None)
        ser = _open_serial(device, self.baud, self.open_timeout)
        self._serials[port] = ser
        self._states.setdefault(port, _PortState(port=port))
        return ser

    def _drop_device(self, port: int) -> None:
        """Close and forget the serial device for ``port``. Caller holds :attr:`_lock`."""
        ser = self._serials.pop(port, None)
        if ser is not None:
            try:
                ser.close()  # type: ignore[attr-defined]
            except OSError:
                pass

    def _ensure_open(self, port: int) -> object:
        """Open the serial device for ``port`` (idempotent) and start the repeater."""
        port = ps.validate_port(port)
        with self._lock:
            ser = self._open_device(port)
            self._start_repeater()
            return ser

    # ------------------------------------------------------------------
    # Startup verification (ID probe)
    # ------------------------------------------------------------------
    @staticmethod
    def _is_identify_line(line: str) -> bool:
        """Whether ``line`` is a valid ``ID`` reply (``GCCTRL:...``).

        The firmware replies with e.g.
        ``GCCTRL:1 firmware=1.2 proto=3 report=8 frame=10 baud=115200``.
        """
        return line.startswith(_IDENTIFY_PREFIX) and all(
            f in line for f in _IDENTIFY_FIELDS
        )

    def _read_line(self, ser: object, timeout: float) -> str:
        """Read one line-terminated reply from ``ser``, waiting up to ``timeout``.

        The firmware's identify reply is a single ``\\n``-terminated line. Reads
        byte-by-byte until the newline or the deadline; the serial read timeout
        means an idle device returns empty reads, so we keep polling until the
        overall deadline rather than treating a single empty read as done.
        """
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            try:
                b = ser.read(1)  # type: ignore[attr-defined]
            except (OSError, ValueError):
                break
            if not b:
                continue
            if b == b"\n":
                break
            buf += b
        return buf.decode("ascii", "replace").strip()

    def _identify(self, ser: object, timeout: float) -> str:
        """Send the ``ID`` command and return the device's identify line.

        Retries a few times because the Arduino can drop serial bytes while it
        is busy servicing a console poll, so a single exchange can be lost.
        Returns the last non-empty line (possibly ``""`` if nothing answered).
        """
        last = ""
        for _ in range(_ID_ATTEMPTS):
            try:
                ser.reset_input_buffer()  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass
            try:
                ser.write(_ID_COMMAND)  # type: ignore[attr-defined]
            except (OSError, ValueError):
                break
            line = self._read_line(ser, timeout)
            if line:
                last = line
                if self._is_identify_line(line):
                    break
        return last

    def verify(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """Probe every configured serial device with the firmware ``ID`` command.

        Used on startup to confirm each connected Arduino actually responds (so a
        dead or wrong device is caught early), then to push a neutral report so
        no button/stick is left held. The repeater is started for any device
        that opened successfully, so the controller is ready for use.

        Returns a per-port report: ``{"port", "device", "ok", "id", "error"}``.
        ``ok`` is True only when the device answered with a valid ``GCCTRL:``
        identify line. A device that fails to open or answer is *reported* (not
        raised) so one dead port does not stop the others being probed.
        """
        timeout = float(timeout) if timeout is not None else self.open_timeout
        results: list[dict[str, Any]] = []
        with self._lock:
            for port in range(len(self.serial_devices)):
                device = self.serial_devices[port]
                entry: dict[str, Any] = {
                    "port": port,
                    "device": device,
                    "ok": False,
                    "id": None,
                    "error": None,
                }
                ser: object | None = None
                try:
                    ser = self._open_device(port)
                    id_line = self._identify(ser, timeout)
                    entry["id"] = id_line
                    entry["ok"] = self._is_identify_line(id_line)
                except Exception as exc:  # noqa: BLE001 - per-port probe must never abort the whole startup sweep
                    entry["error"] = str(exc)
                    self._drop_device(port)
                results.append(entry)
                # Reset the buttons to be sure nothing is left held, for any
                # device that opened (even if the ID probe did not answer). This
                # is best-effort: if the device went away the repeater drops it.
                if ser is not None and entry["error"] is None:
                    neutral = self.build_frame(_PortState(port=port))
                    try:
                        for _ in range(_NEUTRAL_RETRIES):
                            ser.write(neutral)  # type: ignore[attr-defined]
                    except (OSError, ValueError):
                        self._drop_device(port)
            self._start_repeater()
        return results

    # ------------------------------------------------------------------
    # Frame building
    # ------------------------------------------------------------------
    @staticmethod
    def build_report(state: _PortState) -> bytes:
        """Build the 8-byte report for a port state."""
        return bytes([
            state.buttons0,
            state.buttons1,
            state.main_x,
            state.main_y,
            state.c_x,
            state.c_y,
            state.left,
            state.right,
        ])

    @classmethod
    def build_frame(cls, state: _PortState) -> bytes:
        """Build a full 10-byte frame: sync + 8-byte report + XOR checksum."""
        report = cls.build_report(state)
        checksum = 0
        for b in report:
            checksum ^= b
        return bytes([SYNC]) + report + bytes([checksum])

    # ------------------------------------------------------------------
    # Repeater thread
    # ------------------------------------------------------------------
    def _start_repeater(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._repeater_loop, name="gc-hw-controller", daemon=True
        )
        self._thread.start()

    def _repeater_loop(self) -> None:
        while not self._stop.is_set():
            targets: list[tuple[object, bytes]] = []
            with self._lock:
                for port, ser in list(self._serials.items()):
                    if not getattr(ser, "is_open", True):
                        continue
                    state = self._states.get(port)
                    if state is None:
                        continue
                    targets.append((ser, self.build_frame(state)))
            for ser, frame in targets:
                try:
                    ser.write(frame)  # type: ignore[attr-defined]
                except (OSError, ValueError):
                    # The device went away; drop it so a later primitive reopens.
                    with self._lock:
                        for p, s in list(self._serials.items()):
                            if s is ser:
                                try:
                                    s.close()  # type: ignore[attr-defined]
                                except OSError:
                                    pass
                                self._serials.pop(p, None)
                                break
            self._stop.wait(self.send_interval)

    # ------------------------------------------------------------------
    # Primitive actions
    # ------------------------------------------------------------------
    def press(self, port: int, button: str) -> None:
        port = ps.validate_port(port)
        token = ps.validate_button(button)
        self._ensure_open(port)
        b0_bit, b1_bit = _BUTTON_BITS[token]
        with self._lock:
            state = self._states[port]
            state.buttons0 |= b0_bit
            state.buttons1 |= b1_bit
            if token == "L":
                state.left = TRIG_PRESSED
            elif token == "R":
                state.right = TRIG_PRESSED

    def release(self, port: int, button: str) -> None:
        port = ps.validate_port(port)
        token = ps.validate_button(button)
        b0_bit, b1_bit = _BUTTON_BITS[token]
        with self._lock:
            state = self._states.get(port)
            if state is None:
                return
            state.buttons0 &= ~b0_bit
            state.buttons1 &= ~b1_bit
            if token == "L":
                state.left = TRIG_RELEASED
            elif token == "R":
                state.right = TRIG_RELEASED

    def set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        port = ps.validate_port(port)
        stick = ps.validate_stick(stick)
        self._ensure_open(port)
        bx, by = _stick_to_byte(x), _stick_to_byte(y)
        with self._lock:
            state = self._states[port]
            if stick == "main":
                state.main_x, state.main_y = bx, by
            else:
                state.c_x, state.c_y = bx, by

    def set_trigger(self, port: int, trigger: str, value: float) -> None:
        port = ps.validate_port(port)
        trigger = ps.validate_trigger(trigger)
        self._ensure_open(port)
        byte = _trigger_to_byte(value)
        with self._lock:
            state = self._states[port]
            if trigger == "L":
                state.left = byte
            else:
                state.right = byte

    def get_state(self, port: int) -> ps.PadState:
        port = ps.validate_port(port)
        with self._lock:
            state = self._states.get(port)
            if state is None:
                return ps.PadState(port=port)
            buttons: set[str] = set()
            for token, (b0, b1) in _BUTTON_BITS.items():
                if (b0 and state.buttons0 & b0) or (b1 and state.buttons1 & b1):
                    buttons.add(token)
            return ps.PadState(
                port=port,
                buttons=buttons,
                main=(_byte_to_stick(state.main_x), _byte_to_stick(state.main_y)),
                c=(_byte_to_stick(state.c_x), _byte_to_stick(state.c_y)),
                l=_byte_to_trigger(state.left),
                r=_byte_to_trigger(state.right),
            )

    def reset(self, port: int) -> None:
        port = ps.validate_port(port)
        self._ensure_open(port)
        with self._lock:
            self._states[port] = _PortState(port=port)

    def close(self) -> None:
        super().close()
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        # Reset every port to a neutral report and push it out before closing so
        # the console isn't left with a held button/stick after the server exits.
        # The Arduino can drop bytes during a poll, so repeat the neutral frame a
        # few times to be safe.
        with self._lock:
            for port in self._states:
                self._states[port] = _PortState(port=port)
            for port, ser in list(self._serials.items()):
                if not getattr(ser, "is_open", True):
                    continue
                frame = self.build_frame(self._states[port])
                for _ in range(_NEUTRAL_RETRIES):
                    try:
                        ser.write(frame)  # type: ignore[attr-defined]
                    except (OSError, ValueError):
                        break
                try:
                    ser.close()  # type: ignore[attr-defined]
                except OSError:
                    pass
            self._serials.clear()
