"""Tests for the unified ROM-load path (netboot for hardware, relaunch for Dolphin)."""

from __future__ import annotations

import textwrap

import pytest

import gc_mcp.server as server
from gc_mcp.config import ServerConfig
from gc_mcp.errors import ControllerError, NetbootError, NotFoundError
from gc_mcp.instances import (
    HardwareInstance,
    HardwareInstanceSpec,
    HardwareManager,
    InstanceManager,
)
from gc_mcp.platform import GameCubeHWPlatform


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_hw_config(tmp_path, **kw) -> ServerConfig:
    return ServerConfig(
        base_dir=tmp_path / "data",
        hw_capture_device="/dev/video0",
        **kw,
    )


class FakeNetbootClient:
    """Stand-in for :class:`NetbootClient` that records what was asked of it."""

    def __init__(self, reachable=True, status="READY", boot_reply="OK") -> None:
        self._reachable = reachable
        self._status = status
        self._boot_reply = boot_reply
        self.boot_calls: list[str] = []

    def reachable(self) -> bool:
        return self._reachable

    def status(self) -> str:
        return self._status

    def boot(self, rom_path: str) -> str:
        self.boot_calls.append(rom_path)
        return self._boot_reply


def _patch_netboot(monkeypatch, inst, **kw) -> FakeNetbootClient:
    fake = FakeNetbootClient(**kw)
    monkeypatch.setattr(inst, "netboot_client", lambda timeout=None: fake)
    return fake


def _make_hw_inst(tmp_path, **spec_kw) -> HardwareInstance:
    return HardwareInstance(
        HardwareInstanceSpec(instance_id="hw-001", **spec_kw),
        make_hw_config(tmp_path),
    )


