"""Tests for real GameCube hardware capture and the persistent HW registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from gc_mcp import server
from gc_mcp.capture import V4L2Capture, detect_video_devices
from gc_mcp.capture.v4l2 import (
    _WARMED,
    _parse_can_capture,
    _parse_card_type,
    _parse_formats,
)
from gc_mcp.config import ServerConfig
from gc_mcp.controllers.hardware import HardwareController
from gc_mcp.errors import (
    AlreadyExistsError,
    CaptureError,
    ControllerError,
    NotFoundError,
)
from gc_mcp.instances import (
    HardwareInstance,
    HardwareInstanceSpec,
    HardwareManager,
)
from gc_mcp.platform import GameCubeHWPlatform, resolve_platform


# ---------------------------------------------------------------------------
# detect_video_devices
# ---------------------------------------------------------------------------
class TestDetectVideoDevices:
    def test_returns_list_of_dicts(self):
        devices = detect_video_devices()
        assert isinstance(devices, list)
        assert all(isinstance(d, dict) for d in devices)
        assert all("device" in d for d in devices)
        assert all({"name", "can_capture", "formats", "error"} <= set(d) for d in devices)

    def test_probes_capability_with_v4l2ctl(self, monkeypatch):
        # A metadata-only node must report can_capture False even though the
        # driver-wide Capabilities line mentions "Video Capture".
        all_out = (
            b"Driver Info:\n"
            b"	Card type        : UGREEN 25773: UGREEN 25773\n"
            b"	Capabilities     : 0x84a00001\n"
            b"		Video Capture\n"
            b"	Device Caps      : 0x04a00000\n"
            b"		Metadata Capture\n"
        )
        fmt_out = b"ioctl: VIDIOC_ENUM_FMT\n\tType: Video Capture\n"

        def fake_run(cmd, **kwargs):
            assert cmd[0].endswith("v4l2-ctl")
            if "--all" in cmd:
                return FakeProc(0, stdout=all_out)
            if "--list-formats" in cmd:
                return FakeProc(0, stdout=fmt_out)
            raise AssertionError(f"unexpected cmd: {cmd}")

        monkeypatch.setattr("gc_mcp.capture.v4l2.shutil.which", lambda name: "/usr/bin/v4l2-ctl")
        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        monkeypatch.setattr("gc_mcp.capture.v4l2.Path", lambda p: _PathStub(p))

        devices = detect_video_devices()
        assert len(devices) == 1
        entry = devices[0]
        assert entry["device"] == "/dev/video0"
        assert entry["name"] == "UGREEN 25773: UGREEN 25773"
        assert entry["can_capture"] is False
        assert entry["formats"] == []
        assert entry["error"] is None

    def test_video_capture_node_reports_true(self, monkeypatch):
        all_out = (
            b"	Card type        : HD Pro Webcam C920\n"
            b"	Device Caps      : 0x84a00001\n"
        )
        fmt_out = b"	[0]: 'MJPG' (Motion-JPEG)\n	[1]: 'YUYV' (YUYV 4:2:2)\n"

        def fake_run(cmd, **kwargs):
            if "--all" in cmd:
                return FakeProc(0, stdout=all_out)
            return FakeProc(0, stdout=fmt_out)

        monkeypatch.setattr("gc_mcp.capture.v4l2.shutil.which", lambda name: "/usr/bin/v4l2-ctl")
        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        monkeypatch.setattr("gc_mcp.capture.v4l2.Path", lambda p: _PathStub(p))

        entry = detect_video_devices()[0]
        assert entry["can_capture"] is True
        assert entry["formats"] == ["MJPG", "YUYV"]


class _PathStub:
    """Stand-in for pathlib.Path so detect_video_devices sees /dev/video0."""

    def __init__(self, value) -> None:
        self._value = value

    def is_dir(self) -> bool:
        return True

    def glob(self, pattern):
        return iter([_PathStub("/dev/video0")])

    def exists(self) -> bool:
        return True

    def __str__(self) -> str:
        return self._value


class TestParseHelpers:
    def test_parse_card_type(self):
        assert _parse_card_type("	Card type        : UGREEN 25773: UGREEN 25773\n") == "UGREEN 25773: UGREEN 25773"
        assert _parse_card_type("no card here") is None

    def test_parse_can_capture_prefers_device_caps(self):
        # Device Caps lacks Video Capture (0x1) even though the capabilities
        # list mentions it -> must be False.
        text = "	Capabilities     : 0x84a00001\n		Video Capture\n	Device Caps      : 0x04a00000\n"
        assert _parse_can_capture(text) is False

    def test_parse_can_capture_true(self):
        text = "	Device Caps      : 0x84a00001\n"
        assert _parse_can_capture(text) is True

    def test_parse_can_capture_fallback(self):
        assert _parse_can_capture("Video Capture") is True
        assert _parse_can_capture("Metadata Capture") is False

    def test_parse_formats(self):
        text = "	[0]: 'MJPG' (Motion-JPEG, ...)\n	[1]: 'YUYV' (YUYV 4:2:2)\n"
        assert _parse_formats(text) == ["MJPG", "YUYV"]
        assert _parse_formats("Type: Video Capture\n") == []


# ---------------------------------------------------------------------------
# V4L2Capture (ffmpeg path)
# ---------------------------------------------------------------------------
class FakeProc:
    def __init__(self, returncode=0, stderr=b"", stdout=b"") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


@pytest.fixture
def fake_device(tmp_path):
    device = tmp_path / "video0"
    device.write_bytes(b"\x00")
    return device


class TestV4L2Capture:
    @pytest.fixture(autouse=True)
    def _clear_warm_state(self):
        # The warm-up cache is module-level; reset it between tests so a device
        # "warmed" in one test does not leak into another.
        _WARMED.clear()
        yield
        _WARMED.clear()

    def test_capture_writes_png_via_ffmpeg(self, fake_device, tmp_path, monkeypatch):
        dest = tmp_path / "frame.png"
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            # ffmpeg writes the PNG to the last argument.
            Path(cmd[-1]).write_bytes(b"fake-png")
            return FakeProc(0)

        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)

        cap = V4L2Capture(device=str(fake_device), width=640, height=480, format="mjpg")
        result = cap.capture(dest)
        assert result == dest
        assert dest.read_bytes() == b"fake-png"
        # The command must target the device and output one PNG frame.
        assert any("v4l2" in arg for arg in calls[0])
        assert calls[0][-1] == str(dest)
        assert "-frames:v" in calls[0]

    def test_capture_missing_device_raises(self, tmp_path):
        cap = V4L2Capture(device=str(tmp_path / "nope"), width=640, height=480)
        with pytest.raises(CaptureError):
            cap.capture(tmp_path / "out.png")

    def test_capture_ffmpeg_failure_raises(self, fake_device, tmp_path, monkeypatch):
        def fake_run(cmd, **kwargs):
            return FakeProc(1, b"device busy")

        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        cap = V4L2Capture(device=str(fake_device))
        with pytest.raises(CaptureError):
            cap.capture(tmp_path / "out.png")

    def test_configure_updates(self):
        cap = V4L2Capture(device="/dev/video0")
        result = cap.configure(width=1280, height=720, format="yuyv", device="/dev/video1")
        assert cap.width == 1280
        assert cap.height == 720
        assert cap.format == "yuyv"
        assert cap.device == "/dev/video1"
        assert result["backend"] == "v4l2"
        assert result["device"] == "/dev/video1"

    def test_configure_warmup(self):
        cap = V4L2Capture(device="/dev/video0")
        result = cap.configure(warmup_seconds=7.0, warmup_ttl=120.0)
        assert cap.warmup_seconds == 7.0
        assert cap.warmup_ttl == 120.0
        assert result["warmup_seconds"] == 7.0
        assert result["warmup_ttl"] == 120.0

    def test_warmup_cold_uses_sustained_stream(self, fake_device, tmp_path, monkeypatch):
        """A cold device with warmup enabled streams for warmup_seconds first."""
        dest = tmp_path / "frame.png"
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"warm-png")
            return FakeProc(0)

        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        cap = V4L2Capture(device=str(fake_device), width=640, height=480,
                          format="mjpg", warmup_seconds=5.0)
        result = cap.capture(dest)
        assert result == dest
        assert dest.read_bytes() == b"warm-png"
        # The first command must be a sustained stream (-t) writing the last
        # frame (-update 1), not a single-frame grab.
        assert "-t" in calls[0]
        assert "5.0" in calls[0]
        assert "-update" in calls[0]
        assert calls[0][-1] == str(dest)
        # The device should now be marked warm.
        assert not cap._needs_warmup(cap._warm_key())

    def test_warmup_disabled_by_default(self, fake_device, tmp_path, monkeypatch):
        """With warmup_seconds=0 the fast single-frame path is used."""
        dest = tmp_path / "frame.png"
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"png")
            return FakeProc(0)

        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        cap = V4L2Capture(device=str(fake_device), width=640, height=480, format="mjpg")
        cap.capture(dest)
        assert "-frames:v" in calls[0]
        assert "-update" not in calls[0]

    def test_warmup_skipped_when_already_warm(self, fake_device, tmp_path, monkeypatch):
        """A device already warmed this process takes the fast path."""
        dest = tmp_path / "frame.png"
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"png")
            return FakeProc(0)

        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        cap = V4L2Capture(device=str(fake_device), width=640, height=480,
                          format="mjpg", warmup_seconds=5.0)
        # Warm it up once (first call), then a second capture must be fast.
        cap.capture(dest)
        cap.capture(dest)
        # First call = warm-up (-t), second call = single frame (-frames:v).
        assert "-t" in calls[0]
        assert "-frames:v" in calls[1]

    def test_warmup_after_ttl_expires(self, fake_device, tmp_path, monkeypatch):
        """A device warmed past warmup_ttl must be warmed again."""
        dest = tmp_path / "frame.png"
        calls: list[list[str]] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"png")
            return FakeProc(0)

        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        cap = V4L2Capture(device=str(fake_device), width=640, height=480,
                          format="mjpg", warmup_seconds=5.0, warmup_ttl=60.0)
        cap.capture(dest)
        # Pretend the TTL has elapsed.
        _WARMED[cap._warm_key()] -= 61
        assert cap._needs_warmup(cap._warm_key()) is True
        cap.capture(dest)
        assert "-t" in calls[1]

    def test_warmup_ffmpeg_missing_raises(self, fake_device, tmp_path, monkeypatch):
        """Warm-up fails cleanly if ffmpeg is unavailable."""
        monkeypatch.setattr("gc_mcp.capture.v4l2.shutil.which", lambda name: None)
        cap = V4L2Capture(device=str(fake_device), warmup_seconds=5.0)
        with pytest.raises(CaptureError):
            cap.capture(tmp_path / "out.png")


# ---------------------------------------------------------------------------
# HardwareManager (persistence)
# ---------------------------------------------------------------------------
def make_hw_config(tmp_path, **kw) -> ServerConfig:
    return ServerConfig(
        base_dir=tmp_path / "data",
        hw_capture_device="/dev/video0",
        **kw,
    )


class TestHardwareManager:
    def test_register_and_status(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        status = mgr.register(
            "hw-001",
            capture_device="/dev/video0",
            capture={"width": 640, "height": 480, "format": "mjpg"},
            serial_devices=["/dev/ttyUSB0", "/dev/ttyUSB1"],
        )
        assert status["instance_id"] == "hw-001"
        assert status["kind"] == "gamecube-hw"
        assert status["capture_device"] == "/dev/video0"
        assert status["capture"]["width"] == 640
        assert status["serial_devices"] == ["/dev/ttyUSB0", "/dev/ttyUSB1"]
        assert len(mgr) == 1

    def test_register_defaults(self, tmp_path):
        cfg = make_hw_config(tmp_path, hw_capture_width=800, hw_capture_height=600)
        mgr = HardwareManager(cfg)
        status = mgr.register("hw-002")
        assert status["capture_device"] == "/dev/video0"
        assert status["capture"]["width"] == 800
        assert status["capture"]["height"] == 600
        assert status["serial_devices"] == []

    def test_register_applies_warmup_defaults(self, tmp_path):
        cfg = make_hw_config(tmp_path, hw_capture_warmup=7.0, hw_capture_warmup_ttl=90.0)
        mgr = HardwareManager(cfg)
        status = mgr.register("hw-003")
        assert status["capture"]["warmup_seconds"] == 7.0
        assert status["capture"]["warmup_ttl"] == 90.0

    def test_register_duplicate_raises(self, tmp_path):
        mgr = HardwareManager(make_hw_config(tmp_path))
        mgr.register("hw-001")
        with pytest.raises(AlreadyExistsError):
            mgr.register("hw-001")

    def test_too_many_serial_devices_raises(self, tmp_path):
        mgr = HardwareManager(make_hw_config(tmp_path))
        with pytest.raises(ValueError):
            mgr.register("hw-001", serial_devices=["a", "b", "c", "d", "e"])

    def test_get_unknown_raises(self, tmp_path):
        mgr = HardwareManager(make_hw_config(tmp_path))
        with pytest.raises(NotFoundError):
            mgr.get("nope")

    def test_persists_across_reload(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register(
            "hw-001",
            capture_device="/dev/video1",
            capture={"format": "yuyv", "fps": 60},
            serial_devices=["/dev/ttyUSB0"],
        )
        # A fresh manager on the same config must reload the registry.
        mgr2 = HardwareManager(cfg)
        assert mgr2.get_or_none("hw-001") is not None
        status = mgr2.status("hw-001")
        assert status["capture_device"] == "/dev/video1"
        assert status["capture"]["format"] == "yuyv"
        assert status["capture"]["fps"] == 60
        assert status["serial_devices"] == ["/dev/ttyUSB0"]

    def test_update_persists(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register("hw-001")
        status = mgr.update("hw-001", capture_device="/dev/video2",
                            serial_devices=["/dev/ttyUSB0"])
        assert status["capture_device"] == "/dev/video2"
        assert status["serial_devices"] == ["/dev/ttyUSB0"]
        # Reload to confirm persistence.
        assert HardwareManager(cfg).status("hw-001")["capture_device"] == "/dev/video2"

    def test_register_and_update_info(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        status = mgr.register("hw-001", info="living-room console")
        assert status["info"] == "living-room console"
        # info defaults to "" when not supplied.
        assert mgr.register("hw-002")["info"] == ""
        # Update the info field and confirm it persists across a reload.
        updated = mgr.update("hw-001", info="now in the garage")
        assert updated["info"] == "now in the garage"
        reloaded = HardwareManager(cfg).status("hw-001")
        assert reloaded["info"] == "now in the garage"

    def test_remove_persists(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register("hw-001")
        mgr.register("hw-002")
        result = mgr.remove("hw-001")
        assert result["removed"] is True
        assert len(mgr) == 1
        assert HardwareManager(cfg).get_or_none("hw-001") is None

    def test_corrupt_file_is_ignored(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        cfg.hardware_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.hardware_file.write_text("{ not json", encoding="utf-8")
        mgr = HardwareManager(cfg)
        assert len(mgr) == 0

    def test_serial_devices_stored_as_strings(self, tmp_path):
        mgr = HardwareManager(make_hw_config(tmp_path))
        status = mgr.register("hw-001", serial_devices=["/dev/ttyUSB0", 1])
        assert status["serial_devices"] == ["/dev/ttyUSB0", "1"]


# ---------------------------------------------------------------------------
# GameCubeHWPlatform
# ---------------------------------------------------------------------------
class TestGameCubeHWPlatform:
    def test_describe_registered(self, tmp_path):
        inst = HardwareInstance(
            HardwareInstanceSpec(instance_id="hw-001", capture_device="/dev/video0",
                                 serial_devices=["/dev/ttyUSB0"]),
            make_hw_config(tmp_path),
        )
        plat = GameCubeHWPlatform(inst)
        desc = plat.describe()
        assert desc["kind"] == "gamecube-hw"
        assert desc["instance_id"] == "hw-001"
        assert desc["capture_device"] == "/dev/video0"
        assert desc["serial_devices"] == ["/dev/ttyUSB0"]
        assert desc["registered"] is True

    def test_describe_unregistered(self):
        plat = GameCubeHWPlatform(hardware_id="hw-999")
        desc = plat.describe()
        assert desc["registered"] is False
        assert desc["hardware_id"] == "hw-999"

    def test_capture_screenshot(self, tmp_path, monkeypatch):
        cfg = make_hw_config(tmp_path)
        device = tmp_path / "video0"
        device.write_bytes(b"\x00")
        inst = HardwareInstance(
            HardwareInstanceSpec(instance_id="hw-001", capture_device=str(device)),
            cfg,
        )
        plat = GameCubeHWPlatform(inst)

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"png")
            return FakeProc(0)

        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        dest = tmp_path / "shot.png"
        result = plat.capture_screenshot(dest)
        assert result == dest
        assert dest.exists()

    def test_capture_configure_persists(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register("hw-001")
        plat = GameCubeHWPlatform(mgr.get("hw-001"))
        result = plat.capture_configure(width=320, height=240)
        assert result["width"] == 320
        assert result["height"] == 240
        assert HardwareManager(cfg).status("hw-001")["capture"]["width"] == 320

    def test_controller_press_without_serial_device_raises(self, tmp_path):
        inst = HardwareInstance(
            HardwareInstanceSpec(instance_id="hw-001"),
            make_hw_config(tmp_path),
        )
        plat = GameCubeHWPlatform(inst)
        # The controller backend needs a serial device for the port; without one
        # it raises a ControllerError describing the missing device.
        with pytest.raises(ControllerError) as exc:
            plat.controller_press(0, "A")
        assert "no serial device" in str(exc.value)

    def test_startup_verifies_controller_serials(self, tmp_path, monkeypatch):
        """manager.startup() probes each instance's controller and records reports."""
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register("hw-001", serial_devices=["/dev/ttyUSB0", "/dev/ttyUSB1"])

        fake_reports = [
            {"port": 0, "device": "/dev/ttyUSB0", "ok": True,
             "id": "GCCTRL:1 firmware=1 proto=3 report=8 frame=10 baud=115200",
             "error": None},
            {"port": 1, "device": "/dev/ttyUSB1", "ok": True,
             "id": "GCCTRL:1 firmware=1 proto=3 report=8 frame=10 baud=115200",
             "error": None},
        ]
        calls: list = []

        def fake_verify(self, timeout=None):
            calls.append(timeout)
            return list(fake_reports)

        monkeypatch.setattr(HardwareController, "verify", fake_verify)

        result = mgr.startup()
        assert result["hw-001"] == fake_reports
        inst = mgr.get("hw-001")
        assert inst.controller_verified() is True
        assert calls  # verify() was invoked (with a timeout)
        # The status surfaces the verified flag and per-port reports.
        assert inst.status()["controller"]["verified"] is True
        assert inst.status()["controller"]["reports"] == fake_reports

    def test_startup_skips_instances_without_serial(self, tmp_path, monkeypatch):
        """manager.startup() skips instances that have no serial devices to probe."""
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register("hw-noserial")
        mgr.register("hw-serial", serial_devices=["/dev/ttyUSB0"])

        fake = [{"port": 0, "device": "/dev/ttyUSB0", "ok": True,
                 "id": "GCCTRL:1 firmware=1 proto=3 report=8 frame=10 baud=115200",
                 "error": None}]
        monkeypatch.setattr(
            HardwareController, "verify", lambda self, timeout=None: list(fake)
        )

        result = mgr.startup()
        assert "hw-noserial" not in result
        assert "hw-serial" in result
        # An instance with no serial devices is trivially considered verified.
        assert mgr.get("hw-noserial").controller_verified() is True
        assert mgr.get("hw-serial").controller_verified() is True

    def test_controller_verified_edge_cases(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        # No serial devices -> trivially verified.
        no_serial = HardwareInstance(
            HardwareInstanceSpec(instance_id="hw-001"), cfg
        )
        assert no_serial.controller_verified() is True

        inst = HardwareInstance(
            HardwareInstanceSpec(instance_id="hw-002", serial_devices=["/dev/ttyUSB0"]),
            cfg,
        )
        # Reports not yet recorded -> not verified.
        assert inst.controller_verified() is False
        # Wrong number of reports (none recorded) -> not verified.
        inst.set_controller_reports([])
        assert inst.controller_verified() is False
        # Every port ok -> verified.
        inst.set_controller_reports([{"ok": True, "device": "/dev/ttyUSB0"}])
        assert inst.controller_verified() is True
        # Any port not ok -> not verified.
        inst.set_controller_reports([{"ok": False}])
        assert inst.controller_verified() is False

    def test_memory_unsupported(self, tmp_path):
        inst = HardwareInstance(
            HardwareInstanceSpec(instance_id="hw-001"),
            make_hw_config(tmp_path),
        )
        plat = GameCubeHWPlatform(inst)
        with pytest.raises(Exception) as exc:
            plat.memory_read(0x80000000, 4)
        assert "not implemented" in str(exc.value)


# ---------------------------------------------------------------------------
# resolve_platform
# ---------------------------------------------------------------------------
class TestResolvePlatformHardware:
    def test_gamecube_hw_prefix(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register("hw-001")
        plat = resolve_platform("gamecube-hw:hw-001", None, mgr)
        assert isinstance(plat, GameCubeHWPlatform)
        assert plat.describe()["instance_id"] == "hw-001"

    def test_bare_id_routes_to_hardware(self, tmp_path):
        cfg = make_hw_config(tmp_path)
        mgr = HardwareManager(cfg)
        mgr.register("hw-001")
        # manager=None so a bare id must resolve to the hardware instance.
        plat = resolve_platform("hw-001", None, mgr)
        assert isinstance(plat, GameCubeHWPlatform)

    def test_unknown_bare_id_raises(self, tmp_path):
        mgr = HardwareManager(make_hw_config(tmp_path))
        with pytest.raises(NotFoundError):
            resolve_platform("nope", None, mgr)


# ---------------------------------------------------------------------------
# MCP tool layer
# ---------------------------------------------------------------------------
@pytest.fixture
def hw(monkeypatch, tmp_path):
    """A fresh, isolated hardware manager wired into the server module."""
    cfg = make_hw_config(tmp_path)
    mgr = HardwareManager(cfg)
    monkeypatch.setattr(server, "hardware", mgr)
    monkeypatch.setattr(server, "config", cfg)
    return mgr


class TestHardwareTools:
    def test_register_and_list(self, hw):
        res = server.gc_instance_create_hardware("hw-001", capture_device="/dev/video0")
        assert res["instance_id"] == "hw-001"
        listing = server.gc_instance_list()
        assert listing[0]["instance_id"] == "hw-001"
        assert listing[0]["capture_device"] == "/dev/video0"

    def test_status_and_remove(self, hw):
        server.gc_instance_create_hardware("hw-001")
        status = server.gc_instance_status("hw-001")
        assert status["kind"] == "gamecube-hw"
        removed = server.gc_instance_remove("hw-001")
        assert removed["removed"] is True
        assert server.gc_instance_list() == []

    def test_update(self, hw):
        server.gc_instance_create_hardware("hw-001")
        res = server.gc_hardware_update("hw-001", serial_devices=["/dev/ttyUSB0"])
        assert res["serial_devices"] == ["/dev/ttyUSB0"]

    def test_register_and_list_include_info(self, hw):
        server.gc_instance_create_hardware("hw-001", info="test rig")
        listing = server.gc_instance_list()
        assert listing[0]["info"] == "test rig"

    def test_platform_list_includes_hardware_info(self, hw):
        server.gc_instance_create_hardware("hw-001", capture_device="/dev/video0",
                                    info="test rig")
        platforms = server.gc_platform_list()
        hw_entries = [p for p in platforms if p["kind"] == "gamecube-hw"]
        assert any(p["instance_id"] == "hw-001" and p["info"] == "test rig"
                   for p in hw_entries)

    def test_platform_list_includes_dolphin_info(self, hw, monkeypatch):
        # gc_platform_list also surfaces Dolphin entries; patch the Dolphin
        # manager so its status dict (with `info`) flows through.
        class FakeManager:
            def list(self):
                return [{
                    "instance_id": "dol-001",
                    "running": True,
                    "pid": 1234,
                    "info": "dev rig",
                }]

        monkeypatch.setattr(server, "manager", FakeManager())
        platforms = server.gc_platform_list()
        dolphin = [p for p in platforms if p["kind"] == "dolphin"]
        assert any(p["instance_id"] == "dol-001" and p["info"] == "dev rig"
                   for p in dolphin)

    def test_platform_list_includes_hardware(self, hw):
        server.gc_instance_create_hardware("hw-001", capture_device="/dev/video0")
        platforms = server.gc_platform_list()
        hw_entries = [p for p in platforms if p["kind"] == "gamecube-hw"]
        assert any(p["instance_id"] == "hw-001" for p in hw_entries)

    def test_capture_screenshot_on_hardware(self, hw, monkeypatch, tmp_path):
        device = tmp_path / "video0"
        device.write_bytes(b"\x00")
        server.gc_instance_create_hardware("hw-001", capture_device=str(device))

        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"png")
            return FakeProc(0)

        monkeypatch.setattr("gc_mcp.capture.v4l2.subprocess.run", fake_run)
        res = server.gc_capture_screenshot("hw-001")
        assert res["path"].endswith(".png")
        assert Path(res["path"]).exists()

    def test_capture_configure_tool(self, hw, tmp_path):
        server.gc_instance_create_hardware("hw-001")
        res = server.gc_capture_configure("hw-001", width=320, height=240)
        assert res["width"] == 320
        assert res["height"] == 240
        assert hw.status("hw-001")["capture"]["width"] == 320

    def test_server_info_reports_hardware(self, hw):
        info = server.gc_server_info()
        assert "hardware_instances" in info
        assert info["hardware_file"].endswith("hardware.json")
