"""One running Dolphin emulator instance.

An instance owns an isolated ``--user`` data directory, an optional Xvfb display
server (for the ``xvfb`` capture backend), a spawned Dolphin process, a
controller backend (Dolphin "Pipes"), a capture backend, and a per-PID memory
backend. All of the ``gc_*`` tools operate through an instance; the ``dolphin_*``
tools operate on Dolphin's own configuration.

The launch uses only stock Dolphin flags (``--user``, ``--exec``, ``--batch``,
``--video_backend``, ``--config``), so no fork/patch is required.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..capture import CaptureBackend, FrameDumpCapture, Xvfb, XvfbCapture
from ..config import ServerConfig
from ..controllers import DolphinPipesController
from ..errors import StartupError
from ..memory import ProcessVMMemory
from .config_builder import InstanceConfig, InstanceConfigBuilder

#: Video backends accepted by Dolphin's ``--video_backend`` flag.
VIDEO_BACKENDS = ("OGL", "Vulkan", "Null", "Software Renderer")


@dataclass
class InstanceSpec:
    """Everything required to launch and configure one Dolphin instance."""

    instance_id: str
    rom_path: Path
    user_dir: Path
    #: Controller ports (0-3) to attach a standard GameCube controller to.
    controller_ports: tuple[int, ...] = (0,)
    #: BBA mode (ipc/hle/xlink/tap/tapserver/modem) or None to disable.
    bba_mode: str | None = None
    bba_mac: str | None = None
    video_backend: str = "OGL"
    capture_backend: str = "auto"
    memory_backend: str = "process_vm"
    #: X display number (allocated by the manager); used when capture is xvfb.
    display: int | None = None
    internal_resolution: int | None = None
    #: Extra Dolphin.ini ``[Core]`` overrides (e.g. ``{"SkipIPL": "False"}``).
    extra_core: dict[str, str] = field(default_factory=dict)
    #: Extra GCPadNew.ini overrides keyed by ``(port, key)``.
    extra_pad: dict[tuple[int, str], str] = field(default_factory=dict)
    #: Additional Dolphin CLI args appended verbatim.
    extra_args: list[str] = field(default_factory=list)


class DolphinInstance:
    """Owns a single Dolphin process and its sub-resources."""

    def __init__(self, spec: InstanceSpec, server_config: ServerConfig) -> None:
        self.spec = spec
        self.server_config = server_config
        self._proc: subprocess.Popen | None = None
        self._xvfb: Xvfb | None = None
        self._controller: DolphinPipesController | None = None
        self._capture: CaptureBackend | None = None
        self._memory: ProcessVMMemory | None = None
        self._log_path = spec.user_dir / "dolphin.log"
        self._started_at: float | None = None
        self._stop_requested = False

    # ------------------------------------------------------------------
    # Resource getters (created lazily / on start)
    # ------------------------------------------------------------------
    @property
    def controller(self) -> DolphinPipesController:
        if self._controller is None:
            raise StartupError("controller backend is not initialised")
        return self._controller

    @property
    def capture(self) -> CaptureBackend:
        if self._capture is None:
            raise StartupError("capture backend is not initialised")
        return self._capture

    @property
    def memory(self) -> ProcessVMMemory:
        if self._memory is None:
            raise StartupError("memory backend is not initialised")
        return self._memory

    @property
    def pid(self) -> int | None:
        if self._proc is None:
            return None
        return self._proc.pid

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def log_path(self) -> Path:
        return self._log_path

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------
    def start(self) -> dict:
        spec = self.spec
        cfg = self.server_config

        # 1. Write configuration (Dolphin.ini, GCPadNew.ini, GFX.ini, FIFOs).
        instance_cfg = InstanceConfig(
            user_dir=spec.user_dir,
            controller_ports=spec.controller_ports,
            bba_mode=spec.bba_mode,
            bba_mac=spec.bba_mac,
            video_backend=spec.video_backend,
            internal_resolution=spec.internal_resolution,
            extra_core=spec.extra_core,
            extra_pad=spec.extra_pad,
        )
        InstanceConfigBuilder(instance_cfg).write()

        # 2. Build the controller backend (creates the FIFOs before launch so
        #    Dolphin's Pipes backend sees them at startup).
        self._controller = DolphinPipesController(
            pipes_dir=spec.user_dir / "Pipes",
            ports=4,
            open_retries=cfg.fifo_open_retries,
            retry_delay=cfg.fifo_retry_delay,
        )
        self._controller.ensure_fifos()

        # 3. Choose the capture backend.
        self._capture = self._build_capture(cfg)

        # 4. Start Xvfb if the capture backend needs a display.
        if isinstance(self._capture, XvfbCapture):
            if spec.display is None:
                raise StartupError("xvfb capture backend requires a display number")
            self._xvfb = Xvfb(spec.display)
            self._xvfb.start()

        # 5. Launch Dolphin.
        self._launch_dolphin(cfg)

        self._started_at = time.monotonic()

        # 6. Create the per-PID memory backend.
        if self.pid is not None:
            self._memory = ProcessVMMemory(self.pid)

        return self.status()

    def _build_capture(self, cfg: ServerConfig) -> CaptureBackend:
        backend = self.spec.capture_backend
        if backend == "auto":
            backend = cfg.resolve_capture_backend()
        if backend == "framedump":
            return FrameDumpCapture(self.spec.user_dir / "Dump" / "Frames")
        if backend == "xvfb":
            if self.spec.display is None:
                raise StartupError("xvfb capture backend requires a display number")
            return XvfbCapture(self.spec.display)
        raise StartupError(f"unknown capture backend {backend!r}")

    def _resolve_video_backend(self, cfg: ServerConfig) -> str:
        backend = self.spec.video_backend or cfg.default_video_backend or "OGL"
        if backend not in VIDEO_BACKENDS:
            raise StartupError(f"unknown video backend {backend!r}")
        return backend

    def _launch_dolphin(self, cfg: ServerConfig) -> None:
        spec = self.spec
        if shutil.which(cfg.dolphin_binary) is None:
            raise StartupError(
                f"Dolphin binary {cfg.dolphin_binary!r} not found on PATH"
            )

        cmd = [cfg.dolphin_binary]
        cmd += [f"--user={spec.user_dir}"]
        cmd += [f"--exec={spec.rom_path}"]
        cmd += ["--batch"]  # headless: no UI, but the render widget still shows.
        cmd += ["-v", self._resolve_video_backend(cfg)]
        # Capture-specific CLI overrides.
        cmd += self._capture.launch_args()  # type: ignore[union-attr]
        cmd += spec.extra_args

        env = os.environ.copy()
        if isinstance(self._capture, XvfbCapture) and self._xvfb is not None:
            env["DISPLAY"] = self._xvfb.display_string
        # Software Mesa if no hardware GPU is present.
        if not cfg.gpu_available:
            env["LIBGL_ALWAYS_SOFTWARE"] = "1"
            env.setdefault("GALLIUM_DRIVER", "llvmpipe")

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._log_path, "wb") as log_fh:
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    env=env,
                    cwd=str(spec.user_dir),
                )
            except OSError as exc:
                raise StartupError(f"failed to launch Dolphin: {exc}") from exc

        # Wait briefly for the process to start and not immediately exit.
        time.sleep(0.5)
        if self._proc.poll() is not None:
            raise StartupError(
                f"Dolphin exited immediately with code {self._proc.returncode}; "
                f"see log at {self._log_path}"
            )

    # ------------------------------------------------------------------
    # Status / readiness
    # ------------------------------------------------------------------
    def status(self) -> dict:
        spec = self.spec
        running = self.running
        return {
            "instance_id": spec.instance_id,
            "pid": self.pid,
            "running": running,
            "rom": str(spec.rom_path),
            "user_dir": str(spec.user_dir),
            "controller_ports": list(spec.controller_ports),
            "bba_mode": spec.bba_mode,
            "video_backend": self._resolve_video_backend(self.server_config),
            "capture_backend": self.spec.capture_backend,
            "memory_backend": self.spec.memory_backend,
            "controller_ready": self._controller_ready(),
            "memory_ready": self._memory_ready(),
            "display": self.spec.display,
            "log_path": str(self._log_path),
            "started_at": self._started_at,
            "stop_requested": self._stop_requested,
        }

    def _controller_ready(self) -> bool:
        if self._controller is None or not self.running:
            return False
        try:
            # A reader is present only once Dolphin has opened the FIFO's read end.
            return self._controller.probe(0)
        except Exception:
            return False

    def _memory_ready(self) -> bool:
        return self._memory is not None and self._memory.is_ready()

    def wait_ready(self, timeout: float | None = None) -> dict:
        """Wait until the instance is at least running (controller FIFO open)."""
        cfg = self.server_config
        timeout = timeout if timeout is not None else cfg.startup_timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.running:
                break
            if self._controller_ready():
                return self.status()
            time.sleep(0.2)
        return self.status()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def stop(self, timeout: float = 5.0) -> dict:
        self._stop_requested = True
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=timeout)
            except OSError:
                pass

        # Stop the sequence runner and close FIFO writers.
        if self._controller is not None:
            self._controller.close()
        if self._capture is not None:
            self._capture.close()
        if self._xvfb is not None:
            self._xvfb.stop()
        return self.status()

    def close(self) -> None:
        """Release all resources without necessarily killing Dolphin."""
        if self._controller is not None:
            self._controller.close()
        if self._capture is not None:
            self._capture.close()
        if self._xvfb is not None:
            self._xvfb.stop()

    def __repr__(self) -> str:
        return f"<DolphinInstance id={self.spec.instance_id} running={self.running}>"
