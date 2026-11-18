"""V4L2-based capture backend for real GameCube hardware.

Real hardware is connected to a capture card / USB capture device exposed as a
Video4Linux2 device (e.g. ``/dev/video0``). We grab a single frame from the
device and write it as a PNG, exposing the same ``capture(dest_path)`` interface
as the emulator capture backends.

The backend prefers OpenCV (``cv2`` — the same library the example
``cap_example.py`` uses) because it lets us flush the stale hardware buffers
before reading a fresh, low-latency frame, and because it lets us request the
device's native MJPG format. If ``cv2`` is not installed it falls back to
grabbing a single frame with ``ffmpeg -f v4l2``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..errors import CaptureError
from .base import CaptureBackend

#: Devices that have already been warmed up, keyed by ``(device, width, height,
#: format, fps)`` -> monotonic timestamp of the last successful warm-up. A USB
#: capture card delivers a placeholder frame until it has been streamed for a
#: few seconds; tracking this lets us only pay that cost once per device.
_WARMED: dict[tuple[str, int, int, str, int], float] = {}
_WARM_LOCK = threading.Lock()


def detect_video_devices() -> list[dict[str, Any]]:
    """Probe the Video4Linux2 device nodes present under ``/dev``.

    This is what lets an agent discover which capture device to register a real
    GameCube against (e.g. ``/dev/video0``). Each device is probed with
    ``v4l2-ctl`` (when installed and accessible) so we can report not only the
    node but also whether it is actually able to *capture video frames*.

    Some nodes expose only UVC *metadata* (e.g. a capture card's second
    interface), which cannot produce a video stream. Reporting ``can_capture``
    lets an agent avoid registering such a node.

    Returns a list of dicts, one per node:

    ``{"device": "/dev/video0", "name": ..., "can_capture": bool,
      "formats": [...], "error": str | None}``

    ``name``/``formats``/``can_capture`` are ``None``/empty/``False`` when
    ``v4l2-ctl`` is unavailable or the node cannot be probed.
    """
    dev = Path("/dev")
    if not dev.is_dir():
        return []
    nodes = sorted(str(p) for p in dev.glob("video*") if p.exists())
    ctl = shutil.which("v4l2-ctl")

    results: list[dict[str, Any]] = []
    for node in nodes:
        entry: dict[str, Any] = {
            "device": node,
            "name": None,
            "can_capture": False,
            "formats": [],
            "error": None,
        }
        if ctl is None:
            results.append(entry)
            continue

        # Card name + whether the node itself supports Video Capture.
        try:
            all_res = subprocess.run(
                [ctl, "-d", node, "--all"], capture_output=True, timeout=10, check=False
            )
            text = all_res.stdout.decode("utf-8", "replace")
            entry["name"] = _parse_card_type(text)
            entry["can_capture"] = _parse_can_capture(text)
            if all_res.returncode != 0 and not text:
                entry["error"] = all_res.stderr.decode("utf-8", "replace").strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            entry["error"] = str(exc)

        # Enumerate the pixel formats; an empty list means no video stream.
        try:
            fmt_res = subprocess.run(
                [ctl, "-d", node, "--list-formats"], capture_output=True, timeout=10, check=False
            )
            entry["formats"] = _parse_formats(fmt_res.stdout.decode("utf-8", "replace"))
            if entry["formats"]:
                entry["can_capture"] = True
        except (subprocess.TimeoutExpired, OSError) as exc:
            if entry["error"] is None:
                entry["error"] = str(exc)

        results.append(entry)
    return results


def _parse_card_type(text: str) -> str | None:
    """Extract the card name from ``v4l2-ctl --all`` output (``Card type``)."""
    match = re.search(r"Card type\s*:\s*(.+)", text)
    return match.group(1).strip() if match else None


def _parse_can_capture(text: str) -> bool:
    """Whether the node itself supports video capture.

    ``v4l2-ctl --all`` prints a ``Device Caps`` line that is authoritative for
    the node; V4L2_CAP_VIDEO_CAPTURE is bit 0 (``0x1``). The driver-wide
    ``Capabilities`` line can list ``Video Capture`` even on a node that only
    exposes metadata, so we must prefer ``Device Caps`` when present.
    """
    match = re.search(r"Device Caps\s*:\s*(0x[0-9a-fA-F]+)", text)
    if match:
        return bool(int(match.group(1), 16) & 0x1)
    # Fallback: no Device Caps line, check the capability list.
    return "Video Capture" in text


def _parse_formats(text: str) -> list[str]:
    """Extract pixel formats from ``v4l2-ctl --list-formats`` output.

    Lines look like ``\t[0]: 'MJPG' (Motion-JPEG, ...)``.
    """
    formats: list[str] = []
    for line in text.splitlines():
        match = re.search(r"\[[0-9]+\]:\s*'([^']+)'", line)
        if match:
            formats.append(match.group(1))
    return formats


def _normalise_format(value: str | None) -> str | None:
    """Normalise a pixel-format name for ffmpeg's ``-input_format``.

    OpenCV uses fourcc strings like ``MJPG`` / ``YUYV``; ffmpeg's v4l2 demuxer
    expects names like ``mjpeg`` / ``yuyv422``. Map the common ones.
    """
    if not value:
        return None
    mapping = {
        "mjpg": "mjpeg",
        "mjpeg": "mjpeg",
        "yuyv": "yuyv422",
        "yuyv422": "yuyv422",
        "nv12": "nv12",
        "rgb24": "rgb24",
        "bgr24": "bgr24",
    }
    return mapping.get(str(value).strip().lower(), str(value).strip().lower())


class V4L2Capture(CaptureBackend):
    """Capture a single frame from a V4L2 video device into a PNG."""

    name = "v4l2"

    def __init__(self, device: str = "/dev/video0", width: int = 640,
                 height: int = 480, format: str = "mjpg", fps: int = 30,
                 flush_frames: int = 5, opencv_first: bool = True,
                 warmup_seconds: float = 0.0, warmup_ttl: float = 60.0) -> None:
        self.device = str(device)
        self.width = int(width)
        self.height = int(height)
        self.format = str(format)
        self.fps = int(fps)
        #: How many stale hardware buffers to flush before reading a fresh frame
        #: (used by the OpenCV path, matching ``cap_example.py``).
        self.flush_frames = int(flush_frames)
        #: Whether to prefer OpenCV when both it and ffmpeg are available.
        self.opencv_first = bool(opencv_first)
        #: How long to stream the device before grabbing a frame the first time.
        #: A USB capture card delivers a placeholder/black frame until it has
        #: been fed a continuous stream for a few seconds. ``0`` disables the
        #: warm-up (a fast single-frame grab, which may return a placeholder).
        self.warmup_seconds = float(warmup_seconds)
        #: How long a warmed device is trusted before it must be warmed again.
        self.warmup_ttl = float(warmup_ttl)

    # ------------------------------------------------------------------
    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    def _run(self, cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise CaptureError(f"required capture tool not found: {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CaptureError(f"capture timed out: {cmd}") from exc

    # ------------------------------------------------------------------
    # Warm-up state (module-level so it survives across backend instances)
    # ------------------------------------------------------------------
    def _warm_key(self) -> tuple[str, int, int, str, int]:
        return (self.device, self.width, self.height, self.format, self.fps)

    def _needs_warmup(self, key: tuple) -> bool:
        if self.warmup_seconds <= 0:
            return False
        now = time.monotonic()
        with _WARM_LOCK:
            last = _WARMED.get(key)
            return last is None or (now - last) > self.warmup_ttl

    def _mark_warm(self, key: tuple) -> None:
        if self.warmup_seconds <= 0:
            return
        now = time.monotonic()
        with _WARM_LOCK:
            # Prune stale entries so the dict stays bounded in a long-lived server.
            stale = [k for k, ts in _WARMED.items() if (now - ts) > self.warmup_ttl]
            for k in stale:
                _WARMED.pop(k, None)
            _WARMED[key] = now

    def _capture_opencv(self, dest_path: Path) -> Path:
        """Grab a frame with OpenCV (low-latency, flushes stale buffers)."""
        try:
            import cv2
        except ImportError:
            raise CaptureError(
                "opencv (cv2) is not installed; install it or use ffmpeg to "
                "capture from a V4L2 device"
            )

        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise CaptureError(
                f"could not open video device {self.device}",
                details={"device": self.device},
            )
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.format.upper()))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            if self.fps:
                cap.set(cv2.CAP_PROP_FPS, self.fps)
            # Flush the hardware's stale buffers so we read a fresh frame.
            for _ in range(self.flush_frames):
                cap.grab()
            ok, frame = cap.read()
            if not ok:
                raise CaptureError(
                    f"failed to read a frame from {self.device}",
                    details={"device": self.device},
                )
            wrote = cv2.imwrite(str(dest_path), frame)
            if not wrote:
                raise CaptureError(
                    f"OpenCV could not write {dest_path}",
                    details={"device": self.device},
                )
            return dest_path
        finally:
            cap.release()

    def _capture_ffmpeg(self, dest_path: Path) -> Path:
        """Grab a single frame with ``ffmpeg -f v4l2``."""
        if shutil.which("ffmpeg") is None:
            raise CaptureError(
                "neither ffmpeg nor opencv (cv2) is available; cannot capture "
                f"from a V4L2 device ({self.device})"
            )

        base = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
        ]
        fmt = _normalise_format(self.format)
        attempts: list[list[str]] = []
        if fmt:
            attempts.append(base + ["-input_format", fmt])
        # A second attempt without a forced pixel format lets the device choose.
        attempts.append(base[:])

        last_error = ""
        for prefix in attempts:
            cmd = prefix + [
                "-video_size",
                self.resolution,
                "-i",
                self.device,
                "-frames:v",
                "1",
                str(dest_path),
            ]
            result = self._run(cmd)
            if result.returncode == 0 and dest_path.exists():
                return dest_path
            last_error = result.stderr.decode("utf-8", "replace").strip()

        raise CaptureError(
            f"could not capture a frame from {self.device}: {last_error or 'ffmpeg produced no output'}",
            details={"device": self.device, "resolution": self.resolution},
        )

    def _capture_warmup_ffmpeg(self, dest_path: Path) -> Path:
        """Stream the device for ``warmup_seconds`` and write the final frame.

        A USB capture card only delivers real content once it has been fed a
        continuous stream; this is the ``ffmpeg`` form of that warm-up. We keep
        only the last frame (``-update 1``) so the caller still gets a usable
        screenshot even on the first (slow) capture.
        """
        if shutil.which("ffmpeg") is None:
            raise CaptureError(
                "neither ffmpeg nor opencv (cv2) is available; cannot warm up "
                f"the V4L2 device ({self.device})"
            )

        base = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "v4l2",
        ]
        fmt = _normalise_format(self.format)
        attempts: list[list[str]] = []
        if fmt:
            attempts.append(base + ["-input_format", fmt])
        attempts.append(base[:])

        last_error = ""
        for prefix in attempts:
            cmd = prefix + [
                "-video_size",
                self.resolution,
                "-i",
                self.device,
                "-t",
                str(self.warmup_seconds),
                "-update",
                "1",
                str(dest_path),
            ]
            result = self._run(cmd, timeout=max(30, int(self.warmup_seconds) + 15))
            if result.returncode == 0 and dest_path.exists():
                return dest_path
            last_error = result.stderr.decode("utf-8", "replace").strip()

        raise CaptureError(
            f"could not warm up V4L2 device {self.device}: {last_error or 'ffmpeg produced no output'}",
            details={
                "device": self.device,
                "resolution": self.resolution,
                "warmup_seconds": self.warmup_seconds,
            },
        )

    def _capture_warmup_opencv(self, dest_path: Path) -> Path:
        """Stream the device for ``warmup_seconds`` then read a frame (OpenCV)."""
        try:
            import cv2
        except ImportError:
            raise CaptureError(
                "opencv (cv2) is not installed; install it or use ffmpeg to "
                "warm up a V4L2 device"
            )

        cap = cv2.VideoCapture(self.device, cv2.CAP_V4L2)
        if not cap.isOpened():
            raise CaptureError(
                f"could not open video device {self.device}",
                details={"device": self.device},
            )
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*self.format.upper()))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            if self.fps:
                cap.set(cv2.CAP_PROP_FPS, self.fps)
            # Stream for warmup_seconds so the hardware initialises, then read.
            frames = max(1, int(self.warmup_seconds * max(self.fps, 1)))
            for _ in range(frames):
                cap.grab()
            ok, frame = cap.read()
            if not ok:
                raise CaptureError(
                    f"failed to read a frame from {self.device}",
                    details={"device": self.device},
                )
            wrote = cv2.imwrite(str(dest_path), frame)
            if not wrote:
                raise CaptureError(
                    f"OpenCV could not write {dest_path}",
                    details={"device": self.device},
                )
            return dest_path
        finally:
            cap.release()

    # ------------------------------------------------------------------
    def capture(self, dest_path: Path) -> Path:
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if not Path(self.device).exists():
            raise CaptureError(
                f"video device {self.device} does not exist",
                details={"device": self.device},
            )

        try:
            import cv2  # noqa: F401
            has_cv = True
        except ImportError:
            has_cv = False

        # On a cold device, stream for warmup_seconds first so the card delivers
        # real content. This both warms the card and writes a usable frame.
        key = self._warm_key()
        if self.warmup_seconds > 0 and self._needs_warmup(key):
            if has_cv and self.opencv_first:
                try:
                    result = self._capture_warmup_opencv(dest_path)
                    self._mark_warm(key)
                    return result
                except CaptureError:
                    # OpenCV may not support this device; try ffmpeg's warm-up
                    # before falling back to a single frame.
                    pass
            try:
                result = self._capture_warmup_ffmpeg(dest_path)
                self._mark_warm(key)
                return result
            except CaptureError:
                # Fall through to the single-frame path; it may still succeed.
                pass

        if has_cv and self.opencv_first:
            try:
                return self._capture_opencv(dest_path)
            except CaptureError:
                # Fall through to ffmpeg; it may still succeed.
                pass
        elif has_cv:
            try:
                return self._capture_ffmpeg(dest_path)
            except CaptureError:
                return self._capture_opencv(dest_path)

        return self._capture_ffmpeg(dest_path)

    def configure(self, **opts: Any) -> dict:
        for key in ("device", "width", "height", "format", "fps",
                    "flush_frames", "opencv_first",
                    "warmup_seconds", "warmup_ttl"):
            if key in opts:
                setattr(self, key, opts[key])
        return {
            "backend": self.name,
            "device": self.device,
            "width": self.width,
            "height": self.height,
            "format": self.format,
            "fps": self.fps,
            "flush_frames": self.flush_frames,
            "warmup_seconds": self.warmup_seconds,
            "warmup_ttl": self.warmup_ttl,
        }
