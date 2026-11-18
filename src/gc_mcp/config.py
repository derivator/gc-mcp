"""Server configuration.

Read from environment variables so the server is easy to configure in a
container without code changes. Sensible defaults are chosen for a headless
Docker deployment.
"""

from __future__ import annotations

import os
import shutil
import stat
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


#: GameCube IPL regions Dolphin recognises. Each maps to a ``<region>/IPL.bin``
#: source file under the configured IPL folder and to a ``GC/<region>/``
#: directory under the per-instance user dir (Dolphin's ``GetBootROMPath``).
#: ``JAP`` is Dolphin's legacy directory name for the NTSC-J boot ROM.
IPL_REGIONS = ("USA", "JAP", "EUR")


def detect_gpu() -> bool:
    """Return True if a hardware GPU render node is available under /dev/dri."""
    return detect_gpu_device() is not None


def detect_gpu_device() -> str | None:
    """Return the path of a usable GPU DRM device, or ``None``.

    Prefers a ``/dev/dri/card*`` node (a full DRM device, which VirtualGL's EGL
    back end can open directly) and falls back to a ``/dev/dri/renderD*`` render
    node. This is what we hand to ``vglrun -d`` so OpenGL renders on the real GPU
    instead of Mesa's software ``llvmpipe``.
    """
    dri = Path("/dev/dri")
    try:
        if not dri.is_dir():
            return None
        cards = sorted(dri.glob("card*"), key=lambda p: p.name)
        for c in cards:
            try:
                if c.exists() and stat.S_ISCHR(c.stat().st_mode):
                    return str(c)
            except OSError:
                continue
        renders = sorted(dri.glob("renderD*"), key=lambda p: p.name)
        for r in renders:
            try:
                if r.exists():
                    return str(r)
            except OSError:
                continue
    except OSError:
        pass
    return None


def vglrun_available() -> bool:
    """Return True if VirtualGL's ``vglrun`` wrapper is on PATH."""
    return shutil.which("vglrun") is not None


def parse_resolution(value: str) -> tuple[int, int]:
    """Parse a ``"WxH"`` resolution string into ``(width, height)``."""
    try:
        w, h = str(value).strip().lower().split("x", 1)
        return (int(w), int(h))
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"invalid resolution {value!r}; expected a 'WxH' string like '640x480'"
        ) from exc


def _gpu_accel_modes() -> tuple[str, ...]:
    return ("auto", "none", "software", "vgl", "vgl-egl", "vgl-glx")


def resolve_gpu_accel(
    mode: str | None,
    gpu_device: str | None,
    vglrun: bool,
    vgl_3d_display: str | None = None,
) -> str:
    """Resolve a requested GPU-acceleration mode to a concrete rendering path.

    Returns one of ``"software"``, ``"vgl-egl"`` or ``"vgl-glx"``.

    * ``none`` / ``software`` / ``off``  -> software (llvmpipe).
    * ``vgl``                            -> VirtualGL; prefer the EGL back end
      (render on the GPU via a DRI device, no 3D X server needed), else the GLX
      back end if a 3D X server display is configured.
    * ``vgl-egl`` / ``vgl-glx``          -> force a specific VirtualGL back end.
    * ``auto``                           -> VirtualGL EGL when both ``vglrun`` and
      a GPU device are present, otherwise software.
    """
    mode = (mode or "auto").strip().lower()
    if mode in ("none", "software", "off"):
        return "software"
    if mode == "vgl-egl":
        if not (vglrun and gpu_device):
            raise ValueError(
                "gpu_accel=vgl-egl requires vglrun on PATH and a GPU device "
                "(set GC_MCP_GPU_DEVICE if auto-detection fails)"
            )
        return "vgl-egl"
    if mode == "vgl-glx":
        if not (vglrun and vgl_3d_display):
            raise ValueError(
                "gpu_accel=vgl-glx requires vglrun on PATH and a 3D X server "
                "display (set GC_MCP_VGL_3D_DISPLAY)"
            )
        return "vgl-glx"
    if mode == "vgl":
        if vglrun and gpu_device:
            return "vgl-egl"
        if vglrun and vgl_3d_display:
            return "vgl-glx"
        return "software"
    # auto
    if vglrun and gpu_device:
        return "vgl-egl"
    if vglrun and vgl_3d_display:
        return "vgl-glx"
    return "software"


