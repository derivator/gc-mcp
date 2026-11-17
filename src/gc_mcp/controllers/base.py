"""Controller backend abstraction and the timed-sequence engine.

The controller tools need to support *momentary actions / timed input
sequences* because the model driving the agent may be too slow to reliably
pair a press and a release. ``gc_controller_sequence`` therefore schedules a
list of steps (button press/release, stick/trigger moves, waits) at millisecond
timestamps and executes them in a background thread.
"""

from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..errors import SequenceError
from . import pad_state as ps


@dataclass
class InputStep:
    """One step in a timed input sequence.

    ``t`` is milliseconds from the start of the sequence.
    """

    t: float
    type: str  # press | release | stick | trigger | wait | reset
    button: str | None = None
    stick: str | None = None
    x: float | None = None
    y: float | None = None
    trigger: str | None = None
    value: float | None = None


def parse_step(raw: dict) -> InputStep:
    """Validate and normalise a step dictionary from a tool argument."""
    if not isinstance(raw, dict):
        raise SequenceError(f"each sequence step must be an object, got {type(raw).__name__}")
    t = float(raw.get("t", 0))
    step_type = str(raw.get("type", "")).strip().lower()
    if step_type not in {"press", "release", "stick", "trigger", "wait", "reset"}:
        raise SequenceError(f"invalid sequence step type {step_type!r}")

    step = InputStep(t=t, type=step_type)
    if step_type in {"press", "release"}:
        step.button = ps.validate_button(raw.get("button", ""))
    elif step_type == "stick":
        step.stick = ps.validate_stick(raw.get("stick", "main"))
        step.x = float(raw.get("x", 0.0))
        step.y = float(raw.get("y", 0.0))
    elif step_type == "trigger":
        step.trigger = ps.validate_trigger(raw.get("trigger", "L"))
        step.value = ps.api_trigger_to_pipe(float(raw.get("value", 0.0)))
    elif step_type == "wait":
        # For a wait step, `t` is the absolute timestamp; the "duration" is the
        # gap to the next step. Nothing else to do.
        pass
    return step


class SequenceRunner:
    """Runs an ordered list of InputSteps on a background thread."""

    def __init__(self, backend: ControllerBackend, port: int, steps: list[InputStep],
                 loop: bool = False) -> None:
        self.backend = backend
        self.port = port
        self.steps = sorted(steps, key=lambda s: s.t)
        self.loop = loop
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=f"gc-seq-{uuid.uuid4().hex[:8]}",
                                        daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            start = time.monotonic()
            while not self._stop.is_set():
                for step in self.steps:
                    if self._stop.is_set():
                        return
                    target = start + (step.t / 1000.0)
                    delay = target - time.monotonic()
                    if delay > 0 and self._stop.wait(delay):
                        return
                    if self._stop.is_set():
                        return
                    self.backend.apply_step(self.port, step)
                if not self.loop:
                    return
                start = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - defensive safety net
            self.backend._sequence_failed(self.port, exc)


class ControllerBackend(ABC):
    """Abstract controller backend.

    Implementations provide the primitive actions (press/release/set_stick/
    set_trigger/reset/get_state). Sequence scheduling is provided here for all
    backends so real-hardware backends get it for free.
    """

    def __init__(self) -> None:
        self._sequences: dict[str, SequenceRunner] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Abstract primitives
    # ------------------------------------------------------------------
    @abstractmethod
    def press(self, port: int, button: str) -> None:
        ...

    @abstractmethod
    def release(self, port: int, button: str) -> None:
        ...

    @abstractmethod
    def set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        ...

    @abstractmethod
    def set_trigger(self, port: int, trigger: str, value: float) -> None:
        ...

    @abstractmethod
    def get_state(self, port: int) -> ps.PadState:
        ...

    @abstractmethod
    def reset(self, port: int) -> None:
        ...

    def close(self) -> None:
        """Cancel any running sequences and release resources."""
        for seq in list(self._sequences.values()):
            seq.cancel()

    # ------------------------------------------------------------------
    # Step application (used by the sequence runner)
    # ------------------------------------------------------------------
    def apply_step(self, port: int, step: InputStep) -> None:
        if step.type == "press":
            self.press(port, step.button)  # type: ignore[arg-type]
        elif step.type == "release":
            self.release(port, step.button)  # type: ignore[arg-type]
        elif step.type == "stick":
            self.set_stick(port, step.stick, step.x or 0.0, step.y or 0.0)  # type: ignore[arg-type]
        elif step.type == "trigger":
            self.set_trigger(port, step.trigger, step.value or 0.0)  # type: ignore[arg-type]
        elif step.type == "reset":
            self.reset(port)
        # 'wait' steps are handled purely by timing in the runner.

    def _sequence_failed(self, port: int, exc: Exception) -> None:
        # Store nothing for now; the caller can observe the sequence is no
        # longer running via sequence_cancel / get_state.
        pass

    # ------------------------------------------------------------------
    # High-level action
    # ------------------------------------------------------------------
    def press_once(self, port: int, button: str, hold_duration_ms: float = 200.0) -> None:
        """Press a button, hold it for ``hold_duration_ms``, then release it.

        This is the recommended way to *tap* a button for agent use. Unlike
        :meth:`press` / :meth:`release`, which require the caller to pair them
        at the right time, ``press_once`` performs the whole press → hold →
        release cycle in a single call and only returns once the button has been
        released. The button is always released even if an error occurs.

        ``hold_duration_ms`` defaults to 200 ms.
        """
        if hold_duration_ms < 0:
            raise ValueError("hold_duration_ms must be >= 0")
        self.press(port, button)
        try:
            time.sleep(hold_duration_ms / 1000.0)
        finally:
            self.release(port, button)

    def stick_flick(self, port: int, stick: str, x: float, y: float,
                    hold_duration_ms: float = 200.0) -> None:
        """Push the analog stick to ``(x, y)``, hold it, then return to centre.

        This is the analog counterpart to :meth:`press_once`: a single atomic
        action used to *flick* the stick for menu navigation. It pushes the
        stick to the given direction, holds it for ``hold_duration_ms``, then
        returns it to ``(0, 0)`` (centre). The stick is always returned to centre
        even if an error occurs.

        ``x``/``y`` are in the API's -1..1 range (0,0 = centre). ``stick`` is
        ``'main'`` or ``'c'``. ``hold_duration_ms`` defaults to 200 ms.
        """
        if hold_duration_ms < 0:
            raise ValueError("hold_duration_ms must be >= 0")
        self.set_stick(port, stick, x, y)
        try:
            time.sleep(hold_duration_ms / 1000.0)
        finally:
            self.set_stick(port, stick, 0.0, 0.0)

    # ------------------------------------------------------------------
    # Sequence API
    # ------------------------------------------------------------------
    def sequence(self, port: int, steps: list[dict], loop: bool = False) -> dict:
        parsed = [parse_step(s) for s in steps]
        if not parsed:
            raise SequenceError("sequence must contain at least one step")
        runner = SequenceRunner(self, port, parsed, loop=loop)
        seq_id = uuid.uuid4().hex
        with self._lock:
            self._sequences[seq_id] = runner
        runner.start()
        return {"sequence_id": seq_id, "steps": len(parsed), "loop": loop, "running": True}

    def sequence_cancel(self, sequence_id: str) -> bool:
        with self._lock:
            runner = self._sequences.get(sequence_id)
        if runner is None:
            return False
        runner.cancel()
        return True

    def list_sequences(self) -> list[dict]:
        with self._lock:
            return [
                {"sequence_id": sid, "running": r.running}
                for sid, r in self._sequences.items()
            ]
