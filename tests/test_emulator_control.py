"""Tests for the emulator-control (pause / step / save / load) tools and the
KeyboardInjector that drives Dolphin hotkeys via xdotool."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import gc_mcp.server as server
from gc_mcp.keyboard import (
    DEFAULT_HOTKEYS,
    HOTKEY_FRAME_ADVANCE,
    HOTKEY_DEVICE,
    HOTKEY_KEY_FRAME_ADVANCE,
    HOTKEY_KEY_TOGGLE_PAUSE,
    HOTKEY_LOAD_STATE_TEMPLATE,
    HOTKEY_SAVE_STATE_TEMPLATE,
    HOTKEY_TOGGLE_PAUSE,
    KeyboardInjector,
    build_hotkey_ini,
    read_hotkeys_ini,
    write_hotkeys_ini,
)


class FakeProc:
    """A stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# KeyboardInjector (mocked subprocess.run / shutil.which)
# ---------------------------------------------------------------------------
class TestKeyboardInjector:
    def _make(self, monkeypatch, *, available: bool = True, search_out: str = "42",
              search_rc: int = 0, key_rc: int = 0, focus_rc: int = 0) -> KeyboardInjector:
        calls: list[list[str]] = []

        def fake_run(args, **kwargs):
            calls.append(args)
            if args[1] == "search":
                return FakeProc(search_rc, search_out)
            if args[1] == "windowfocus":
                return FakeProc(focus_rc)
            if args[1] == "key":
                return FakeProc(key_rc)
            return FakeProc(0)

        monkeypatch.setattr("gc_mcp.keyboard.subprocess.run", fake_run)
        monkeypatch.setattr(
            "gc_mcp.keyboard.shutil.which", lambda name: "/usr/bin/xdotool" if available else None
        )
        injector = KeyboardInjector(":91")
        injector._calls = calls  # type: ignore[attr-defined]
        return injector

    def test_display_normalisation(self):
        assert KeyboardInjector(":91").display == ":91"
        assert KeyboardInjector(91).display == ":91"
        assert KeyboardInjector("91").display == ":91"
        assert KeyboardInjector("localhost:91").display == "localhost:91"

    def test_available_false(self, monkeypatch):
        monkeypatch.setattr("gc_mcp.keyboard.shutil.which", lambda name: None)
        injector = KeyboardInjector(":91")
        assert injector.available() is False
        res = injector.toggle_pause()
        assert res.ok is False
        assert "xdotool is not installed" in res.error

    def test_toggle_pause_sends_f10(self, monkeypatch):
        inj = self._make(monkeypatch)
        res = inj.toggle_pause()
        assert res.ok is True
        assert res.keys == HOTKEY_TOGGLE_PAUSE
        # search, windowfocus, then key
        assert inj._calls[-1] == ["xdotool", "key", "--clearmodifiers", "F10"]
        assert inj._calls[-2] == ["xdotool", "windowfocus", "--sync", "42"]

    def test_step_frame_uses_configured_hotkey(self, monkeypatch):
        inj = self._make(monkeypatch)
        res = inj.step_frame()
        assert res.ok is True
        assert res.keys == HOTKEY_FRAME_ADVANCE
        assert inj._calls[-1] == ["xdotool", "key", "--clearmodifiers", HOTKEY_FRAME_ADVANCE]

    def test_save_state_slot_binding(self, monkeypatch):
        inj = self._make(monkeypatch)
        res = inj.save_state(3)
        assert res.ok is True
        assert res.keys == HOTKEY_SAVE_STATE_TEMPLATE.format(slot=3)

    def test_load_state_slot_binding(self, monkeypatch):
        inj = self._make(monkeypatch)
        res = inj.load_state(5)
        assert res.ok is True
        assert res.keys == HOTKEY_LOAD_STATE_TEMPLATE.format(slot=5)

    def test_key_failure_reported(self, monkeypatch):
        inj = self._make(monkeypatch, key_rc=1)
        res = inj.toggle_pause()
        assert res.ok is False
        assert "xdotool key failed" in res.error

    def test_focus_failure_reported(self, monkeypatch):
        inj = self._make(monkeypatch, focus_rc=1)
        res = inj.toggle_pause()
        assert res.ok is False
        assert "could not focus Dolphin window" in res.error

    def test_no_window_found_skips_focus(self, monkeypatch):
        inj = self._make(monkeypatch, search_out="")
        res = inj.toggle_pause()
        assert res.ok is True
        # No windowfocus call; straight to key.
        assert inj._calls[-1] == ["xdotool", "key", "--clearmodifiers", "F10"]

    def test_focus_false_skips_search(self, monkeypatch):
        inj = self._make(monkeypatch)
        res = inj.send_hotkey("F10", action="toggle_pause", focus=False)
        assert res.ok is True
        assert inj._calls == [["xdotool", "key", "--clearmodifiers", "F10"]]


