"""Capture backend abstraction.

A capture backend grabs the emulator's rendered output and writes a PNG file.
Different backends are used depending on the environment (Docker/headless):

* ``xvfb`` (default): Dolphin runs under a virtual X server; the frame is
  grabbed from the X display with ffmpeg (x11grab) or xwd.
* ``framedump``: Dolphin's built-in frame dump to PNG, a fallback that needs no
  display server.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class CaptureBackend(ABC):
    """Abstract capture backend."""

    #: Machine-readable backend name.
    name: str = "base"

    @abstractmethod
    def capture(self, dest_path: Path) -> Path:
        """Capture the current frame to ``dest_path`` (PNG) and return it."""
        ...

    def launch_args(self) -> list[str]:
        """Extra Dolphin CLI arguments this backend needs (e.g. frame dump)."""
        return []

    def configure(self, **opts: object) -> dict:
        """Update backend configuration and return the effective config."""
        return {"backend": self.name, **opts}

    def close(self) -> None:
        """Release resources. No-op by default."""
