"""Tests for DolphinInstance.read_log and the save-state copy behaviour."""

from __future__ import annotations

from pathlib import Path

from gc_mcp.config import ServerConfig
from gc_mcp.instances.dolphin_instance import DolphinInstance, InstanceSpec


class FakeKeyboard:
    """A stand-in for KeyboardInjector that records save_state calls."""

    def __init__(self) -> None:
        self.saved: list[int] = []

    def save_state(self, slot: int):
        self.saved.append(slot)
        return type("KR", (), {
            "to_dict": lambda self: {"ok": True, "action": "save_state", "slot": slot},
        })()


def _make_instance(tmp_path, *, copy_base=None) -> DolphinInstance:
    cfg = ServerConfig(base_dir=tmp_path / "data")
    spec = InstanceSpec(
        instance_id="t",
        rom_path=tmp_path / "game.iso",
        user_dir=tmp_path / "user",
    )
    inst = DolphinInstance(spec, cfg)
    inst._keyboard = FakeKeyboard()
    return inst


class TestReadLog:
    def test_missing_log(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        res = inst.read_log()
        assert res["exists"] is False
        assert res["lines"] == []
        assert res["total_lines"] == 0

    def test_reads_lines(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        log = inst.log_path
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("boot ok\nOSREPORT: hello\ncrash!\n")
        res = inst.read_log()
        assert res["exists"] is True
        assert res["total_lines"] == 3
        assert res["lines"] == ["boot ok", "OSREPORT: hello", "crash!"]

    def test_grep_filters_case_insensitive(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        log = inst.log_path
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("boot ok\nOSREPORT: HELLO\ncrash!\n")
        res = inst.read_log(grep="hello")
        assert res["lines"] == ["OSREPORT: HELLO"]

    def test_tail_returns_last_lines(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        log = inst.log_path
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("a\nb\nc\nd\ne\n")
        res = inst.read_log(tail=2)
        assert res["lines"] == ["d", "e"]

    def test_grep_then_tail(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        log = inst.log_path
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("a\nerror1\nb\nerror2\nc\n")
        res = inst.read_log(grep="error", tail=1)
        assert res["lines"] == ["error2"]


class TestSaveStateCopy:
    def _make_state(self, inst: DolphinInstance, slot: int) -> Path:
        state = inst.spec.user_dir / "StateSaves" / f"GM4E01.s{slot:02d}"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_bytes(b"state-data")
        return state

    def test_copies_to_states_dir(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        self._make_state(inst, 1)
        res = inst.emulator_save_state(1)
        assert res["copied"] is True
        assert res["state_file"].endswith("GM4E01.s01")
        dest = Path(res["copied_to"])
        assert dest.is_file()
        assert dest.read_bytes() == b"state-data"
        # Default copy target is <base_dir>/states.
        assert dest.parent == tmp_path / "data" / "states"

    def test_copies_to_custom_dir(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        self._make_state(inst, 2)
        custom = tmp_path / "export"
        res = inst.emulator_save_state(2, copy_dir=custom)
        assert res["copied"] is True
        dest = Path(res["copied_to"])
        assert dest.parent == custom
        assert dest.read_bytes() == b"state-data"

    def test_no_state_file_reports_not_copied(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        # No StateSaves/*.s03 file exists; the save hotkey reports ok but the
        # copy must be reported as not happening.
        res = inst.emulator_save_state(3)
        assert res["copied"] is False
        assert res["state_file"] is None
        assert "did not appear" in res["error"]

    def test_keyboard_failure_skips_copy(self, tmp_path) -> None:
        inst = _make_instance(tmp_path)
        inst._keyboard = type("KR", (), {
            "save_state": lambda self, slot: type("KR", (), {
                "to_dict": lambda self: {"ok": False, "action": "save_state",
                                         "error": "xdotool missing"},
            })(),
        })()
        res = inst.emulator_save_state(1)
        assert res["ok"] is False
        assert "copied" not in res
