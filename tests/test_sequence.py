"""Tests for the timed input-sequence engine and step parsing."""

from __future__ import annotations

import time

import pytest

from gc_mcp.controllers.base import InputStep, SequenceRunner, parse_step
from gc_mcp.controllers.pad_state import PadState
from gc_mcp.errors import SequenceError


class FakeBackend:
    """Records applied steps; ignores sequence machinery."""

    def __init__(self) -> None:
        self.applied: list[InputStep] = []

    def apply_step(self, port: int, step: InputStep) -> None:
        self.applied.append(step)


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
