"""Dolphin frame-dump capture backend (fallback).

Dolphin can dump every rendered frame to a PNG in the instance's
``Dump/Frames/`` directory. This backend enables that at launch (via
``-C Movie.DumpFrames=True``) and, on capture, copies the newest dumped frame.
It needs no display server, so it works in a fully headless container, but it
is heavier than grabbing the X framebuffer.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..errors import CaptureError
from .base import CaptureBackend


class FrameDumpCapture(CaptureBackend):
    """Capture by copying the latest Dolphin frame-dump PNG."""

    name = "framedump"

    def __init__(self, dump_dir: Path) -> None:
        self.dump_dir = Path(dump_dir)

    def launch_args(self) -> list[str]:
        # Enable frame dumping and output PNGs (not the default AVI video). The
        # `-C` flag uses Dolphin's system names: "Dolphin" is System::Main, so
        # Movie.DumpFrames lives there, and "Graphics" is System::GFX.
        return [
            "-C",
            "Dolphin.Movie.DumpFrames=True",
            "-C",
            "Graphics.Settings.DumpFramesAsImages=True",
        ]

    def _latest_frame(self) -> Path | None:
        if not self.dump_dir.is_dir():
            return None
        pngs = sorted(self.dump_dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
        return pngs[-1] if pngs else None

    def capture(self, dest_path: Path) -> Path:
        dest_path = Path(dest_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        latest = self._latest_frame()
        if latest is None:
            raise CaptureError(
                "no frame-dump PNG available yet; ensure Movie.DumpFrames is enabled "
                "and the game has rendered at least one frame",
                details={"dump_dir": str(self.dump_dir)},
            )
        shutil.copyfile(latest, dest_path)
        return dest_path

    def configure(self, **opts: object) -> dict:
        if "dump_dir" in opts:
            self.dump_dir = Path(opts["dump_dir"])  # type: ignore[arg-type]
        return {"backend": self.name, "dump_dir": str(self.dump_dir)}