# ---------------------------------------------------------------------------
# HardwareManager bba_ip / bba_port persistence
# ---------------------------------------------------------------------------
class TestHardwareManagerBba:
    def test_register_defaults_from_config(self, tmp_path):
        cfg = make_hw_config(tmp_path, hw_bba_ip="192.168.1.61", hw_bba_port=4242)
        mgr = HardwareManager(cfg)
        status = mgr.register("hw-001")
        assert status["bba_ip"] == "192.168.1.61"
        assert status["bba_port"] == 4242

    def test_register_explicit_bba(self, tmp_path):
        mgr = HardwareManager(make_hw_config(tmp_path))
        status = mgr.register("hw-001", bba_ip="10.0.0.5", bba_port=9999)
        assert status["bba_ip"] == "10.0.0.5"
        assert status["bba_port"] == 9999

    def test_update_bba_persists(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register("hw-001")
        status = mgr.update("hw-001", bba_ip="192.168.1.61", bba_port=4242)
        assert status["bba_ip"] == "192.168.1.61"
        assert status["bba_port"] == 4242
        # Reload from disk to confirm persistence.
        reloaded = HardwareManager(cfg).status("hw-001")
        assert reloaded["bba_ip"] == "192.168.1.61"
        assert reloaded["bba_port"] == 4242

    def test_bba_persists_across_reload(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        HardwareManager(cfg).register("hw-001", bba_ip="192.168.1.61")
        status = HardwareManager(cfg).status("hw-001")
        assert status["bba_ip"] == "192.168.1.61"

    def test_bba_defaults_to_none(self, tmp_path):
        mgr = HardwareManager(make_hw_config(tmp_path))
        status = mgr.register("hw-001")
        assert status["bba_ip"] is None
        assert status["bba_port"] == 4242


# ---------------------------------------------------------------------------
# HardwareInstance.rom_load (netboot)
# ---------------------------------------------------------------------------
class TestHardwareRomLoad:
    def test_rom_load_success(self, tmp_path, monkeypatch):
        inst = _make_hw_inst(tmp_path, bba_ip="192.168.1.61")
        fake = _patch_netboot(monkeypatch, inst, reachable=True, status="READY",
                              boot_reply="OK")
        result = inst.rom_load("sdc:/games/Foo.iso")
        assert result["booted"] is True
        assert result["reply"] == "OK"
        assert result["status"] == "READY"
        assert result["rom_path"] == "sdc:/games/Foo.iso"
        assert result["bba"] == "192.168.1.61:4242"
        assert fake.boot_calls == ["sdc:/games/Foo.iso"]

    def test_rom_load_busy(self, tmp_path, monkeypatch):
        inst = _make_hw_inst(tmp_path, bba_ip="192.168.1.61")
        fake = _patch_netboot(monkeypatch, inst, reachable=True, status="BUSY")
        result = inst.rom_load("sdc:/games/Foo.iso")
        assert result["booted"] is False
        assert result["status"] == "BUSY"
        assert "in progress" in result["note"]
        assert fake.boot_calls == []

    def test_rom_load_invalid_path_raises_before_netboot(self, tmp_path, monkeypatch):
        inst = _make_hw_inst(tmp_path, bba_ip="192.168.1.61")
        fake = _patch_netboot(monkeypatch, inst, reachable=True, status="READY")
        with pytest.raises(ValueError):
            inst.rom_load("/host/path.iso")
        assert fake.boot_calls == []

    def test_rom_load_listener_down_tries_return_to_swiss(self, tmp_path, monkeypatch):
        # Listener down -> reachable() False -> _return_to_swiss() presses
        # A+Z+START on port 0. With no serial device configured this raises
        # ControllerError (there is nothing to press the buttons with).
        inst = _make_hw_inst(tmp_path, bba_ip="192.168.1.61")
        _patch_netboot(monkeypatch, inst, reachable=False)
        with pytest.raises(ControllerError) as exc:
            inst.rom_load("sdc:/games/Foo.iso")
        assert "no serial device" in str(exc.value)

    def test_rom_load_missing_bba_ip_raises_netboot_error(self, tmp_path):
        inst = _make_hw_inst(tmp_path)  # no bba_ip
        with pytest.raises(NetbootError):
            inst.rom_load("sdc:/games/Foo.iso")

    def test_rom_load_reports_netboot_reply(self, tmp_path, monkeypatch):
        inst = _make_hw_inst(tmp_path, bba_ip="192.168.1.61")
        _patch_netboot(monkeypatch, inst, reachable=True, status="READY",
                       boot_reply="ERR disk not found")
        result = inst.rom_load("sdc:/games/Foo.iso")
        assert result["booted"] is False
        assert result["reply"] == "ERR disk not found"

    def test_rom_load_returns_to_swiss_then_boots(self, tmp_path, monkeypatch):
        # Listener down -> _return_to_swiss presses A+Z+START -> _wait_for_swiss
        # polls until the listener is back -> boots the requested ROM.
        inst = _make_hw_inst(tmp_path, bba_ip="192.168.1.61")
        fake_ctl = FakeController()
        inst._controller = fake_ctl
        boot_calls: list[str] = []

        class F:
            def __init__(self) -> None:
                self.n = 0

            def reachable(self) -> bool:
                self.n += 1
                # Down on the initial probe, then back up for _wait_for_swiss.
                return self.n >= 2

            def status(self) -> str:
                return "READY"

            def boot(self, path: str) -> str:
                boot_calls.append(path)
                return "OK"

        monkeypatch.setattr(inst, "netboot_client", lambda timeout=None: F())
        monkeypatch.setattr("gc_mcp.instances.hardware_instance._RETURN_TO_SWISS_POLL", 0.001)
        result = inst.rom_load("sdc:/games/Foo.iso")
        assert result["booted"] is True
        presses = [c for c in fake_ctl.calls if c[0] == "press"]
        assert presses == [("press", 0, "A"), ("press", 0, "Z"), ("press", 0, "START")]
        assert boot_calls == ["sdc:/games/Foo.iso"]

    def test_wait_for_swiss_retries_until_reachable(self, tmp_path, monkeypatch):
        inst = _make_hw_inst(tmp_path)
        calls: list[int] = []

        class F:
            def __init__(self) -> None:
                self.n = 0

            def reachable(self) -> bool:
                calls.append(1)
                self.n += 1
                return self.n >= 3

        monkeypatch.setattr("gc_mcp.instances.hardware_instance._RETURN_TO_SWISS_POLL", 0.001)
        inst._wait_for_swiss(F())
        assert len(calls) == 3

    def test_wait_for_swiss_times_out(self, tmp_path, monkeypatch):
        inst = _make_hw_inst(tmp_path)

        class F:
            def reachable(self) -> bool:
                return False

        monkeypatch.setattr("gc_mcp.instances.hardware_instance._RETURN_TO_SWISS_POLL", 0.001)
        monkeypatch.setattr("gc_mcp.instances.hardware_instance.RETURN_TO_SWISS_WAIT", 0.05)
        with pytest.raises(NetbootError) as exc:
            inst._wait_for_swiss(F())
        assert "never came back up" in str(exc.value)


# ---------------------------------------------------------------------------
# GameCubeHWPlatform controller delegation
# ---------------------------------------------------------------------------
class FakeController:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def press(self, port, button) -> None:
        self.calls.append(("press", port, button))

    def release(self, port, button) -> None:
        self.calls.append(("release", port, button))

    def set_stick(self, port, stick, x, y) -> None:
        self.calls.append(("set_stick", port, stick, x, y))

    def set_trigger(self, port, trigger, value) -> None:
        self.calls.append(("set_trigger", port, trigger, value))

    def get_state(self, port):
        raise NotImplementedError

    def reset(self, port) -> None:
        self.calls.append(("reset", port))


class TestHardwareControllerDelegation:
    def test_platform_delegates_to_instance_controller(self, tmp_path):
        inst = _make_hw_inst(tmp_path)
        fake = FakeController()
        inst._controller = fake
        plat = GameCubeHWPlatform(inst)
        plat.controller_press(0, "A")
        plat.controller_release(0, "A")
        plat.controller_set_stick(0, "main", 1.0, 0.0)
        plat.controller_set_trigger(0, "L", 0.5)
        plat.controller_reset(0)
        assert ("press", 0, "A") in fake.calls
        assert ("release", 0, "A") in fake.calls
        assert ("set_stick", 0, "main", 1.0, 0.0) in fake.calls
        assert ("set_trigger", 0, "L", 0.5) in fake.calls
        assert ("reset", 0) in fake.calls

    def test_controller_press_without_serial_device_raises(self, tmp_path):
        # The real backend needs a serial device for the port; without one it
        # raises ControllerError rather than silently doing nothing.
        inst = _make_hw_inst(tmp_path)
        plat = GameCubeHWPlatform(inst)
        with pytest.raises(ControllerError) as exc:
            plat.controller_press(0, "A")
        assert "no serial device" in str(exc.value)

    def test_return_to_swiss_presses_a_z_start_together(self, tmp_path):
        # _return_to_swiss presses A, Z and START, holds them, then releases in
        # reverse order. Use a fake controller so no serial device is needed.
        inst = _make_hw_inst(tmp_path)
        fake = FakeController()
        inst._controller = fake
        inst._return_to_swiss()
        presses = [c for c in fake.calls if c[0] == "press"]
        releases = [c for c in fake.calls if c[0] == "release"]
        assert presses == [("press", 0, "A"), ("press", 0, "Z"), ("press", 0, "START")]
        assert releases == [("release", 0, "START"), ("release", 0, "Z"), ("release", 0, "A")]


# ---------------------------------------------------------------------------
# Server tool layer
# ---------------------------------------------------------------------------
@pytest.fixture
def hw(monkeypatch, tmp_path):
    cfg = make_hw_config(tmp_path)
    mgr = HardwareManager(cfg)
    monkeypatch.setattr(server, "hardware", mgr)
    monkeypatch.setattr(server, "config", cfg)
    return mgr


class TestHardwareRomLoadTools:
    def test_register_with_bba_ip(self, hw):
        res = server.gc_hardware_register("hw-001", bba_ip="192.168.1.61")
        assert res["bba_ip"] == "192.168.1.61"
        assert res["bba_port"] == 4242

    def test_update_bba_ip(self, hw):
        server.gc_hardware_register("hw-001")
        res = server.gc_hardware_update("hw-001", bba_ip="192.168.1.61")
        assert res["bba_ip"] == "192.168.1.61"

    def test_platform_list_surfaces_bba(self, hw):
        server.gc_hardware_register("hw-001", bba_ip="192.168.1.61")
        platforms = server.gc_platform_list()
        entry = next(p for p in platforms if p["kind"] == "gamecube-hw")
        assert entry["bba_ip"] == "192.168.1.61"
        assert entry["bba_port"] == 4242

    def test_rom_load_tool_on_hardware(self, hw, monkeypatch):
        server.gc_hardware_register("hw-001", bba_ip="192.168.1.61")
        inst = hw.get("hw-001")
        fake = _patch_netboot(monkeypatch, inst, reachable=True, status="READY",
                              boot_reply="OK")
        res = server.gc_rom_load("hw-001", "sdc:/games/Foo.iso")
        assert res["booted"] is True
        assert fake.boot_calls == ["sdc:/games/Foo.iso"]

    def test_rom_load_tool_missing_bba_ip(self, hw):
        server.gc_hardware_register("hw-001")
        with pytest.raises(NetbootError):
            server.gc_rom_load("hw-001", "sdc:/games/Foo.iso")


# ---------------------------------------------------------------------------
# Dolphin relaunch (unified gc_rom_load on a Dolphin instance)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_dolphin(tmp_path):
    script = tmp_path / "dolphin-emu"
    log = tmp_path / "invocation.txt"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            echo "$@" > "{log}"
            while :; do sleep 0.1; done
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def make_dolphin_config(tmp_path, binary) -> ServerConfig:
    return ServerConfig(
        base_dir=tmp_path / "data",
        dolphin_binary=str(binary),
        gpu_available=False,
        capture_backend="framedump",
        startup_timeout=2.0,
    )


class TestDolphinRelaunch:
    def test_relaunch_swaps_rom_and_restarts(self, tmp_path, fake_dolphin):
        rom1 = tmp_path / "game1.iso"
        rom1.write_bytes(b"\x00" * 1024)
        rom2 = tmp_path / "game2.iso"
        rom2.write_bytes(b"\x00" * 1024)

        cfg = make_dolphin_config(tmp_path, fake_dolphin)
        manager = InstanceManager(cfg)
        manager.start("test1", rom1, capture_backend="framedump")

        try:
            inst = manager.get("test1")
            assert inst.running is True

            result = inst.relaunch(rom2)
            assert result["running"] is True
            assert inst.spec.rom_path == rom2

            # The new process must have been launched with the new ROM.
            invocation = (tmp_path / "invocation.txt").read_text()
            assert f"--exec={rom2}" in invocation
            assert f"--exec={rom1}" not in invocation
        finally:
            manager.shutdown()

    def test_relaunch_missing_rom_raises(self, tmp_path, fake_dolphin):
        rom = tmp_path / "game.iso"
        rom.write_bytes(b"\x00" * 1024)
        cfg = make_dolphin_config(tmp_path, fake_dolphin)
        manager = InstanceManager(cfg)
        manager.start("test1", rom, capture_backend="framedump")
        try:
            inst = manager.get("test1")
            with pytest.raises(NotFoundError):
                inst.relaunch(tmp_path / "nope.iso")
        finally:
            manager.shutdown()

    def test_platform_rom_load_delegates_to_relaunch(self, tmp_path, fake_dolphin):
        rom1 = tmp_path / "game1.iso"
        rom1.write_bytes(b"\x00" * 1024)
        rom2 = tmp_path / "game2.iso"
        rom2.write_bytes(b"\x00" * 1024)
        cfg = make_dolphin_config(tmp_path, fake_dolphin)
        manager = InstanceManager(cfg)
        manager.start("test1", rom1, capture_backend="framedump")
        try:
            inst = manager.get("test1")
            plat = server.DolphinPlatform(inst)
            result = plat.rom_load(str(rom2))
            assert result["running"] is True
            assert inst.spec.rom_path == rom2
        finally:
            manager.shutdown()
