"""Xvfb-based capture backend (Option A).

Dolphin renders into a virtual X server. We grab the root window and encode a
PNG. Requires ``Xvfb`` and either ``ffmpeg`` (preferred) or ``xwd``.

If a hardware GPU render node is present (``/dev/dri/renderD*``) we leave the
GL backend alone; otherwise the instance should run Dolphin with Mesa's software
``llvmpipe`` renderer (handled by the instance / video backend selection).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ..errors import CaptureError
from .base import CaptureBackend


class Xvfb:
    """Minimal wrapper for an Xvfb server process."""

    def __init__(self, display: int, screen: str = "1280x720x24") -> None:
        self.display = int(display)
        self.screen = screen
        self._proc: subprocess.Popen | None = None

    @property
    def display_string(self) -> str:
        return f":{self.display}"

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        if shutil.which("Xvfb") is None:
            raise CaptureError("Xvfb is not installed (needed for the xvfb capture backend)")
        self._proc = subprocess.Popen(
            [
                "Xvfb",
                self.display_string,
                "-screen",
                "0",
                self.screen,
                "-nolisten",
                "tcp",
                "-ac",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None


class XvfbCapture(CaptureBackend):
    """Capture a frame from an X display into a PNG."""

    name = "xvfb"

    def __init__(self, display: int | str, screen_size: str = "1280x720") -> None:
        if isinstance(display, int):
            self.display = f":{display}"
        else:
            self.display = display if str(display).startswith(":") else f":{display}"
        self.screen_size = screen_size

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, timeout=30)
        except FileNotFoundError as exc:
            raise CaptureError(f"required capture tool not found: {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CaptureError(f"capture timed out: {cmd}") from exc

    def capture(self, dest_path: Path) -> Path:
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Prefer ffmpeg x11grab.
        if shutil.which("ffmpeg"):
            cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-f",
                "x11grab",
                "-video_size",
                self.screen_size,
                "-i",
                self.display,
                "-frames:v",
                "1",
                str(dest_path),
            ]
            result = self._run(cmd)
            if result.returncode == 0 and dest_path.exists():
                return dest_path

        # Fall back to xwd (dump root window) + ffmpeg/convert to PNG.
        if shutil.which("xwd"):
            raw = dest_path.with_suffix(".xwd")
            xwd_result = self._run(["xwd", "-root", "-display", self.display, "-out", str(raw)])
            if xwd_result.returncode == 0 and raw.exists():
                if shutil.which("convert"):
                    conv = self._run(["convert", str(raw), str(dest_path)])
                    if conv.returncode == 0 and dest_path.exists():
                        raw.unlink(missing_ok=True)
                        return dest_path
                if shutil.which("ffmpeg"):
                    conv = self._run(
                        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), str(dest_path)]
                    )
                    if conv.returncode == 0 and dest_path.exists():
                        raw.unlink(missing_ok=True)
                        return dest_path

        raise CaptureError(
            f"could not capture frame from {self.display}: "
            "ffmpeg/xwd produced no output. Is the X server running?",
            details={"display": self.display},
        )

    def configure(self, **opts: object) -> dict:
        for key in ("display", "screen_size"):
            if key in opts:
                setattr(self, key, opts[key])
        return {"backend": self.name, "display": self.display, "screen_size": self.screen_size}
