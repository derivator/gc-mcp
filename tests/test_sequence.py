"""Tests for the timed input-sequence engine and step parsing."""

from __future__ import annotations

import time

import pytest

from gc_mcp.controllers.base import (
    ControllerBackend,
    InputStep,
    SequenceRunner,
    parse_step,
)
from gc_mcp.controllers.pad_state import PadState
from gc_mcp.errors import SequenceError


class TapBackend(ControllerBackend):
    """Minimal backend that records press/release for press_once tests."""

    def __init__(self) -> None:
        super().__init__()
        self.pressed: list[tuple[int, str]] = []
        self.released: list[tuple[int, str]] = []

    def press(self, port: int, button: str) -> None:
        self.pressed.append((port, button))

    def release(self, port: int, button: str) -> None:
        self.released.append((port, button))

    def set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        pass

    def set_trigger(self, port: int, trigger: str, value: float) -> None:
        pass

    def get_state(self, port: int) -> PadState:
        return PadState(port=port)

    def reset(self, port: int) -> None:
        pass


class FakeBackend:
    """Records applied steps; ignores sequence machinery."""

    def __init__(self) -> None:
        self.applied: list[InputStep] = []

    def apply_step(self, port: int, step: InputStep) -> None:
        self.applied.append(step)


class StickBackend(ControllerBackend):
    """Minimal backend that records set_stick calls for stick_flick tests."""

    def __init__(self) -> None:
        super().__init__()
        self.sticks: list[tuple[int, str, float, float]] = []

    def press(self, port: int, button: str) -> None:
        pass

    def release(self, port: int, button: str) -> None:
        pass

    def set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        self.sticks.append((port, stick, x, y))

    def set_trigger(self, port: int, trigger: str, value: float) -> None:
        pass

    def get_state(self, port: int) -> PadState:
        return PadState(port=port)

    def reset(self, port: int) -> None:
        pass


class TestParseStep:
    def test_press(self) -> None:
        step = parse_step({"t": 100, "type": "press", "button": "a"})
        assert step.t == 100
        assert step.type == "press"
        assert step.button == "A"

    def test_release(self) -> None:
        step = parse_step({"t": 50, "type": "release", "button": "START"})
        assert step.button == "START"

    def test_stick(self) -> None:
        step = parse_step({"t": 0, "type": "stick", "stick": "main", "x": 1.0, "y": -1.0})
        assert step.stick == "main"
        assert step.x == 1.0
        assert step.y == -1.0

    def test_trigger(self) -> None:
        step = parse_step({"t": 0, "type": "trigger", "trigger": "l", "value": 0.5})
        assert step.trigger == "L"
        assert step.value == pytest.approx(0.5)

    def test_wait(self) -> None:
        step = parse_step({"t": 200, "type": "wait"})
        assert step.type == "wait"

    def test_reset(self) -> None:
        step = parse_step({"t": 0, "type": "reset"})
        assert step.type == "reset"

    def test_invalid_type(self) -> None:
        with pytest.raises(SequenceError):
            parse_step({"t": 0, "type": "bogus"})

    def test_not_a_dict(self) -> None:
        with pytest.raises(SequenceError):
            parse_step("not a dict")


class TestSequenceRunner:
    def test_runs_steps_in_time_order(self) -> None:
        backend = FakeBackend()
        steps = [
            parse_step({"t": 20, "type": "press", "button": "A"}),
            parse_step({"t": 0, "type": "press", "button": "B"}),
        ]
        runner = SequenceRunner(backend, port=0, steps=steps)
        runner.start()
        # Wait for the runner to finish.
        for _ in range(100):
            if not runner.running:
                break
            time.sleep(0.01)
        assert [s.button for s in backend.applied] == ["B", "A"]

    def test_sorts_by_time(self) -> None:
        steps = [
            parse_step({"t": 100, "type": "press", "button": "A"}),
            parse_step({"t": 10, "type": "press", "button": "B"}),
        ]
        runner = SequenceRunner(FakeBackend(), port=0, steps=steps)
        assert [s.button for s in runner.steps] == ["B", "A"]

    def test_cancel_stops(self) -> None:
        backend = FakeBackend()
        steps = [parse_step({"t": 0, "type": "wait"}), parse_step({"t": 10_000, "type": "press", "button": "A"})]
        runner = SequenceRunner(backend, port=0, steps=steps)
        runner.start()
        runner.cancel()
        for _ in range(100):
            if not runner.running:
                break
            time.sleep(0.01)
        assert not runner.running

    def test_running_flag(self) -> None:
        # Use a step far in the future so the thread stays alive.
        runner = SequenceRunner(FakeBackend(), port=0,
                                steps=[parse_step({"t": 10_000, "type": "wait"})])
        assert runner.running is False
        runner.start()
        assert runner.running is True
        runner.cancel()


class TestPressOnce:
    def test_press_then_release_in_order(self) -> None:
        backend = TapBackend()
        backend.press_once(0, "START", hold_duration_ms=10)
        assert backend.pressed == [(0, "START")]
        assert backend.released == [(0, "START")]

    def test_default_hold_duration_is_200(self) -> None:
        import inspect

        sig = inspect.signature(ControllerBackend.press_once)
        assert sig.parameters["hold_duration_ms"].default == 200.0

    def test_release_even_if_interrupted_during_hold(self, monkeypatch) -> None:
        backend = TapBackend()

        def boom(_delay: float) -> None:
            raise KeyboardInterrupt

        # Make time.sleep raise mid-hold; the real implementation's try/finally
        # must still release the button.
        monkeypatch.setattr("gc_mcp.controllers.base.time.sleep", boom)
        with pytest.raises(KeyboardInterrupt):
            backend.press_once(0, "A", hold_duration_ms=10)
        assert backend.pressed == [(0, "A")]
        assert backend.released == [(0, "A")]

    def test_negative_hold_duration_raises(self) -> None:
        backend = TapBackend()
        with pytest.raises(ValueError):
            backend.press_once(0, "A", hold_duration_ms=-1)
        # No press/release should have been issued.
        assert backend.pressed == []
        assert backend.released == []


class TestStickFlick:
    def test_push_then_center_in_order(self) -> None:
        backend = StickBackend()
        backend.stick_flick(0, "main", 0.0, 1.0, hold_duration_ms=10)
        assert backend.sticks == [(0, "main", 0.0, 1.0), (0, "main", 0.0, 0.0)]

    def test_default_hold_duration_is_200(self) -> None:
        import inspect

        sig = inspect.signature(ControllerBackend.stick_flick)
        assert sig.parameters["hold_duration_ms"].default == 200.0

    def test_center_even_if_interrupted_during_hold(self, monkeypatch) -> None:
        backend = StickBackend()

        def boom(_delay: float) -> None:
            raise KeyboardInterrupt

        # Make time.sleep raise mid-hold; the real implementation's try/finally
        # must still return the stick to centre.
        monkeypatch.setattr("gc_mcp.controllers.base.time.sleep", boom)
        with pytest.raises(KeyboardInterrupt):
            backend.stick_flick(0, "c", 1.0, -1.0, hold_duration_ms=10)
        assert backend.sticks == [(0, "c", 1.0, -1.0), (0, "c", 0.0, 0.0)]

    def test_negative_hold_duration_raises(self) -> None:
        backend = StickBackend()
        with pytest.raises(ValueError):
            backend.stick_flick(0, "main", 1.0, 0.0, hold_duration_ms=-1)
        # No stick move should have been issued.
        assert backend.sticks == []
