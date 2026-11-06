"""Tests for the Dolphin per-instance configuration builder."""

from __future__ import annotations

import os

import pytest

from gc_mcp.config import IPL_REGIONS
from gc_mcp.errors import ConfigError
from gc_mcp.instances.config_builder import (
    BBA_MODE_EXI,
    InstanceConfig,
    InstanceConfigBuilder,
    SI_DEVICE_GC_CONTROLLER,
    SI_DEVICE_NONE,
    normalize_bba_mode,
    normalize_mac,
)


class TestNormalizeBbaMode:
    def test_known_modes(self) -> None:
        assert normalize_bba_mode("ipc") == BBA_MODE_EXI["ipc"]
        assert normalize_bba_mode("hle") == BBA_MODE_EXI["hle"]
        assert normalize_bba_mode("xlink") == BBA_MODE_EXI["xlink"]
        assert normalize_bba_mode("tap") == BBA_MODE_EXI["tap"]
        assert normalize_bba_mode("tapserver") == BBA_MODE_EXI["tapserver"]
        assert normalize_bba_mode("modem") == BBA_MODE_EXI["modem"]

    def test_none_disables(self) -> None:
        assert normalize_bba_mode(None) is None
        assert normalize_bba_mode("") is None
        assert normalize_bba_mode("none") is None
        assert normalize_bba_mode("off") is None

    def test_case_insensitive(self) -> None:
        assert normalize_bba_mode("IPC") == BBA_MODE_EXI["ipc"]

    def test_unknown_raises(self) -> None:
        with pytest.raises(ConfigError):
            normalize_bba_mode("bogus")


class TestNormalizeMac:
    def test_generate_default_prefix(self) -> None:
        mac = normalize_mac(None)
        assert mac.startswith("00:09:bf")
        assert len(mac) == 17

    def test_custom_prefix(self) -> None:
        mac = normalize_mac(None, default_prefix="00:17:ab")
        assert mac.startswith("00:17:ab")

    def test_valid_explicit(self) -> None:
        assert normalize_mac("00:09:bf:12:34:56") == "00:09:bf:12:34:56"

    def test_invalid_explicit(self) -> None:
        with pytest.raises(ConfigError):
            normalize_mac("de:ad:be:ef:00:00")


