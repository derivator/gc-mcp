"""Capture backends."""

from pathlib import Path

from .base import CaptureBackend
from .framedump import FrameDumpCapture
from .v4l2 import V4L2Capture, detect_video_devices
from .xvfb import Xvfb, XvfbCapture

__all__ = [
    "CaptureBackend",
    "FrameDumpCapture",
    "V4L2Capture",
    "Xvfb",
    "XvfbCapture",
    "detect_video_devices",
]


def create_capture_backend(name: str, *, display: int | None = None,
                           dump_dir: Path | None = None, **opts) -> CaptureBackend:
    """Factory for capture backends."""
    if name == "framedump":
        if dump_dir is None:
            raise ValueError("framedump capture backend requires a dump_dir")
        return FrameDumpCapture(dump_dir)
    if name == "xvfb":
        if display is None:
            raise ValueError("xvfb capture backend requires a display number")
        return XvfbCapture(display, **opts)
    if name == "v4l2":
        return V4L2Capture(**opts)
    raise ValueError(f"unknown capture backend: {name!r}")
