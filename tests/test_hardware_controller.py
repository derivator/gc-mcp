"""Tests for the real-hardware serial controller backend."""

from __future__ import annotations

import sys
import time
import types

import pytest

from gc_mcp.controllers import pad_state as ps
from gc_mcp.controllers.hardware import (
    _ID_COMMAND,
    CENTER,
    H1,
    SYNC,
    TRIG_PRESSED,
    TRIG_RELEASED,
    HardwareController,
    _byte_to_stick,
    _byte_to_trigger,
    _open_serial,
    _PortState,
    _stick_to_byte,
    _trigger_to_byte,
)
from gc_mcp.errors import ControllerError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class FakeSerial:
    """Stand-in for ``serial.Serial`` that records every write."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.is_open = True

    def write(self, data) -> None:
        self.writes.append(bytes(data))

    def close(self) -> None:
        self.is_open = False


@pytest.fixture
def fake_serial(monkeypatch) -> FakeSerial:
    fake = FakeSerial()
    monkeypatch.setattr(
        "gc_mcp.controllers.hardware._open_serial",
        lambda device, baud, timeout: fake,
    )
    return fake


class VerifySerial:
    """Fake ``serial.Serial`` that answers the ``ID`` command like the Arduino.

    ``write(b"ID\\n")`` loads ``response`` into the read buffer; ``read`` drains
    it byte-by-byte (as ``_read_line`` does). Other writes (e.g. the neutral
    frame) are recorded but do not produce a reply.
    """

    def __init__(self, response: bytes = b"") -> None:
        self.writes: list[bytes] = []
        self.is_open = True
        self.closed = False
        self.response = bytes(response)
        self._buffer = bytearray()

    def write(self, data) -> None:
        data = bytes(data)
        self.writes.append(data)
        if data == _ID_COMMAND:
            self._buffer.extend(self.response)

    def read(self, n: int = 1) -> bytes:
        if not self._buffer:
            return b""
        out = bytes(self._buffer[:n])
        del self._buffer[:n]
        return out

    def reset_input_buffer(self) -> None:
        self._buffer.clear()

    def close(self) -> None:
        self.is_open = False
        self.closed = True


#: A valid firmware identify reply (see main.cpp).
_ID_LINE = b"GCCTRL:1 firmware=1.2 proto=3 report=8 frame=10 baud=115200\n"


def _ctrl_with_serials(
    monkeypatch,
    devices: list[str],
    responses: list[bytes] | None = None,
    **kw,
) -> tuple[HardwareController, list[VerifySerial]]:
    """Build a :class:`HardwareController` whose ``_open_serial`` returns pre-made
    :class:`VerifySerial` fakes, one per device.

    ``responses`` (optional) sets the ``ID`` reply each fake returns. Ports are
    opened in order, so the ``i``-th fake backs the ``i``-th device.
    """
    fakes = [VerifySerial() for _ in devices]
    if responses:
        for fake, resp in zip(fakes, responses):
            fake.response = resp
    counter = iter(fakes)

    def opener(device, baud, timeout):
        return next(counter)

    monkeypatch.setattr("gc_mcp.controllers.hardware._open_serial", opener)
    return HardwareController(devices, **kw), fakes


def _last_report(fake: FakeSerial) -> bytes | None:
    frames = [f for f in fake.writes if len(f) == 10 and f[0] == SYNC]
    return frames[-1][1:9] if frames else None


# ---------------------------------------------------------------------------
# Frame building
# ---------------------------------------------------------------------------
class TestFrameBuilding:
    def test_released_report(self) -> None:
        state = _PortState(port=0)
        assert HardwareController.build_report(state) == bytes(
            [0x00, 0x80, 0x80, 0x80, 0x80, 0x80, 0x1F, 0x1F]
        )

    def test_frame_structure_and_checksum(self) -> None:
        state = _PortState(port=0)
        frame = HardwareController.build_frame(state)
        assert frame[0] == SYNC
        assert len(frame) == 10
        assert frame[1:9] == bytes([0x00, 0x80, 0x80, 0x80, 0x80, 0x80, 0x1F, 0x1F])
        checksum = 0
        for b in frame[1:9]:
            checksum ^= b
        assert frame[9] == checksum

    def test_pressed_button_report(self) -> None:
        state = _PortState(port=0)
        state.buttons0 = 0x01  # A
        state.buttons1 = H1 | 0x10  # Z
        assert HardwareController.build_report(state)[0] == 0x01
        assert HardwareController.build_report(state)[1] == H1 | 0x10


# ---------------------------------------------------------------------------
# Byte conversion helpers
# ---------------------------------------------------------------------------
class TestByteConversions:
    def test_stick_encoding(self) -> None:
        assert _stick_to_byte(-1.0) == 0x00
        assert _stick_to_byte(0.0) == CENTER
        assert _stick_to_byte(1.0) == 0xFF

    def test_stick_decoding(self) -> None:
        assert _byte_to_stick(0x00) == -1.0
        assert _byte_to_stick(CENTER) == pytest.approx(0.0, abs=0.01)
        assert _byte_to_stick(0xFF) == 1.0

    def test_trigger_encoding(self) -> None:
        assert _trigger_to_byte(0.0) == TRIG_RELEASED
        assert _trigger_to_byte(1.0) == TRIG_PRESSED
        assert _trigger_to_byte(0.5) == 0x8F  # 31 + 0.5 * 224

    def test_trigger_decoding(self) -> None:
        assert _byte_to_trigger(TRIG_RELEASED) == 0.0
        assert _byte_to_trigger(TRIG_PRESSED) == 1.0
        assert _byte_to_trigger(0x8F) == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# Primitive state transitions
# ---------------------------------------------------------------------------
class TestPrimitives:
    def test_press_and_release_state(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            ctl.press(0, "A")
            assert "A" in ctl.get_state(0).buttons
            ctl.release(0, "A")
            assert "A" not in ctl.get_state(0).buttons
        finally:
            ctl.close()

    def test_all_buttons_map_to_correct_state(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            for button in ps.BUTTONS:
                ctl.press(0, button)
                assert button in ctl.get_state(0).buttons, button
                ctl.release(0, button)
                assert button not in ctl.get_state(0).buttons, button
        finally:
            ctl.close()

    def test_l_r_set_trigger_byte(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            ctl.press(0, "L")
            state = ctl.get_state(0)
            assert "L" in state.buttons
            assert state.l == pytest.approx(1.0)
            ctl.release(0, "L")
            state = ctl.get_state(0)
            assert "L" not in state.buttons
            assert state.l == pytest.approx(0.0)
        finally:
            ctl.close()

    def test_set_stick_round_trip(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            ctl.set_stick(0, "main", 1.0, -1.0)
            state = ctl.get_state(0)
            assert state.main[0] == pytest.approx(1.0)
            assert state.main[1] == pytest.approx(-1.0)
            ctl.set_stick(0, "c", 0.0, 0.0)
            state = ctl.get_state(0)
            assert state.c[0] == pytest.approx(0.0, abs=0.01)
            assert state.c[1] == pytest.approx(0.0, abs=0.01)
        finally:
            ctl.close()

    def test_set_trigger_round_trip(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            ctl.set_trigger(0, "R", 0.5)
            assert ctl.get_state(0).r == pytest.approx(0.5, abs=0.01)
            ctl.set_trigger(0, "L", 1.0)
            assert ctl.get_state(0).l == pytest.approx(1.0)
        finally:
            ctl.close()

    def test_reset_returns_to_default(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            ctl.press(0, "A")
            ctl.set_stick(0, "main", 1.0, 1.0)
            ctl.reset(0)
            state = ctl.get_state(0)
            assert state.buttons == set()
            assert state.main == pytest.approx((0.0, 0.0), abs=0.01)
            assert state.l == pytest.approx(0.0)
        finally:
            ctl.close()

    def test_get_state_unconfigured_port_is_default(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            state = ctl.get_state(1)
            assert state.port == 1
            assert state.buttons == set()
        finally:
            ctl.close()


# ---------------------------------------------------------------------------
# Repeater behaviour
# ---------------------------------------------------------------------------
class TestRepeater:
    def test_sends_frames_while_button_held(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            ctl.press(0, "A")
            time.sleep(0.05)
            frames = [f for f in fake_serial.writes if len(f) == 10 and f[0] == SYNC]
            assert frames, "repeater should have sent frames"
            # At least one frame must carry the A bit (buttons0 == 0x01).
            assert any(f[1] & 0x01 for f in frames)
        finally:
            ctl.close()

    def test_sends_released_frame_after_release(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            ctl.press(0, "A")
            time.sleep(0.02)
            ctl.release(0, "A")
            time.sleep(0.03)
            frames = [f for f in fake_serial.writes if len(f) == 10 and f[0] == SYNC]
            assert frames
            # The repeater must keep sending, now with A released.
            assert any(not (f[1] & 0x01) for f in frames)
        finally:
            ctl.close()

    def test_encodes_stick_into_report(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        try:
            ctl.set_stick(0, "main", 1.0, -1.0)
            time.sleep(0.03)
            report = _last_report(fake_serial)
            assert report is not None
            assert report[2] == 0xFF  # mainX
            assert report[3] == 0x00  # mainY
        finally:
            ctl.close()

    def test_close_stops_repeater(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        ctl.press(0, "A")
        time.sleep(0.02)
        ctl.close()
        fake_serial.writes.clear()
        time.sleep(0.03)
        assert fake_serial.writes == []

    def test_close_resets_buttons_to_neutral(self, fake_serial) -> None:
        """Closing the controller must write a neutral report before closing."""
        ctl = HardwareController(["/dev/ttyUSB0"], send_interval=0.001)
        ctl.press(0, "A")
        ctl.press(0, "Z")
        ctl.set_stick(0, "main", 1.0, 1.0)
        ctl.set_trigger(0, "L", 1.0)
        time.sleep(0.02)
        ctl.close()
        # The last report written before the device closed must be neutral.
        report = _last_report(fake_serial)
        assert report is not None
        assert report == bytes(
            [0x00, H1, CENTER, CENTER, CENTER, CENTER, TRIG_RELEASED, TRIG_RELEASED]
        )
        # The neutral frame is repeated to survive dropped bytes.
        neutral = [
            f for f in fake_serial.writes
            if len(f) == 10 and f[0] == SYNC and f[1:9] == report
        ]
        assert len(neutral) >= 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
class TestErrors:
    def test_press_without_serial_device_raises(self) -> None:
        ctl = HardwareController([])
        with pytest.raises(ControllerError) as exc:
            ctl.press(0, "A")
        assert "no serial device" in str(exc.value)

    def test_press_on_unconfigured_port_raises(self, fake_serial) -> None:
        ctl = HardwareController(["/dev/ttyUSB0"])
        with pytest.raises(ControllerError):
            ctl.press(2, "A")

    def test_open_serial_failure_raises_controller_error(self, monkeypatch) -> None:
        fake_mod = types.ModuleType("serial")

        class _SerialException(Exception):
            pass

        class _Serial:
            def __init__(self, *args, **kwargs):
                raise _SerialException("no such device")

        fake_mod.SerialException = _SerialException
        fake_mod.Serial = _Serial
        monkeypatch.setitem(sys.modules, "serial", fake_mod)
        with pytest.raises(ControllerError) as exc:
            _open_serial("/dev/ttyUSB0", 115200, 1.0)
        assert "cannot open serial device" in str(exc.value)


# ---------------------------------------------------------------------------
# Startup verification (ID probe)
# ---------------------------------------------------------------------------
class TestStartupVerification:
    def test_is_identify_line(self) -> None:
        assert HardwareController._is_identify_line(
            "GCCTRL:1 firmware=1.2 proto=3 report=8 frame=10 baud=115200"
        ) is True
        assert HardwareController._is_identify_line("garbage") is False
        assert HardwareController._is_identify_line("") is False

    def test_verify_reports_ok_for_responding_device(self, monkeypatch) -> None:
        ctl, fakes = _ctrl_with_serials(
            monkeypatch, ["/dev/ttyUSB0"], responses=[_ID_LINE], send_interval=0.001
        )
        try:
            reports = ctl.verify(timeout=0.5)
            assert len(reports) == 1
            assert reports[0]["ok"] is True
            assert reports[0]["id"].startswith("GCCTRL:")
            assert reports[0]["error"] is None
            # The ID command (not just a frame) was sent.
            assert _ID_COMMAND in fakes[0].writes
        finally:
            ctl.close()

    def test_verify_resets_buttons_to_neutral(self, monkeypatch) -> None:
        ctl, fakes = _ctrl_with_serials(
            monkeypatch, ["/dev/ttyUSB0"], responses=[_ID_LINE], send_interval=0.001
        )
        try:
            ctl.verify(timeout=0.5)
            neutral = ctl.build_frame(_PortState(port=0))
            # The neutral report is pushed repeatedly to survive dropped bytes.
            count = sum(1 for w in fakes[0].writes if w == neutral)
            assert count >= 3
        finally:
            ctl.close()

    def test_verify_reports_not_ok_for_silent_device(self, monkeypatch) -> None:
        ctl, _ = _ctrl_with_serials(
            monkeypatch, ["/dev/ttyUSB0"], responses=[b""], send_interval=0.001
        )
        try:
            reports = ctl.verify(timeout=0.2)
            assert reports[0]["ok"] is False
            assert reports[0]["error"] is None
        finally:
            ctl.close()

    def test_verify_reports_error_for_unopenable_device(self, monkeypatch) -> None:
        def boom(device, baud, timeout):
            raise ControllerError(f"cannot open serial device {device!r}")

        monkeypatch.setattr("gc_mcp.controllers.hardware._open_serial", boom)
        ctl = HardwareController(["/dev/ttyUSB0"])
        try:
            reports = ctl.verify(timeout=0.2)
            assert reports[0]["ok"] is False
            assert reports[0]["error"] is not None
            assert "cannot open" in reports[0]["error"]
        finally:
            ctl.close()

    def test_verify_starts_repeater(self, monkeypatch) -> None:
        ctl, fakes = _ctrl_with_serials(
            monkeypatch, ["/dev/ttyUSB0"], responses=[_ID_LINE], send_interval=0.001
        )
        try:
            ctl.verify(timeout=0.5)
            assert ctl._thread is not None and ctl._thread.is_alive()
            # The repeater keeps writing frames to the open device.
            time.sleep(0.03)
            assert any(len(w) == 10 and w[0] == SYNC for w in fakes[0].writes)
        finally:
            ctl.close()
