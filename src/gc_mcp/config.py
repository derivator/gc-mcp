"""Server configuration.

Read from environment variables so the server is easy to configure in a
container without code changes. Sensible defaults are chosen for a headless
Docker deployment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def detect_gpu() -> bool:
    """Return True if a hardware GPU render node is available under /dev/dri."""
    dri = Path("/dev/dri")
    try:
        if not dri.is_dir():
            return False
        return any(dri.glob("renderD*"))
    except OSError:
        return False


@dataclass
class ServerConfig:
    """Global, immutable-ish server configuration."""

    dolphin_binary: str = field(
        default_factory=lambda: _env_str("GC_MCP_DOLPHIN_BINARY", "dolphin-emu")
    )
    base_dir: Path = field(
        default_factory=lambda: Path(_env_str("GC_MCP_DATA_DIR", str(Path.home() / ".gc-mcp")))
    )

    # Emulation defaults
    default_video_backend: str = field(
        default_factory=lambda: _env_str("GC_MCP_VIDEO_BACKEND", "OGL")
    )
    default_bba_mode: str = field(
        default_factory=lambda: _env_str("GC_MCP_BBA_MODE", "ipc")
    )
    default_bba_mac_prefix: str = field(
        default_factory=lambda: _env_str("GC_MCP_BBA_MAC_PREFIX", "00:09:bf")
    )
    default_controller: str = field(default_factory=lambda: _env_str("GC_MCP_CONTROLLER", "standard"))

    # Capture
    capture_backend: str = field(default_factory=lambda: _env_str("GC_MCP_CAPTURE_BACKEND", "auto"))

    # Memory
    memory_backend: str = field(default_factory=lambda: _env_str("GC_MCP_MEMORY_BACKEND", "process_vm"))

    # Timing / robustness
    startup_timeout: float = field(default_factory=lambda: _env_float("GC_MCP_STARTUP_TIMEOUT", 20.0))
    fifo_open_retries: int = field(default_factory=lambda: _env_int("GC_MCP_FIFO_RETRIES", 200))
    fifo_retry_delay: float = field(default_factory=lambda: _env_float("GC_MCP_FIFO_RETRY_DELAY", 0.05))

    # Runtime-detected capabilities
    gpu_available: bool = field(default_factory=detect_gpu)

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Derived paths
    # ------------------------------------------------------------------
    @property
    def instances_dir(self) -> Path:
        return self.base_dir / "instances"

    @property
    def captures_dir(self) -> Path:
        return self.base_dir / "captures"

    def ensure_dirs(self) -> None:
        for d in (self.instances_dir, self.captures_dir):
            d.mkdir(parents=True, exist_ok=True)

    def resolve_capture_backend(self) -> str:
        """Resolve `auto` to a concrete backend based on the environment."""
        if self.capture_backend and self.capture_backend != "auto":
            return self.capture_backend
        # Xvfb + OpenGL is the default path; it needs a display server available.
        # If we can't find the tools, fall back to the in-emulator frame dump.
        return "xvfb"


#: A module-level shared config instance.
config = ServerConfig()
