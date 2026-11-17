"""One real GameCube hardware instance.

Unlike a :class:`DolphinInstance` (which spawns and owns an emulator process), a
hardware instance represents a *physical* console that is permanently connected
to this host. It has no process to launch or stop; instead it is *registered*
once and then remembered by the server (persisted to ``<data>/hardware.json``)
until it is explicitly removed.

Its settings are:

* a Video4Linux2 **capture device** (e.g. ``/dev/video0``) used to grab frames
  from the console's video output, and
* up to four **serial devices** that will eventually act as the GameCube
  controller ports.

For now only the screen capture is implemented (via the ``v4l2`` capture
backend); controller and memory operations raise ``UnsupportedError``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..capture import V4L2Capture
from ..config import ServerConfig
from ..controllers import HardwareController
from ..errors import NetbootError
from ..netboot import DEFAULT_TIMEOUT, NetbootClient, validate_rom_path

#: Maximum number of serial devices (one per GameCube controller port).
MAX_SERIAL_DEVICES = 4

#: Default netboot listener port (Swiss listens on TCP 4242).
DEFAULT_BBA_PORT = 4242

#: How long (seconds) to wait for the Swiss netboot listener to come back up
#: after the A+Z+START return-to-Swiss combo before giving up. The console
#: resets and reboots Swiss, which takes a few seconds before the listener
#: starts answering.
RETURN_TO_SWISS_WAIT = 20.0

#: Poll interval (seconds) while waiting for the listener to come back up.
_RETURN_TO_SWISS_POLL = 1.0


@dataclass
class HardwareInstanceSpec:
    """Persistent settings for one real GameCube hardware instance."""

    instance_id: str
    #: Free-form description/annotation shown in list and status output.
    info: str = ""
    #: Video4Linux2 capture device path (e.g. ``/dev/video0``).
    capture_device: str = "/dev/video0"
    #: Capture backend options (width/height/format/fps/flush_frames and the
    #: warm-up settings ``warmup_seconds``/``warmup_ttl``).
    capture: dict[str, Any] = field(default_factory=dict)
    #: Up to four serial device paths that act as the GameCube controllers.
    serial_devices: list[str] = field(default_factory=list)
    #: The console's own BBA IP, used to reach the Swiss netboot listener for
    #: ``gc_rom_load`` (e.g. ``192.0.2.10``). None until configured.
    bba_ip: str | None = None
    #: The netboot listener port on the console (default 4242).
    bba_port: int = DEFAULT_BBA_PORT
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "info": self.info,
            "capture_device": self.capture_device,
            "capture": dict(self.capture),
            "serial_devices": list(self.serial_devices),
            "bba_ip": self.bba_ip,
            "bba_port": self.bba_port,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class HardwareInstance:
    """A registered real GameCube console and its capture backend."""

    def __init__(self, spec: HardwareInstanceSpec, server_config: ServerConfig,
                 persist: Callable[[], None] | None = None) -> None:
        self.spec = spec
        self.server_config = server_config
        #: Called after a mutation so the manager can persist the registry.
        self._persist = persist
        self._controller: HardwareController | None = None
        #: Per-port result of the last ``HardwareController.verify()`` (ID probe),
        #: populated on server startup; ``None`` until verified.
        self._controller_reports: list[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Controller verification (startup ID probe)
    # ------------------------------------------------------------------
    def set_controller_reports(self, reports: list[dict[str, Any]]) -> None:
        """Record the per-port result of a controller ``verify()`` probe."""
        self._controller_reports = list(reports)

    def controller_verified(self) -> bool:
        """Whether every configured serial device passed the startup ID probe.

        Returns True if there are no serial devices to verify, or if every
        probed device answered with a valid identify line.
        """
        if not self.spec.serial_devices:
            return True
        reports = self._controller_reports
        if reports is None or len(reports) != len(self.spec.serial_devices):
            return False
        return all(r.get("ok") for r in reports)

    # ------------------------------------------------------------------
    # Controller backend (serial devices)
    # ------------------------------------------------------------------
    @property
    def controller(self) -> HardwareController:
        devices = list(self.spec.serial_devices)
        if self._controller is None:
            self._controller = HardwareController(devices)
        elif (isinstance(self._controller, HardwareController)
              and self._controller.serial_devices != devices):
            # The configured serial devices changed; rebuild the backend so the
            # new ports are used. (A test/other backend swapped in directly is
            # left untouched.)
            self._controller.close()
            self._controller = HardwareController(devices)
        return self._controller

    # ------------------------------------------------------------------
    # ROM loading (Swiss netboot)
    # ------------------------------------------------------------------
    def netboot_client(self, timeout: float | None = None) -> NetbootClient:
        return NetbootClient(
            self.spec.bba_ip,
            self.spec.bba_port,
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
        )

    def rom_load(self, rom_path: str, *, timeout: float | None = None) -> dict[str, Any]:
        """Boot a ROM on the console via Swiss's netboot listener (TCP 4242).

        ``rom_path`` is a *console-side* path (``sdc:/`` or ``fsp:/``). If the
        console is currently inside a game the netboot listener is down, so this
        first returns to Swiss with the A+Z+START combo and waits for the
        listener to come back up before booting.

        Raises :class:`NetbootError` if the ``BOOT`` command is not answered with
        ``OK`` — in particular if the listener returns *no reply* at all (which
        usually means the console is mid-boot or the connection was dropped), or
        if it refuses the ROM (``ERR <reason>``).
        """
        if not self.spec.bba_ip:
            raise NetbootError(
                "this hardware instance has no BBA IP configured; set it with "
                "gc_hardware_update(instance_id, bba_ip='<console-ip>')"
            )
        path = validate_rom_path(rom_path)
        client = self.netboot_client(timeout=timeout)
        # If the listener is down the console is inside a game; get back to Swiss.
        if not client.reachable():
            self._return_to_swiss()
            self._wait_for_swiss(client)
        status = client.status()
        if status.upper() == "BUSY":
            return {
                "instance_id": self.spec.instance_id,
                "kind": "gamecube-hw",
                "rom_path": path,
                "bba": self._bba(),
                "status": "BUSY",
                "booted": False,
                "reply": status,
                "note": "a boot is already in progress; wait for STATUS to return "
                "to READY before booting again",
            }
        reply = client.boot(path)
        if not reply.strip():
            raise NetbootError(
                f"the Swiss netboot listener at {self._bba()} returned no reply "
                f"to 'BOOT {path}'"
            )
        if not reply.upper().startswith("OK"):
            raise NetbootError(
                f"Swiss refused to boot {path!r}: {reply}"
            )
        return {
            "instance_id": self.spec.instance_id,
            "kind": "gamecube-hw",
            "rom_path": path,
            "bba": self._bba(),
            "status": status,
            "reply": reply,
            "booted": True,
        }

    def _bba(self) -> str:
        return f"{self.spec.bba_ip}:{self.spec.bba_port}"

    def _return_to_swiss(self) -> None:
        """Return to Swiss from inside a game by pressing A+Z+START.

        The three buttons are pressed together (the serial controller holds them
        via its repeater thread), held briefly so Swiss can catch the combo, then
        released. Pressing on a port with no serial device raises
        :class:`ControllerError`.
        """
        buttons = ("A", "Z", "START")
        for button in buttons:
            self.controller.press(0, button)
        time.sleep(0.2)
        for button in reversed(buttons):
            self.controller.release(0, button)

    def _wait_for_swiss(self, client: NetbootClient) -> None:
        """Wait for the Swiss netboot listener to come back up.

        After ``_return_to_swiss`` presses A+Z+START the console resets and
        reboots Swiss, which takes a few seconds before its netboot listener
        starts answering. Poll the listener for up to :data:`RETURN_TO_SWISS_WAIT`
        seconds, raising :class:`NetbootError` if it never comes up.
        """
        deadline = time.monotonic() + RETURN_TO_SWISS_WAIT
        while time.monotonic() < deadline:
            if client.reachable():
                return
            time.sleep(_RETURN_TO_SWISS_POLL)
        raise NetbootError(
            "returned to Swiss with the A+Z+START combo but the netboot "
            "listener never came back up; the console may still be booting or "
            "the combo was not caught"
        )

    # ------------------------------------------------------------------
    # Capture backend (built lazily from the spec so configure() stays in sync)
    # ------------------------------------------------------------------
    @property
    def capture(self) -> V4L2Capture:
        return V4L2Capture(
            device=self.spec.capture_device,
            width=int(self.spec.capture.get("width", self.server_config.hw_capture_width)),
            height=int(self.spec.capture.get("height", self.server_config.hw_capture_height)),
            format=self.spec.capture.get("format", self.server_config.hw_capture_format),
            fps=int(self.spec.capture.get("fps", self.server_config.hw_capture_fps)),
            flush_frames=int(self.spec.capture.get("flush_frames", self.server_config.hw_capture_flush)),
            warmup_seconds=float(self.spec.capture.get("warmup_seconds", self.server_config.hw_capture_warmup)),
            warmup_ttl=float(self.spec.capture.get("warmup_ttl", self.server_config.hw_capture_warmup_ttl)),
        )

    def capture_configure(self, **opts: Any) -> dict:
        """Update the capture settings and persist them.

        Accepts the same keys as :meth:`V4L2Capture.configure` (``device``,
        ``width``, ``height``, ``format``, ``fps``, ``flush_frames``,
        ``warmup_seconds``, ``warmup_ttl``) plus a ``capture`` dict of capture
        options. Returns the effective config.
        """
        if "device" in opts and opts["device"] is not None:
            self.spec.capture_device = str(opts["device"])
        capture = dict(self.spec.capture)
        if isinstance(opts.get("capture"), dict):
            capture.update(opts["capture"])
        for key in ("width", "height", "format", "fps", "flush_frames",
                    "warmup_seconds", "warmup_ttl"):
            if key in opts and opts[key] is not None:
                capture[key] = opts[key]
        self.spec.capture = capture
        self.spec.updated_at = _now()
        self._save()
        return self.capture.configure(**opts)

    def capture_screenshot(self, dest_path: Path | str) -> Path:
        return self.capture.capture(Path(dest_path))

    def _save(self) -> None:
        if self._persist is not None:
            self._persist()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    def close(self) -> None:
        """Release the controller backend (serial ports + repeater thread)."""
        if self._controller is not None:
            self._controller.close()
            self._controller = None

    def capture_ready(self) -> bool:
        """Whether the capture device currently exists and is a char device."""
        device = Path(self.spec.capture_device)
        if not device.exists():
            return False
        try:
            import stat

            return stat.S_ISCHR(device.stat().st_mode)
        except OSError:
            return False

    def status(self) -> dict[str, Any]:
        return {
            "instance_id": self.spec.instance_id,
            "kind": "gamecube-hw",
            "info": self.spec.info,
            "capture_device": self.spec.capture_device,
            "capture": {
                **self.spec.capture,
                "device": self.spec.capture_device,
                "backend": "v4l2",
                "warmup_seconds": float(self.spec.capture.get("warmup_seconds", self.server_config.hw_capture_warmup)),
                "warmup_ttl": float(self.spec.capture.get("warmup_ttl", self.server_config.hw_capture_warmup_ttl)),
            },
            "serial_devices": list(self.spec.serial_devices),
            "bba_ip": self.spec.bba_ip,
            "bba_port": self.spec.bba_port,
            "capture_ready": self.capture_ready(),
            "controller": {
                "verified": self.controller_verified(),
                "reports": self._controller_reports,
            },
            "created_at": self.spec.created_at,
            "updated_at": self.spec.updated_at,
        }

    def __repr__(self) -> str:
        return f"<HardwareInstance id={self.spec.instance_id} device={self.spec.capture_device}>"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
