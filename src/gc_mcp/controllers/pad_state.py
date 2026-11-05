"""GameCube pad state and the encoding between the API and Dolphin's Pipes.

Dolphin's stock "Pipes" controller backend exposes a device per FIFO in the
instance's ``Pipes/`` directory. Commands are newline-delimited tokens:

    PRESS  <button>
    RELEASE <button>
    SET {L,R} <v>
    SET {MAIN,C} <x> <y>

Where buttons are one of: A B X Y Z START L R D_UP D_DOWN D_LEFT D_RIGHT.

Stick values are 0..1 with 0.5 = centre; triggers are 0..1 with 0 = released and
1 = fully pressed. The public API exposes sticks in -1..1 and triggers in 0..1;
this module converts between the two representations.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The four GameCube controller ports (0-based).
PORTS = (0, 1, 2, 3)

#: Valid button identifiers (Pipes tokens).
BUTTONS = (
    "A",
    "B",
    "X",
    "Y",
    "Z",
    "START",
    "L",
    "R",
    "D_UP",
    "D_DOWN",
    "D_LEFT",
    "D_RIGHT",
)

#: Valid sticks.
STICKS = ("main", "c")

#: Valid triggers.
TRIGGERS = ("L", "R")

#: Which stick each GCPad axis token maps to (for display / state).
_STICK_TOKENS = {
    "main": "MAIN",
    "c": "C",
}


def validate_button(button: str) -> str:
    """Return the canonical Pipes token for a button, or raise ValueError."""
    token = str(button).strip().upper()
    if token not in BUTTONS:
        raise ValueError(
            f"invalid button {button!r}; expected one of {', '.join(BUTTONS)}"
        )
    return token


def validate_port(port: int) -> int:
    """Validate a controller port index (0-3)."""
    port = int(port)
    if port not in PORTS:
        raise ValueError(f"invalid controller port {port!r}; expected 0-3")
    return port


def validate_stick(stick: str) -> str:
    stick = str(stick).strip().lower()
    if stick not in STICKS:
        raise ValueError(f"invalid stick {stick!r}; expected one of {', '.join(STICKS)}")
    return stick


def validate_trigger(trigger: str) -> str:
    trigger = str(trigger).strip().upper()
    if trigger not in TRIGGERS:
        raise ValueError(f"invalid trigger {trigger!r}; expected one of {', '.join(TRIGGERS)}")
    return trigger


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def api_stick_to_pipe(value: float) -> float:
    """Convert an API stick axis in -1..1 to a Pipes axis in 0..1 (0.5 = centre)."""
    return clamp((float(value) + 1.0) / 2.0, 0.0, 1.0)


def pipe_stick_to_api(value: float) -> float:
    """Convert a Pipes axis in 0..1 back to an API stick axis in -1..1."""
    return clamp((float(value) * 2.0) - 1.0, -1.0, 1.0)


def api_trigger_to_pipe(value: float) -> float:
    """Convert an API trigger value in 0..1 to a Pipes SET value.

    ``SET L <v>`` results in ``Axis L +`` being set to ``v`` (see the Pipes
    backend), so the mapping is identity.
    """
    return clamp(float(value), 0.0, 1.0)


def pipe_trigger_to_api(value: float) -> float:
    return clamp(float(value), 0.0, 1.0)


@dataclass
class PadState:
    """Snapshot of one GameCube controller's state."""

    port: int
    buttons: set[str] = field(default_factory=set)
    main: tuple[float, float] = (0.0, 0.0)  # API x, y in -1..1
    c: tuple[float, float] = (0.0, 0.0)
    l: float = 0.0  # API trigger 0..1
    r: float = 0.0

    def to_dict(self) -> dict:
        return {
            "port": self.port,
            "buttons": sorted(self.buttons),
            "main_stick": {"x": round(self.main[0], 4), "y": round(self.main[1], 4)},
            "c_stick": {"x": round(self.c[0], 4), "y": round(self.c[1], 4)},
            "l_trigger": round(self.l, 4),
            "r_trigger": round(self.r, 4),
        }