# ---------------------------------------------------------------------------
# build_hotkey_ini
# ---------------------------------------------------------------------------
class TestBuildHotkeyIni:
    def test_writes_expressions_as_is(self):
        ini = build_hotkey_ini({"General/Toggle Pause": "F10"})
        assert ini == f"[Hotkeys]\nDevice = {HOTKEY_DEVICE}\n\nGeneral/Toggle Pause = F10\n"

    def test_combination_expression_not_wrapped(self):
        ini = build_hotkey_ini({"Save State/Save State Slot 1": "@(Shift+F1)"})
        assert "Save State/Save State Slot 1 = @(Shift+F1)" in ini

    def test_multiple_bindings(self):
        ini = build_hotkey_ini(
            {
                "General/Toggle Pause": "F10",
                "General/Frame Advance": "F8",
            }
        )
        assert "General/Toggle Pause = F10" in ini
        assert "General/Frame Advance = F8" in ini

    def test_has_device_line(self):
        ini = build_hotkey_ini({})
        assert ini.startswith(f"[Hotkeys]\nDevice = {HOTKEY_DEVICE}\n")

    def test_device_line_is_xinput2(self):
        # The stock "Keyboard" device is not registered in a headless X11 build;
        # the XInput2 keyboard device is named after the pointer. This is the
        # device string that actually resolves so hotkeys fire.
        assert HOTKEY_DEVICE.startswith("XInput2/")
        ini = build_hotkey_ini({}, device="Keyboard")
        assert ini.startswith("[Hotkeys]\nDevice = Keyboard\n")


class TestHotkeysIniRoundTrip:
    def test_read_write_roundtrip(self, tmp_path):
        path = tmp_path / "Hotkeys.ini"
        write_hotkeys_ini(path, {"General/Toggle Pause": "F10"})
        values = read_hotkeys_ini(path)
        assert values == {"General/Toggle Pause": "F10"}

    def test_read_missing_file(self, tmp_path):
        assert read_hotkeys_ini(tmp_path / "nope.ini") == {}

    def test_read_ignores_other_sections(self, tmp_path):
        path = tmp_path / "Hotkeys.ini"
        path.write_text(
            "[Hotkeys]\nGeneral/Toggle Pause = F10\n\n[GCPad1]\nDevice = Pipe/0/ctrl1\n",
            encoding="utf-8",
        )
        values = read_hotkeys_ini(path)
        assert values == {"General/Toggle Pause": "F10"}

    def test_read_preserves_key_case(self, tmp_path):
        path = tmp_path / "Hotkeys.ini"
        path.write_text("[Hotkeys]\nFrame Advance/Frame Advance = F8\n", encoding="utf-8")
        values = read_hotkeys_ini(path)
        assert values == {"Frame Advance/Frame Advance": "F8"}

    def test_default_hotkeys_is_complete(self):
        # Pause, frame advance, and save/load state must all be present so a
        # generated Hotkeys.ini never silently un-binds them.
        assert DEFAULT_HOTKEYS[HOTKEY_KEY_TOGGLE_PAUSE] == "F10"
        assert DEFAULT_HOTKEYS[HOTKEY_KEY_FRAME_ADVANCE] == HOTKEY_FRAME_ADVANCE
        assert "Save State/Save State Slot 1" in DEFAULT_HOTKEYS
        assert "Load State/Load State Slot 8" in DEFAULT_HOTKEYS


