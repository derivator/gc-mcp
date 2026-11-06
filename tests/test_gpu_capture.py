"""Tests for GPU-acceleration resolution, display sizing and capture crop."""

from __future__ import annotations

import shutil

import pytest

from gc_mcp.capture import FrameDumpCapture, XvfbCapture
from gc_mcp.config import ServerConfig, parse_resolution, resolve_gpu_accel
from gc_mcp.errors import StartupError
from gc_mcp.instances.dolphin_instance import DolphinInstance, InstanceSpec


# ---------------------------------------------------------------------------
# parse_resolution
# ---------------------------------------------------------------------------
class TestParseResolution:
    def test_valid(self) -> None:
        assert parse_resolution("640x480") == (640, 480)
        assert parse_resolution("1280x960") == (1280, 960)

    def test_case_insensitive(self) -> None:
        assert parse_resolution("640X480") == (640, 480)

    def test_invalid_raises(self) -> None:
        for bad in ("640", "640x", "x480", "640xabc", "", None):
            with pytest.raises(ValueError):
                parse_resolution(bad)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# resolve_gpu_accel
# ---------------------------------------------------------------------------
class TestResolveGpuAccel:
    def test_auto_with_vgl_and_gpu_is_egl(self) -> None:
        assert resolve_gpu_accel("auto", "/dev/dri/card2", True) == "vgl-egl"

    def test_auto_without_gpu_is_software(self) -> None:
        assert resolve_gpu_accel("auto", None, False) == "software"

    def test_software_mode(self) -> None:
        for mode in ("none", "software", "off"):
            assert resolve_gpu_accel(mode, "/dev/dri/card2", True) == "software"

    def test_vgl_prefers_egl_then_glx(self) -> None:
        assert resolve_gpu_accel("vgl", "/dev/dri/card2", True) == "vgl-egl"
        assert resolve_gpu_accel("vgl", None, True, ":0.0") == "vgl-glx"
        assert resolve_gpu_accel("vgl", None, True) == "software"

    def test_vgl_egl_requires_gpu(self) -> None:
        with pytest.raises(ValueError):
            resolve_gpu_accel("vgl-egl", None, True)

    def test_vgl_glx_requires_display(self) -> None:
        with pytest.raises(ValueError):
            resolve_gpu_accel("vgl-glx", "/dev/dri/card2", True)

    def test_auto_with_glx_only(self) -> None:
        assert resolve_gpu_accel("auto", None, True, ":0.0") == "vgl-glx"


# ---------------------------------------------------------------------------
# ServerConfig.native_size
# ---------------------------------------------------------------------------
class TestNativeSize:
    def test_default(self) -> None:
        assert ServerConfig().native_size == (640, 480)

    def test_configured(self) -> None:
        cfg = ServerConfig(native_resolution="800x600")
        assert cfg.native_size == (800, 600)


# ---------------------------------------------------------------------------
# Instance sizing + launch command
# ---------------------------------------------------------------------------
def _make_instance(tmp_path, cfg, **spec_kwargs) -> DolphinInstance:
    spec = InstanceSpec(
        instance_id="t",
        rom_path=tmp_path / "game.iso",
        user_dir=tmp_path / "user",
        capture_backend="framedump",
        **spec_kwargs,
    )
    inst = DolphinInstance(spec, cfg)
    # A capture backend is required by _build_launch_command.
    inst._capture = FrameDumpCapture(tmp_path / "Dump" / "Frames")
    return inst


def _make_config(tmp_path, **kw) -> ServerConfig:
    return ServerConfig(
        base_dir=tmp_path / "data",
        dolphin_binary="/bin/true",
        gpu_available=True,
        gpu_device="/dev/dri/card2",
        **kw,
    )


class TestScreenSize:
    def test_native_no_scale(self, tmp_path) -> None:
        cfg = _make_config(tmp_path)
        inst = _make_instance(tmp_path, cfg)
        assert inst._screen_size(cfg) == "640x480"

    def test_internal_scale(self, tmp_path) -> None:
        cfg = _make_config(tmp_path)
        inst = _make_instance(tmp_path, cfg, internal_resolution=2)
        assert inst._screen_size(cfg) == "1280x960"

    def test_per_instance_native_override(self, tmp_path) -> None:
        cfg = _make_config(tmp_path, native_resolution="800x600")
        inst = _make_instance(tmp_path, cfg, native_resolution="640x480")
        assert inst._screen_size(cfg) == "640x480"


class TestLaunchCommand:
    def test_software_sets_llvmpipe_env(self, tmp_path) -> None:
        cfg = _make_config(tmp_path, gpu_accel="software")
        inst = _make_instance(tmp_path, cfg)
        cmd, env = inst._build_launch_command(cfg)
        assert cmd[0] == "/bin/true"
        assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"
        assert env["GALLIUM_DRIVER"] == "llvmpipe"
        assert "vglrun" not in " ".join(cmd)

    def test_vgl_egl_wraps_with_vglrun(self, tmp_path, monkeypatch) -> None:
        cfg = _make_config(tmp_path, gpu_accel="vgl-egl")
        inst = _make_instance(tmp_path, cfg)

        real_which = shutil.which

        def fake_which(name: str) -> str | None:
            if name == "vglrun":
                return "/usr/bin/vglrun"
            return real_which(name)

        monkeypatch.setattr("gc_mcp.instances.dolphin_instance.shutil.which", fake_which)

        cmd, env = inst._build_launch_command(cfg)
        assert cmd[0] == "/usr/bin/vglrun"
        assert cmd[1] == "-d"
        assert cmd[2] == "/dev/dri/card2"
        assert cmd[3] == "/bin/true"
        # Software-render forcing must be removed when rendering on the GPU.
        assert "LIBGL_ALWAYS_SOFTWARE" not in env
        assert "GALLIUM_DRIVER" not in env

    def test_vgl_egl_missing_vglrun_raises(self, tmp_path, monkeypatch) -> None:
        cfg = _make_config(tmp_path, gpu_accel="vgl-egl")
        inst = _make_instance(tmp_path, cfg)
        monkeypatch.setattr("gc_mcp.instances.dolphin_instance.shutil.which", lambda n: None)
        with pytest.raises(StartupError):
            inst._build_launch_command(cfg)

    def test_status_reports_gpu_mode_and_screen(self, tmp_path) -> None:
        cfg = _make_config(tmp_path, gpu_accel="software")
        inst = _make_instance(tmp_path, cfg)
        inst._gpu_mode = "software"
        inst._screen_size_value = "640x480"
        status = inst.status()
        assert status["gpu_accel"] == "software"
        assert status["screen_size"] == "640x480"
        assert status["native_resolution"] == "640x480"


# ---------------------------------------------------------------------------
# XvfbCapture crop
# ---------------------------------------------------------------------------
class TestXvfbCaptureCrop:
    def test_crop_args_when_set(self) -> None:
        cap = XvfbCapture(":90", screen_size="1280x720", crop="640:480:0:0")
        assert cap._crop_args() == ["-vf", "crop=640:480:0:0"]

    def test_no_crop_by_default(self) -> None:
        cap = XvfbCapture(":90")
        assert cap._crop_args() == []

    def test_configure_sets_crop(self) -> None:
        cap = XvfbCapture(":90", screen_size="640x480")
        result = cap.configure(crop="640:480:0:0")
        assert cap.crop == "640:480:0:0"
        assert result["crop"] == "640:480:0:0"
