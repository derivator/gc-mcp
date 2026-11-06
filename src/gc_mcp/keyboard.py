"""Keyboard injection for Dolphin emulator hotkeys.

Dolphin exposes emulator-level actions (pause, frame-advance, save/load state)
as *hotkeys* bound to keyboard keys (see ``HotkeyManager::LoadDefaults``). To
drive these from the MCP server we inject keystrokes into the instance's X
display using ``xdotool`` (the XTest extension). This is the same mechanism a
TAS tool uses to control the emulator.

Prerequisites
-------------
* ``xdotool`` must be installed on PATH.
* The instance must use the ``xvfb`` capture backend so there is an X display
  that hosts Dolphin's render window.
* Dolphin's ``[Input] BackgroundInput = True`` (written by the config builder)
  keeps the *input gate* open even though the ``--batch`` render window never
  receives focus. Without it the gate closes and Dolphin drops the injected
  input.
* Dolphin's ``[General] HotkeysRequireFocus = False`` (also written by the
  config builder) keeps the *hotkey* input gate open. ``HotkeysRequireFocus``
  is ``MAIN_FOCUSED_HOTKEYS`` and defaults to true, which silently drops every
  hotkey in batch mode because the render window never becomes Qt-active.

The hotkey device
------------------
Dolphin's stock ``Hotkeys.ini`` uses ``Device = Keyboard``, which resolves to a
virtual "Keyboard" source device. In a headless X11 build only the XInput2
backend registers a keyboard, and it names its device after the *pointer*
(e.g. ``Virtual core pointer``) with source ``XInput2`` and id ``0``. So the
correct device string for an Xvfb instance is ``XInput2/0/Virtual core
pointer``; the stock ``Keyboard`` device is not registered and would silently
leave every hotkey unbound. ``HOTKEY_DEVICE`` below is that string.

The default bindings below match stock Dolphin (``HotkeyManager::LoadDefaults``).
The only action without a default binding is **Frame Advance**, so the config
builder adds a ``Frame Advance/Frame Advance = F8`` binding to the generated
``Hotkeys.ini``; ``dolphin_hotkey_set`` can re-bind it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Dolphin default hotkey bindings (from HotkeyManager::LoadDefaults).
# ---------------------------------------------------------------------------
#: Toggle the emulator's internal pause state (HK_PLAY_PAUSE).
HOTKEY_TOGGLE_PAUSE = "F10"
#: Frame Advance (HK_FRAME_ADVANCE) - *not* bound by default in Dolphin, so we
#: bind it ourselves in the generated Hotkeys.ini.
HOTKEY_FRAME_ADVANCE = "F8"
#: Save state to slot N (HK_SAVE_STATE_SLOT_N) is Shift+F1..Shift+F8.
HOTKEY_SAVE_STATE_TEMPLATE = "shift+F{slot}"
#: Load state from slot N (HK_LOAD_STATE_SLOT_N) is F1..F8.
HOTKEY_LOAD_STATE_TEMPLATE = "F{slot}"

#: The Hotkeys.ini key for the Frame Advance control (``<group>/<control>``).
HOTKEY_KEY_FRAME_ADVANCE = "Frame Advance/Frame Advance"
#: The Hotkeys.ini key for the Toggle Pause control.
HOTKEY_KEY_TOGGLE_PAUSE = "General/Toggle Pause"
#: The Hotkeys.ini key for a save-state slot (``<group>/<control>``).
HOTKEY_KEY_SAVE_STATE = "Save State/Save State Slot {slot}"
#: The Hotkeys.ini key for a load-state slot (``<group>/<control>``).
HOTKEY_KEY_LOAD_STATE = "Load State/Load State Slot {slot}"

#: The ``Device`` line written into the generated ``Hotkeys.ini``.
#:
#: Dolphin resolves this against the devices registered by the *active* input
#: backend. The stock ``Keyboard`` device is not registered in a headless X11
#: build; the XInput2 backend names its keyboard after the X pointer and uses
#: source ``XInput2`` with id ``0``. For an Xvfb instance the master pointer is
#: always ``Virtual core pointer``, giving the fully-qualified string below. If
#: this does not match (e.g. a differently-named pointer), Dolphin leaves every
#: hotkey unbound and the emulator-control tools silently do nothing.
HOTKEY_DEVICE = "XInput2/0/Virtual core pointer"


#: The full set of hotkey bindings Dolphin binds by default (from
#: ``HotkeyManager::LoadDefaults``, non-Windows), plus the Frame Advance binding
#: we add so ``gc_emulator_step_frame`` works out of the box.
#:
#: Dolphin loads ``Hotkeys.ini`` all-or-nothing: if the file exists, any control
#: not listed is left *unbound* (empty expression) rather than falling back to a
#: default. So any Hotkeys.ini we write must contain every binding we want kept.
DEFAULT_HOTKEYS: dict[str, str] = {
    # General
    "General/Open": "@(Ctrl+O)",
    "General/Toggle Pause": "F10",
    "General/Stop": "Escape",
    "General/Toggle Fullscreen": "@(Alt+Return)",
    "General/Take Screenshot": "F9",
    # Stepping (CPU debugger)
    "Stepping/Step Into": "F11",
    "Stepping/Step Over": "@(Shift+F10)",
    "Stepping/Step Out": "@(Shift+F11)",
    # Breakpoint
    "Breakpoint/Toggle Breakpoint": "@(Shift+F9)",
    # Emulation Speed
    "Emulation Speed/Disable Emulation Speed Limit": "Tab",
    # Wii
    "Wii/Connect Wii Remote 1": "@(Alt+F5)",
    "Wii/Connect Wii Remote 2": "@(Alt+F6)",
    "Wii/Connect Wii Remote 3": "@(Alt+F7)",
    "Wii/Connect Wii Remote 4": "@(Alt+F8)",
    "Wii/Connect Balance Board": "@(Alt+F9)",
    # Load State (slots 1-8 have defaults; 9-10 are unbound by default)
    **{f"Load State/Load State Slot {i}": f"F{i}" for i in range(1, 9)},
    # Save State (slots 1-8 have defaults)
    **{f"Save State/Save State Slot {i}": f"@(Shift+F{i})" for i in range(1, 9)},
    # Other State Hotkeys
    "Other State Hotkeys/Undo Load State": "F12",
    "Other State Hotkeys/Undo Save State": "@(Shift+F12)",
    # Frame Advance - no Dolphin default, so we bind it here.
    HOTKEY_KEY_FRAME_ADVANCE: HOTKEY_FRAME_ADVANCE,
}


@dataclass
class KeyboardResult:
    """Outcome of a keyboard injection attempt."""

    ok: bool
    action: str
    keys: str
    display: str | None
    error: str | None = None
    stdout: str = ""

    def to_dict(self) -> dict:
        d = {
            "ok": self.ok,
            "action": self.action,
            "keys": self.keys,
            "display": self.display,
        }
        if self.error:
            d["error"] = self.error
        if self.stdout:
            d["stdout"] = self.stdout
        return d


class KeyboardInjector:
    """Send keystrokes (Dolphin hotkeys) to an X display via ``xdotool``."""

    def __init__(self, display: str | int, window_name: str | None = None,
                 timeout: float = 10.0) -> None:
        # Normalise to ":N" form. A bare number becomes ":N"; a full display
        # string (e.g. "localhost:91" or "unix:0") is kept as-is.
        if str(display).isdigit():
            self.display = f":{int(display)}"
        elif str(display).startswith(":"):
            self.display = str(display)
        else:
            self.display = str(display)
        #: Optional window-name substring used to find Dolphin's render window
        #: before injecting (so the key events reach the render widget).
        self.window_name = window_name
        self.timeout = timeout

    # ------------------------------------------------------------------
    @staticmethod
    def available() -> bool:
        """Whether ``xdotool`` is installed and usable."""
        return shutil.which("xdotool") is not None

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        """Run an xdotool command on this display."""
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        try:
            return subprocess.run(
                ["xdotool", *args],
                capture_output=True,
                timeout=self.timeout,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "xdotool is not installed; install it to use keyboard hotkey injection"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"xdotool timed out: {args}") from exc

    def _find_window(self) -> str | None:
        """Return the id of Dolphin's render window, or None if not found."""
        name = self.window_name or "Dolphin"
        # Try the specific name first, then any visible top-level window.
        for query in (name, ""):
            args = ["search", "--onlyvisible", "--name", query] if query else ["search", "--onlyvisible"]
            res = self._run(args)
            if res.returncode == 0:
                ids = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
                if ids:
                    # Prefer a non-root window id (the first is usually the most
                    # recently mapped top-level window).
                    return ids[0]
        return None

    # ------------------------------------------------------------------
    def send_hotkey(self, keys: str, *, action: str, focus: bool = True) -> KeyboardResult:
        """Send a single hotkey combo (e.g. ``"F10"``, ``"shift+F1"``).

        ``keys`` uses xdotool key syntax. When ``focus`` is true we first try to
        focus Dolphin's render window so the key events reach it.
        """
        if not self.available():
            return KeyboardResult(
                ok=False, action=action, keys=keys, display=self.display,
                error="xdotool is not installed on PATH; install it to use keyboard "
                "hotkey injection (e.g. `sudo pacman -S xdotool` / `sudo apt install xdotool`)",
            )

        if focus:
            win = self._find_window()
            if win:
                res = self._run(["windowfocus", "--sync", win])
                if res.returncode != 0:
                    return KeyboardResult(
                        ok=False, action=action, keys=keys, display=self.display,
                        error=f"could not focus Dolphin window {win}: {res.stderr.strip()}",
                        stdout=res.stdout.strip(),
                    )

        res = self._run(["key", "--clearmodifiers", keys])
        if res.returncode != 0:
            return KeyboardResult(
                ok=False, action=action, keys=keys, display=self.display,
                error=f"xdotool key failed: {res.stderr.strip()}",
                stdout=res.stdout.strip(),
            )
        return KeyboardResult(ok=True, action=action, keys=keys, display=self.display)

    # ------------------------------------------------------------------
    # Convenience wrappers for the actions we expose as MCP tools.
    # ------------------------------------------------------------------
    def toggle_pause(self) -> KeyboardResult:
        return self.send_hotkey(HOTKEY_TOGGLE_PAUSE, action="toggle_pause")

    def step_frame(self) -> KeyboardResult:
        return self.send_hotkey(HOTKEY_FRAME_ADVANCE, action="step_frame")

    def save_state(self, slot: int) -> KeyboardResult:
        keys = HOTKEY_SAVE_STATE_TEMPLATE.format(slot=slot)
        return self.send_hotkey(keys, action="save_state")

    def load_state(self, slot: int) -> KeyboardResult:
        keys = HOTKEY_LOAD_STATE_TEMPLATE.format(slot=slot)
        return self.send_hotkey(keys, action="load_state")


