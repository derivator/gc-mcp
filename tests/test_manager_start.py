"""End-to-end tests for instance startup using a fake Dolphin binary."""

from __future__ import annotations

import os
import textwrap
import time

import pytest

from gc_mcp.config import ServerConfig
from gc_mcp.errors import NotFoundError, StartupError
from gc_mcp.instances.manager import InstanceManager


@pytest.fixture
def fake_dolphin(tmp_path):
    """A fake `dolphin-emu` that just sleeps, writing its args to a file."""
    script = tmp_path / "dolphin-emu"
    log = tmp_path / "invocation.txt"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            # Record invocation for the test to assert on.
            echo "$@" > "{log}"
            # Keep running so the instance stays "running".
            while :; do sleep 0.1; done
            """
        ),
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def make_config(tmp_path, binary) -> ServerConfig:
    return ServerConfig(
        base_dir=tmp_path / "data",
        dolphin_binary=str(binary),
        gpu_available=False,
        capture_backend="framedump",
        startup_timeout=2.0,
    )


def test_start_writes_config_and_fifos(tmp_path, fake_dolphin):
    rom = tmp_path / "game.iso"
    rom.write_bytes(b"\x00" * 1024)

    cfg = make_config(tmp_path, fake_dolphin)
    manager = InstanceManager(cfg)

    status = manager.start(
        "test1",
        rom,
        controller_ports=(0, 1),
        bba_mode="ipc",
        bba_mac="00:09:bf:00:00:01",
        capture_backend="framedump",
        video_backend="OGL",
    )

    try:
        assert status["running"] is True
        assert status["pid"] is not None

        user_dir = cfg.instances_dir / "test1"
        # Dolphin.ini written.
        ini = (user_dir / "Config" / "Dolphin.ini").read_text()
        assert "SIDevice0 = 6" in ini
        assert "SIDevice1 = 6" in ini
        assert "SIDevice2 = 0" in ini
        assert "SerialPort1 = 14" in ini  # ipc mode
        # GCPadNew.ini written.
        pad = (user_dir / "Config" / "GCPadNew.ini").read_text()
        assert "Device = Pipe/0/ctrl1" in pad
        # FIFOs created.
        for port in (0, 1):
            fifo = user_dir / "Pipes" / f"ctrl{port + 1}"
            assert fifo.exists()
            assert fifo.stat().st_mode & 0o10000
    finally:
        manager.shutdown()


def test_start_sets_info(tmp_path, fake_dolphin):
    rom = tmp_path / "game.iso"
    rom.write_bytes(b"\x00" * 1024)

    cfg = make_config(tmp_path, fake_dolphin)
    manager = InstanceManager(cfg)

    status = manager.start("test1", rom, info="dev rig", capture_backend="framedump")
    try:
        assert status["info"] == "dev rig"
        assert manager.list()[0]["info"] == "dev rig"
    finally:
        manager.shutdown()


def test_start_missing_rom_raises(tmp_path, fake_dolphin):
    cfg = make_config(tmp_path, fake_dolphin)
    manager = InstanceManager(cfg)
    with pytest.raises(NotFoundError):
        manager.start("x", tmp_path / "nope.iso")


def test_start_missing_dolphin_raises(tmp_path):
    rom = tmp_path / "game.iso"
    rom.write_bytes(b"\x00" * 1024)

    cfg = ServerConfig(
        base_dir=tmp_path / "data",
        dolphin_binary=str(tmp_path / "does-not-exist"),
        gpu_available=False,
        capture_backend="framedump",
    )
    manager = InstanceManager(cfg)
    with pytest.raises(StartupError):
        manager.start("y", rom)


def test_start_immediate_exit_reports_startup_error(tmp_path):
    """If the fake dolphin exits at once, StartupError should be raised."""
    rom = tmp_path / "game.iso"
    rom.write_bytes(b"\x00" * 1024)

    exiting = tmp_path / "dolphin-exit"
    exiting.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    exiting.chmod(0o755)

    cfg = ServerConfig(
        base_dir=tmp_path / "data",
        dolphin_binary=str(exiting),
        gpu_available=False,
        capture_backend="framedump",
    )
    manager = InstanceManager(cfg)
    with pytest.raises(StartupError):
        manager.start("z", rom)