# ---------------------------------------------------------------------------
# MCP tool layer (monkeypatch server._instance)
# ---------------------------------------------------------------------------
class FakeEmulatorPlatform:
    """A minimal stand-in for DolphinPlatform with emulator_*/read_log methods."""

    def __init__(self, results: dict | None = None) -> None:
        self.calls: list[tuple] = []
        self.results = results or {}

    def emulator_toggle_pause(self):
        self.calls.append(("toggle_pause",))
        return {"ok": True, "action": "toggle_pause"}

    def emulator_step_frame(self):
        self.calls.append(("step_frame",))
        return {"ok": True, "action": "step_frame"}

    def emulator_save_state(self, slot: int, copy_dir=None):
        self.calls.append(("save_state", slot, copy_dir))
        return {"ok": True, "action": "save_state", "slot": slot, "copied": True}

    def emulator_load_state(self, slot: int):
        self.calls.append(("load_state", slot))
        return {"ok": True, "action": "load_state", "slot": slot}

    def emulator_freeze(self):
        self.calls.append(("freeze",))
        return {"ok": True, "state": "frozen"}

    def emulator_unfreeze(self):
        self.calls.append(("unfreeze",))
        return {"ok": True, "state": "running"}

    def read_log(self, tail=None, grep=None):
        self.calls.append(("read_log", tail, grep))
        return {"instance_id": "inst", "path": "/tmp/dolphin.log", "exists": True,
                "total_lines": 3, "tail": tail, "grep": grep, "returned_lines": 3,
                "lines": ["line1", "line2", "line3"]}


@pytest.fixture
def platform(monkeypatch):
    fake = FakeEmulatorPlatform()
    monkeypatch.setattr(server, "_instance", lambda instance_id: fake)
    return fake


class TestEmulatorTools:
    def test_toggle_pause(self, platform):
        res = server.gc_dolphin_toggle_pause("inst")
        assert res["ok"] is True
        assert platform.calls == [("toggle_pause",)]

    def test_step_frame(self, platform):
        res = server.gc_dolphin_step_frame("inst")
        assert res["ok"] is True
        assert platform.calls == [("step_frame",)]

    def test_save_state(self, platform):
        res = server.gc_dolphin_save_state("inst", 2)
        assert res["slot"] == 2
        assert res["copied"] is True
        assert platform.calls == [("save_state", 2, None)]

    def test_save_state_with_copy_dir(self, platform):
        res = server.gc_dolphin_save_state("inst", 2, copy_dir="/tmp/states")
        assert res["slot"] == 2
        assert platform.calls == [("save_state", 2, Path("/tmp/states"))]

    def test_log(self, platform):
        res = server.gc_log("inst")
        assert res["exists"] is True
        assert res["lines"] == ["line1", "line2", "line3"]
        assert platform.calls == [("read_log", None, None)]

    def test_log_with_tail_and_grep(self, platform):
        res = server.gc_log("inst", tail=10, grep="error")
        assert res["tail"] == 10
        assert res["grep"] == "error"
        assert platform.calls == [("read_log", 10, "error")]

    def test_load_state(self, platform):
        res = server.gc_dolphin_load_state("inst", 7)
        assert res["slot"] == 7
        assert platform.calls == [("load_state", 7)]

    def test_save_state_slot_out_of_range(self, platform):
        res = server.gc_dolphin_save_state("inst", 9)
        assert res["ok"] is False
        assert "slot must be 1-8" in res["error"]
        assert platform.calls == []

    def test_load_state_slot_out_of_range(self, platform):
        res = server.gc_dolphin_load_state("inst", 0)
        assert res["ok"] is False
        assert "slot must be 1-8" in res["error"]
        assert platform.calls == []

    def test_freeze(self, platform):
        res = server.gc_dolphin_freeze("inst")
        assert res["state"] == "frozen"
        assert platform.calls == [("freeze",)]

    def test_unfreeze(self, platform):
        res = server.gc_dolphin_unfreeze("inst")
        assert res["state"] == "running"
        assert platform.calls == [("unfreeze",)]