def build_hotkey_ini(bindings: dict[str, str], device: str = HOTKEY_DEVICE) -> str:
    """Build a Dolphin ``Hotkeys.ini`` body from ``{group/control: expression}``.

    The INI key format is ``<group>/<control>`` (e.g. ``General/Toggle Pause``),
    matching ``ControlGroup::SaveConfig``. Expressions are written as-is (e.g.
    ``F10`` for a single key, ``@(Shift+F8)`` for a combination), matching what
    ``HotkeyManager::LoadDefaults`` stores and what Dolphin re-serialises.

    ``device`` is the ``Device`` line; it must resolve to a registered input
    device or Dolphin leaves every hotkey unbound. See ``HOTKEY_DEVICE``.
    """
    lines = ["[Hotkeys]", f"Device = {device}", ""]
    for key, expression in bindings.items():
        lines.append(f"{key} = {expression}")
    lines.append("")
    return "\n".join(lines)


def read_hotkeys_ini(path: Path) -> dict[str, str]:
    """Read the ``[Hotkeys]`` section of a ``Hotkeys.ini`` into ``{key: expr}``.

    Returns an empty dict if the file does not exist or has no ``[Hotkeys]``
    section. Uses a simple line-based parser (not ``configparser``) so it never
    lower-cases keys or mangles the value expressions.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    in_section = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_section = line == "[Hotkeys]"
            continue
        if in_section and "=" in line:
            key, value = line.split("=", 1)
            key = key.strip()
            # ``Device`` is a section-level setting (which device the hotkeys are
            # bound to), not a hotkey control binding.
            if key != "Device":
                values[key] = value.strip()
    return values


def write_hotkeys_ini(path: Path, bindings: dict[str, str],
                      device: str = HOTKEY_DEVICE) -> None:
    """Write a complete ``Hotkeys.ini`` (``[Hotkeys]`` section) to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_hotkey_ini(bindings, device=device), encoding="utf-8")