def shm_usage() -> dict:
    """Return /dev/shm usage in bytes, or None fields if unavailable.

    Dolphin backs the emulated GameCube RAM with a file in ``/dev/shm``; a small
    ``/dev/shm`` (e.g. 64 MiB) causes a SIGBUS crash once the game allocates RAM.
    Exposing this lets an agent diagnose that failure mode.
    """
    try:
        st = os.statvfs("/dev/shm")
        total = st.f_blocks * st.f_frsize
        avail = st.f_bavail * st.f_frsize
        return {"total": total, "available": avail, "path": "/dev/shm"}
    except OSError:
        return {"total": None, "available": None, "path": "/dev/shm"}


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
    #: GameCube native framebuffer resolution (used to size the X display and to
    #: crop captures to the game output without black borders).
    native_resolution: str = field(default_factory=lambda: _env_str("GC_MCP_NATIVE_RES", "640x480"))

    # GPU rendering
    #: Requested GPU-acceleration mode: auto/none/software/vgl/vgl-egl/vgl-glx.
    gpu_accel: str = field(default_factory=lambda: _env_str("GC_MCP_GPU_ACCEL", "auto"))
    #: Explicit GPU DRM device path; empty/None means auto-detect under /dev/dri.
    gpu_device: str | None = field(default_factory=lambda: _env_str("GC_MCP_GPU_DEVICE", "") or None)
    #: 3D X server display for VirtualGL's GLX back end (e.g. ":0.0").
    vgl_3d_display: str | None = field(default_factory=lambda: _env_str("GC_MCP_VGL_3D_DISPLAY", "") or None)

    # Real GameCube hardware defaults
    #: Default Video4Linux2 capture device for a hardware instance.
    hw_capture_device: str = field(default_factory=lambda: _env_str("GC_MCP_HW_CAPTURE_DEVICE", "/dev/video0"))
    hw_capture_width: int = field(default_factory=lambda: _env_int("GC_MCP_HW_CAPTURE_WIDTH", 640))
    hw_capture_height: int = field(default_factory=lambda: _env_int("GC_MCP_HW_CAPTURE_HEIGHT", 480))
    hw_capture_format: str = field(default_factory=lambda: _env_str("GC_MCP_HW_CAPTURE_FORMAT", "mjpg"))
    hw_capture_fps: int = field(default_factory=lambda: _env_int("GC_MCP_HW_CAPTURE_FPS", 30))
    hw_capture_flush: int = field(default_factory=lambda: _env_int("GC_MCP_HW_CAPTURE_FLUSH", 5))
    #: How long to stream a cold V4L2 device before grabbing a frame (seconds).
    #: A USB capture card only delivers real content after it has been fed a
    #: continuous stream for a few seconds; ``0`` disables the warm-up.
    hw_capture_warmup: float = field(default_factory=lambda: _env_float("GC_MCP_HW_CAPTURE_WARMUP", 5.0))
    #: How long a warmed device is trusted before it must be warmed again (s).
    hw_capture_warmup_ttl: float = field(default_factory=lambda: _env_float("GC_MCP_HW_CAPTURE_WARMUP_TTL", 60.0))
    #: Default BBA IP for a hardware instance (the console's own IP, used to reach
    #: the Swiss netboot listener for ``gc_instance_start``). Empty/None means a
    #: new hardware instance must be given an IP explicitly via
    #: ``gc_instance_create_hardware`` or ``gc_hardware_update`` before ROM
    #: loading works.
    hw_bba_ip: str | None = field(default_factory=lambda: _env_str("GC_MCP_HW_BBA_IP", "") or None)
    #: Default netboot listener port (Swiss listens on TCP 4242).
    hw_bba_port: int = field(default_factory=lambda: _env_int("GC_MCP_HW_BBA_PORT", 4242))

    # Memory
    memory_backend: str = field(default_factory=lambda: _env_str("GC_MCP_MEMORY_BACKEND", "process_vm"))

    # OCR (optional; used by gc_capture_screenshot(ocr=True))
    #: RapidOCR language (e.g. ``en``, ``de``, ``japan``) used when OCR is
    #: requested. Common Tesseract codes (``eng``, ``deu``) are also accepted.
    ocr_lang: str = field(default_factory=lambda: _env_str("GC_MCP_OCR_LANG", "en"))

    # GameCube IPL (boot ROM)
    #: Folder containing GameCube IPL files, one region subfolder per region
    #: (each holding an ``IPL.bin``). When set, instances can boot the original
    #: GameCube IPL (disable "Skip System Menu"); without it the option is
    #: unavailable. Empty/None disables IPL boot.
    ipl_dir: Path | None = field(
        default_factory=lambda: (
            Path(_env_str("GC_MCP_IPL_DIR", "")).expanduser()
            if _env_str("GC_MCP_IPL_DIR", "")
            else None
        )
    )

    # Timing / robustness
    startup_timeout: float = field(default_factory=lambda: _env_float("GC_MCP_STARTUP_TIMEOUT", 20.0))
    fifo_open_retries: int = field(default_factory=lambda: _env_int("GC_MCP_FIFO_RETRIES", 200))
    fifo_retry_delay: float = field(default_factory=lambda: _env_float("GC_MCP_FIFO_RETRY_DELAY", 0.05))

    # Runtime-detected capabilities
    gpu_available: bool = field(default_factory=detect_gpu)
    #: Detected GPU DRM device path (cached at construction time).
    gpu_device_path: str | None = field(default_factory=detect_gpu_device)

    def __post_init__(self) -> None:
        self.base_dir = Path(self.base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        # If the user didn't pin a device, fall back to the auto-detected one.
        if not self.gpu_device:
            self.gpu_device = self.gpu_device_path
        if self.ipl_dir is not None:
            self.ipl_dir = Path(self.ipl_dir).expanduser()

    # ------------------------------------------------------------------
    # Derived paths
    # ------------------------------------------------------------------
    @property
    def instances_dir(self) -> Path:
        return self.base_dir / "instances"

    @property
    def captures_dir(self) -> Path:
        return self.base_dir / "captures"

    @property
    def hardware_file(self) -> Path:
        """The JSON file that persists real GameCube hardware instances."""
        return self.base_dir / "hardware.json"

    @property
    def states_dir(self) -> Path:
        """Directory where ``gc_dolphin_save_state`` copies each save-state file."""
        return self.base_dir / "states"

    @property
    def native_size(self) -> tuple[int, int]:
        """The GameCube native framebuffer size as ``(width, height)``."""
        return parse_resolution(self.native_resolution)

    def ipl_regions(self) -> list[str]:
        """Region names that have an IPL file under ``ipl_dir``."""
        if self.ipl_dir is None:
            return []
        return [r for r in IPL_REGIONS if (self.ipl_dir / r / "IPL.bin").is_file()]

    def ipl_available(self) -> bool:
        """Whether IPL boot is available (an IPL folder with files is set)."""
        return bool(self.ipl_regions())

    def vglrun_available(self) -> bool:
        return vglrun_available()

    def effective_gpu_accel(self) -> str:
        """Resolve the configured ``gpu_accel`` mode against this environment."""
        return resolve_gpu_accel(
            self.gpu_accel,
            self.gpu_device,
            self.vglrun_available(),
            self.vgl_3d_display,
        )

    def ensure_dirs(self) -> None:
        for d in (self.instances_dir, self.captures_dir, self.states_dir):
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
