"""Instance registry / lifecycle manager.

Tracks all running Dolphin instances, allocates unique data directories and X
display numbers, and routes operations to the right instance.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Any

from ..config import ServerConfig
from ..errors import AlreadyExistsError, NotFoundError
from .dolphin_instance import DolphinInstance, InstanceSpec

#: A sane instance id (used for validation).
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")

#: First X display number we hand out for Xvfb.
_BASE_DISPLAY = 90


class InstanceManager:
    """Owns the set of Dolphin instances."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._instances: dict[str, DolphinInstance] = {}
        self._display_counter = _BASE_DISPLAY

    # ------------------------------------------------------------------
    # Validation / allocation
    # ------------------------------------------------------------------
    def _validate_id(self, instance_id: str) -> str:
        instance_id = str(instance_id)
        if not _ID_RE.match(instance_id):
            raise ValueError(
                f"invalid instance_id {instance_id!r}; use letters, digits, "
                "underscores or dashes (max 64 chars)"
            )
        return instance_id

    def _next_display(self) -> int:
        display = self._display_counter
        self._display_counter += 1
        return display

    def _instance_dir(self, instance_id: str) -> Path:
        return self.config.instances_dir / instance_id

    def _normalize_ports(self, ports: Any) -> tuple[int, ...]:
        if ports is None:
            return (0,)
        if isinstance(ports, int):
            ports = (ports,)
        result = []
        for p in ports:
            p = int(p)
            if p not in (0, 1, 2, 3):
                raise ValueError(f"invalid controller port {p!r}; expected 0-3")
            result.append(p)
        return tuple(sorted(set(result))) or (0,)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self, instance_id: str, rom_path: str | Path, *, controller_ports=None,
              bba_mode: str | None = None, bba_mac: str | None = None,
              video_backend: str | None = None, capture_backend: str | None = None,
              memory_backend: str | None = None, internal_resolution: int | None = None,
              native_resolution: str | None = None, gpu_accel: str | None = None,
              extra_core: dict | None = None, extra_pad: dict | None = None,
              extra_args: list | None = None, boot_ipl: bool = False,
              info: str | None = None, **kwargs) -> dict:
        """Create and start a new Dolphin instance."""
        instance_id = self._validate_id(instance_id)
        if instance_id in self._instances:
            raise AlreadyExistsError(f"instance {instance_id!r} already exists")

        rom = Path(rom_path).expanduser().resolve()
        if not rom.is_file():
            raise NotFoundError(f"ROM/DOL/ELF file not found: {rom}")

        user_dir = self._instance_dir(instance_id)
        user_dir.mkdir(parents=True, exist_ok=True)

        ports = self._normalize_ports(controller_ports)
        display = self._next_display() if capture_backend != "framedump" else None

        spec = InstanceSpec(
            instance_id=instance_id,
            info=str(info) if info is not None else "",
            rom_path=rom,
            user_dir=user_dir,
            controller_ports=ports,
            bba_mode=bba_mode,
            bba_mac=bba_mac,
            video_backend=video_backend or self.config.default_video_backend,
            capture_backend=capture_backend or self.config.capture_backend,
            memory_backend=memory_backend or self.config.memory_backend,
            display=display,
            internal_resolution=internal_resolution,
            native_resolution=native_resolution,
            gpu_accel=gpu_accel,
            extra_core=extra_core or {},
            extra_pad=extra_pad or {},
            extra_args=extra_args or [],
            boot_ipl=boot_ipl,
        )

        instance = DolphinInstance(spec, self.config)
        # Register first so a concurrent status/list sees it even while booting.
        self._instances[instance_id] = instance
        try:
            status = instance.start()
            instance.wait_ready()
        except Exception:
            # Clean up a partially-started instance so it doesn't linger.
            self._instances.pop(instance_id, None)
            instance.close()
            raise

        return status

    def get(self, instance_id: str) -> DolphinInstance:
        instance = self._instances.get(instance_id)
        if instance is None:
            raise NotFoundError(f"unknown instance {instance_id!r}")
        return instance

    def get_or_none(self, instance_id: str) -> DolphinInstance | None:
        return self._instances.get(instance_id)

    def list(self) -> list[dict]:
        return [i.status() for i in self._instances.values()]

    def status(self, instance_id: str) -> dict:
        return self.get(instance_id).status()

    def stop(self, instance_id: str, timeout: float = 5.0) -> dict:
        instance = self.get(instance_id)
        return instance.stop(timeout=timeout)

    def remove(self, instance_id: str) -> dict:
        """Stop and remove an instance, and delete its data directory."""
        instance = self._instances.get(instance_id)
        if instance is None:
            raise NotFoundError(f"unknown instance {instance_id!r}")
        instance.stop()
        self._instances.pop(instance_id, None)
        # Remove the per-instance data directory.
        import shutil

        user_dir = instance.spec.user_dir
        if user_dir.exists():
            shutil.rmtree(user_dir, ignore_errors=True)
        return {"instance_id": instance_id, "removed": True}

    def shutdown(self) -> None:
        """Stop and clean up all instances."""
        for instance in list(self._instances.values()):
            try:
                instance.stop()
            except Exception:
                pass
        self._instances.clear()

    def __len__(self) -> int:
        return len(self._instances)