class TestConfigBuilder:
    def _build(self, cfg: InstanceConfig) -> None:
        InstanceConfigBuilder(cfg).write()

    def test_dolphin_ini_si_devices(self, tmp_path) -> None:
        cfg = InstanceConfig(user_dir=tmp_path, controller_ports=(0, 2))
        self._build(cfg)
        text = (tmp_path / "Config" / "Dolphin.ini").read_text()
        assert f"SIDevice0 = {SI_DEVICE_GC_CONTROLLER}" in text
        assert f"SIDevice1 = {SI_DEVICE_NONE}" in text
        assert f"SIDevice2 = {SI_DEVICE_GC_CONTROLLER}" in text
        assert f"SIDevice3 = {SI_DEVICE_NONE}" in text

    def test_dolphin_ini_bba(self, tmp_path) -> None:
        cfg = InstanceConfig(user_dir=tmp_path, bba_mode="ipc", bba_mac="00:09:bf:00:00:01")
        self._build(cfg)
        text = (tmp_path / "Config" / "Dolphin.ini").read_text()
        assert f"SerialPort1 = {BBA_MODE_EXI['ipc']}" in text
        assert "BBA_MAC = 00:09:bf:00:00:01" in text

    def test_dolphin_ini_no_bba(self, tmp_path) -> None:
        cfg = InstanceConfig(user_dir=tmp_path)
        self._build(cfg)
        text = (tmp_path / "Config" / "Dolphin.ini").read_text()
        assert "SerialPort1" not in text

    def test_gcpad_new_sections(self, tmp_path) -> None:
        cfg = InstanceConfig(user_dir=tmp_path, controller_ports=(0, 1))
        self._build(cfg)
        text = (tmp_path / "Config" / "GCPadNew.ini").read_text()
        assert "[GCPad1]" in text
        assert "[GCPad2]" in text
        assert "[GCPad3]" not in text
        assert "Device = Pipe/0/ctrl1" in text
        assert "Device = Pipe/0/ctrl2" in text
        assert "Buttons/A = `Button A`" in text
        assert "Main Stick/Up = `Axis MAIN Y +`" in text
        assert "C-Stick/Left = `Axis C X -`" in text

    def test_fifos_created(self, tmp_path) -> None:
        cfg = InstanceConfig(user_dir=tmp_path, controller_ports=(0, 1, 2, 3))
        self._build(cfg)
        for port in range(4):
            path = tmp_path / "Pipes" / f"ctrl{port + 1}"
            assert path.exists()
            assert os.path.exists(path)
            assert path.stat().st_mode & 0o10000  # S_IFIFO

    def test_extra_core(self, tmp_path) -> None:
        cfg = InstanceConfig(user_dir=tmp_path, extra_core={"SkipIPL": "False"})
        self._build(cfg)
        text = (tmp_path / "Config" / "Dolphin.ini").read_text()
        assert "SkipIPL = False" in text

    def test_gfx_ini(self, tmp_path) -> None:
        cfg = InstanceConfig(user_dir=tmp_path, video_backend="Vulkan", internal_resolution=2)
        self._build(cfg)
        text = (tmp_path / "Config" / "GFX.ini").read_text()
        assert "GFXBackend = Vulkan" in text
        assert "InternalResolution = 2" in text

    def test_dolphin_ini_background_input(self, tmp_path) -> None:
        # Headless (--batch) mode never focuses the render window, so Dolphin
        # closes its input gate and drops all controller input unless background
        # input is enabled. The builder must always write [Input] BackgroundInput.
        cfg = InstanceConfig(user_dir=tmp_path)
        self._build(cfg)
        text = (tmp_path / "Config" / "Dolphin.ini").read_text()
        assert "[Input]" in text
        assert "BackgroundInput = True" in text

    def test_dolphin_ini_hotkeys_require_focus(self, tmp_path) -> None:
        # In headless (--batch) mode the render window is never Qt-active, so
        # Dolphin closes its *hotkey* input gate (MAIN_FOCUSED_HOTKEYS, default
        # true) and silently drops pause/save/load/step hotkeys. The builder must
        # write [General] HotkeysRequireFocus = False to keep that gate open.
        cfg = InstanceConfig(user_dir=tmp_path)
        self._build(cfg)
        text = (tmp_path / "Config" / "Dolphin.ini").read_text()
        assert "[General]" in text
        assert "HotkeysRequireFocus = False" in text

    def test_hotkeys_ini_device_line(self, tmp_path) -> None:
        # The generated Hotkeys.ini must bind hotkeys to a device that actually
        # resolves in a headless X11 build (XInput2 keyboard, named after the
        # pointer). The stock "Keyboard" device is not registered and would leave
        # every hotkey unbound.
        cfg = InstanceConfig(user_dir=tmp_path)
        self._build(cfg)
        text = (tmp_path / "Config" / "Hotkeys.ini").read_text()
        assert text.startswith("[Hotkeys]\n")
        assert "Device = XInput2/0/Virtual core pointer" in text
        assert "General/Toggle Pause = F10" in text
        assert "Frame Advance/Frame Advance = F8" in text

    def test_logger_ini_enables_osreport(self, tmp_path) -> None:
        # A fresh Dolphin --user data directory ships an empty Logger.ini, so
        # SYS_Report/OSReport output from a ROM never reaches the log file. The
        # builder must write a Logger.ini that enables OSREPORT (and MASTER) so a
        # running ROM's console output is observable.
        cfg = InstanceConfig(user_dir=tmp_path)
        self._build(cfg)
        text = (tmp_path / "Config" / "Logger.ini").read_text()
        assert "[Options]" in text
        assert "WriteToFile = True" in text
        assert "WriteToConsole = True" in text
        assert "WriteToWindow = True" in text
        assert "Verbosity = 4" in text
        assert "[Logs]" in text
        assert "OSREPORT = True" in text
        assert "OSREPORT_HLE = True" in text
        assert "MASTER = True" in text


