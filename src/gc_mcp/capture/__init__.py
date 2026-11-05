"""Capture backends."""

from pathlib import Path

from .base import CaptureBackend
from .framedump import FrameDumpCapture
from .xvfb import Xvfb, XvfbCapture

__all__ = ["CaptureBackend", "FrameDumpCapture", "Xvfb", "XvfbCapture"]


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
    raise ValueError(f"unknown capture backend: {name!r}")