# ---------------------------------------------------------------------------
# gc_controller_flick (monkeypatch server._instance)
# ---------------------------------------------------------------------------
class FakeControllerPlatform:
    """A minimal stand-in for DolphinPlatform with controller_flick."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def controller_flick(self, port: int, stick: str, x: float, y: float,
                         hold_duration: float = 200.0):
        self.calls.append((port, stick, x, y, hold_duration))


@pytest.fixture
def controller_platform(monkeypatch):
    fake = FakeControllerPlatform()
    monkeypatch.setattr(server, "_instance", lambda instance_id: fake)
    return fake


class TestControllerFlick:
    def test_flick(self, controller_platform):
        res = server.gc_controller_flick("inst", 0, "main", 0.0, 1.0)
        assert res["state"] == "centered"
        assert controller_platform.calls == [(0, "main", 0.0, 1.0, 200.0)]

    def test_flick_custom_hold(self, controller_platform):
        server.gc_controller_flick("inst", 1, "c", -1.0, 0.5, hold_duration=500.0)
        assert controller_platform.calls == [(1, "c", -1.0, 0.5, 500.0)]


# ---------------------------------------------------------------------------
# gc_dolphin_hotkey_get / gc_dolphin_hotkey_set (monkeypatch server._instance)
# ---------------------------------------------------------------------------
class FakeHotkeyPlatform:
    """A stand-in for DolphinPlatform with an instance.spec.user_dir."""

    def __init__(self, user_dir) -> None:
        self.instance = type("Inst", (), {"spec": type("Spec", (), {"user_dir": user_dir})()})()


@pytest.fixture
def hotkey_platform(monkeypatch, tmp_path):
    fake = FakeHotkeyPlatform(tmp_path)
    monkeypatch.setattr(server, "_instance", lambda instance_id: fake)
    return fake


class TestDolphinHotkeyTools:
    def _hotkeys(self, tmp_path) -> Path:
        return tmp_path / "Config" / "Hotkeys.ini"

    def test_get_returns_default_when_no_file(self, hotkey_platform):
        res = server.gc_dolphin_hotkey_get("inst", "General/Toggle Pause")
        assert res["binding"] == "F10"
        assert res["bound"] is True

    def test_get_unbound_when_missing_from_existing_file(self, hotkey_platform, tmp_path):
        # A Hotkeys.ini exists but does not contain the key -> unbound.
        from gc_mcp.keyboard import write_hotkeys_ini

        write_hotkeys_ini(self._hotkeys(tmp_path), {"Frame Advance/Frame Advance": "F8"})
        res = server.gc_dolphin_hotkey_get("inst", "General/Toggle Pause")
        assert res["bound"] is False
        assert res["binding"] is None

    def test_set_writes_complete_file(self, hotkey_platform, tmp_path):
        server.gc_dolphin_hotkey_set("inst", "Frame Advance/Frame Advance", "F8")
        path = self._hotkeys(tmp_path)
        assert path.exists()
        values = read_hotkeys_ini(path)
        assert values["General/Toggle Pause"] == "F10"  # default preserved
        assert values["Frame Advance/Frame Advance"] == "F8"

    def test_set_overrides_existing(self, hotkey_platform, tmp_path):
        from gc_mcp.keyboard import write_hotkeys_ini

        write_hotkeys_ini(self._hotkeys(tmp_path), {"General/Toggle Pause": "F10"})
        server.gc_dolphin_hotkey_set("inst", "General/Toggle Pause", "F11")
        values = read_hotkeys_ini(self._hotkeys(tmp_path))
        assert values["General/Toggle Pause"] == "F11"

    def test_set_empty_unbinds(self, hotkey_platform, tmp_path):
        server.gc_dolphin_hotkey_set("inst", "Frame Advance/Frame Advance", "")
        values = read_hotkeys_ini(self._hotkeys(tmp_path))
        assert "Frame Advance/Frame Advance" not in values


class TestServerInstructions:
    def test_instructions_are_set(self):
        assert server.mcp.instructions

    def test_instructions_cover_sequence_batching(self):
        assert "gc_controller_sequence" in server.SERVER_INSTRUCTIONS

    def test_instructions_cover_blind_input_before_capture(self):
        assert "blind" in server.SERVER_INSTRUCTIONS
        assert "gc_capture_screenshot" in server.SERVER_INSTRUCTIONS

    def test_instructions_cover_big_endian(self):
        assert ">" in server.SERVER_INSTRUCTIONS

    def test_instructions_cover_emulator_control(self):
        assert "gc_dolphin_toggle_pause" in server.SERVER_INSTRUCTIONS
        assert "gc_dolphin_step_frame" in server.SERVER_INSTRUCTIONS