class TestIpl:
    """Tests for GameCube IPL (boot ROM) installation."""

    def _make_ipl_dir(self, tmp_path, regions: tuple[str, ...] = IPL_REGIONS) -> "os.PathLike":
        """Create an IPL folder with a small fake ``<REGION>/IPL.bin`` per region."""
        ipl_dir = tmp_path / "ipl"
        for region in regions:
            d = ipl_dir / region
            d.mkdir(parents=True, exist_ok=True)
            (d / "IPL.bin").write_bytes(f"fake-{region}-ipl".encode())
        return ipl_dir

    def test_boot_ipl_installs_and_skips(self, tmp_path) -> None:
        # boot_ipl=True copies each available region's IPL into GC/<region>/ and
        # forces [Core] SkipIPL = False so the boot ROM/logo runs.
        ipl_dir = self._make_ipl_dir(tmp_path)
        cfg = InstanceConfig(user_dir=tmp_path, boot_ipl=True, ipl_dir=ipl_dir)
        InstanceConfigBuilder(cfg).write()

        text = (tmp_path / "Config" / "Dolphin.ini").read_text()
        assert "SkipIPL = False" in text
        for region in IPL_REGIONS:
            dest = tmp_path / "GC" / region / "IPL.bin"
            assert dest.is_file()
            assert dest.read_bytes() == f"fake-{region}-ipl".encode()

    def test_boot_ipl_all_regions(self, tmp_path) -> None:
        # Even if only one region's IPL is present, the others are simply skipped
        # and the installed region(s) still get written.
        ipl_dir = self._make_ipl_dir(tmp_path, regions=("USA",))
        cfg = InstanceConfig(user_dir=tmp_path, boot_ipl=True, ipl_dir=ipl_dir)
        InstanceConfigBuilder(cfg).write()

        assert (tmp_path / "GC" / "USA" / "IPL.bin").is_file()
        assert not (tmp_path / "GC" / "JAP" / "IPL.bin").exists()
        assert not (tmp_path / "GC" / "EUR" / "IPL.bin").exists()

    def test_boot_ipl_requires_ipl_dir(self, tmp_path) -> None:
        # boot_ipl=True with no configured IPL folder must fail loudly.
        cfg = InstanceConfig(user_dir=tmp_path, boot_ipl=True, ipl_dir=None)
        with pytest.raises(ConfigError):
            InstanceConfigBuilder(cfg).write()

    def test_boot_ipl_requires_files(self, tmp_path) -> None:
        # boot_ipl=True with a folder that has no IPL files must fail.
        ipl_dir = tmp_path / "ipl"
        ipl_dir.mkdir()
        cfg = InstanceConfig(user_dir=tmp_path, boot_ipl=True, ipl_dir=ipl_dir)
        with pytest.raises(ConfigError):
            InstanceConfigBuilder(cfg).write()

    def test_boot_ipl_default_does_nothing(self, tmp_path) -> None:
        # Default (boot_ipl=False) must not write SkipIPL or create GC/ dirs.
        cfg = InstanceConfig(user_dir=tmp_path)
        InstanceConfigBuilder(cfg).write()

        text = (tmp_path / "Config" / "Dolphin.ini").read_text()
        assert "SkipIPL" not in text
        assert not (tmp_path / "GC").exists()

    def test_result_reports_boot_ipl(self, tmp_path) -> None:
        ipl_dir = self._make_ipl_dir(tmp_path)
        cfg = InstanceConfig(user_dir=tmp_path, boot_ipl=True, ipl_dir=ipl_dir)
        result = InstanceConfigBuilder(cfg).write()
        assert result["boot_ipl"] is True
        assert result["ipl_dir"] == str(ipl_dir)
