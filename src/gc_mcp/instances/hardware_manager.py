"""Persistent registry for real GameCube hardware instances.

Hardware instances are *permanently remembered*: they are persisted to
``<data>/hardware.json`` so that a server restart does not forget a connected
console. They are only forgotten when explicitly removed via
:meth:`HardwareManager.remove` (exposed as ``gc_hardware_remove``).

A hardware instance has no subprocess to manage (unlike a Dolphin instance), so
this manager is mostly about registration, validation, persistence and routing
to the right :class:`HardwareInstance`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..config import ServerConfig
from ..errors import AlreadyExistsError, NotFoundError
from .hardware_instance import MAX_SERIAL_DEVICES, HardwareInstance, HardwareInstanceSpec, _now

#: A sane instance id (shared with the Dolphin manager).
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class HardwareManager:
    """Owns the set of registered real GameCube hardware instances."""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._instances: dict[str, HardwareInstance] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _path(self) -> Path:
        return self.config.hardware_file

    def _load(self) -> None:
        path = self._path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A corrupt registry file should not take the server down. Preserve
            # the existing file and start with an empty set.
            return
        for entry in data.get("instances", []):
            try:
                spec = HardwareInstanceSpec(**entry)
                spec.capture_device = str(spec.capture_device)
                spec.serial_devices = [str(s) for s in spec.serial_devices]
                spec.capture = dict(spec.capture)
                spec.bba_port = int(spec.bba_port)
            except (TypeError, ValueError):
                continue
            if not _ID_RE.match(spec.instance_id):
                continue
            inst = HardwareInstance(spec, self.config, persist=self._save)
            self._instances[spec.instance_id] = inst

    def _save(self) -> None:
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "instances": [spec.to_dict() for inst in self._instances.values()
                          for spec in (inst.spec,)],
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Validation / normalisation
    # ------------------------------------------------------------------
    def _validate_id(self, instance_id: str) -> str:
        instance_id = str(instance_id)
        if not _ID_RE.match(instance_id):
            raise ValueError(
                f"invalid instance_id {instance_id!r}; use letters, digits, "
                "underscores or dashes (max 64 chars)"
            )
        return instance_id

    def _normalise_capture(self, capture: dict[str, Any] | None) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if isinstance(capture, dict):
            for key in ("width", "height", "format", "fps", "flush_frames",
                        "warmup_seconds", "warmup_ttl"):
                if key in capture and capture[key] is not None:
                    out[key] = capture[key]
        # Apply configured defaults for anything not supplied.
        out.setdefault("width", self.config.hw_capture_width)
        out.setdefault("height", self.config.hw_capture_height)
        out.setdefault("format", self.config.hw_capture_format)
        out.setdefault("fps", self.config.hw_capture_fps)
        out.setdefault("flush_frames", self.config.hw_capture_flush)
        out.setdefault("warmup_seconds", self.config.hw_capture_warmup)
        out.setdefault("warmup_ttl", self.config.hw_capture_warmup_ttl)
        return out

    def _normalise_serial(self, devices: Any) -> list[str]:
        if devices is None:
            return []
        if isinstance(devices, str):
            devices = [devices]
        result = []
        for d in devices:
            d = str(d).strip()
            if not d:
                continue
            result.append(d)
        if len(result) > MAX_SERIAL_DEVICES:
            raise ValueError(
                f"too many serial devices ({len(result)}); a GameCube has "
                f"at most {MAX_SERIAL_DEVICES} controller ports"
            )
        return result

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def register(self, instance_id: str, *, capture_device: str | None = None,
                 capture: dict[str, Any] | None = None,
                 serial_devices: list[str] | None = None,
                 bba_ip: str | None = None, bba_port: int | None = None) -> dict:
        """Register (and remember) a real GameCube hardware instance."""
        instance_id = self._validate_id(instance_id)
        if instance_id in self._instances:
            raise AlreadyExistsError(f"hardware instance {instance_id!r} already exists")

        now = _now()
        spec = HardwareInstanceSpec(
            instance_id=instance_id,
            capture_device=str(capture_device or self.config.hw_capture_device),
            capture=self._normalise_capture(capture),
            serial_devices=self._normalise_serial(serial_devices),
            bba_ip=str(bba_ip) if bba_ip is not None else self.config.hw_bba_ip,
            bba_port=int(bba_port) if bba_port is not None else self.config.hw_bba_port,
            created_at=now,
            updated_at=now,
        )
        inst = HardwareInstance(spec, self.config, persist=self._save)
        self._instances[instance_id] = inst
        self._save()
        return inst.status()

    def get(self, instance_id: str) -> HardwareInstance:
        inst = self._instances.get(instance_id)
        if inst is None:
            raise NotFoundError(f"unknown hardware instance {instance_id!r}")
        return inst

    def get_or_none(self, instance_id: str) -> HardwareInstance | None:
        return self._instances.get(instance_id)

    def update(self, instance_id: str, *, capture_device: str | None = None,
               capture: dict[str, Any] | None = None,
               serial_devices: list[str] | None = None,
               bba_ip: str | None = None, bba_port: int | None = None) -> dict:
        """Update an existing hardware instance's settings and persist them."""
        inst = self.get(instance_id)
        if capture_device is not None:
            inst.spec.capture_device = str(capture_device)
        if capture is not None:
            inst.spec.capture = self._normalise_capture(capture)
        if serial_devices is not None:
            inst.spec.serial_devices = self._normalise_serial(serial_devices)
        if bba_ip is not None:
            inst.spec.bba_ip = str(bba_ip)
        if bba_port is not None:
            inst.spec.bba_port = int(bba_port)
        inst.spec.updated_at = _now()
        self._save()
        return inst.status()

    def list(self) -> list[dict]:
        return [i.status() for i in self._instances.values()]

    def status(self, instance_id: str) -> dict:
        return self.get(instance_id).status()

    def remove(self, instance_id: str) -> dict:
        """Stop and forget a hardware instance (and any capture handles)."""
        inst = self._instances.get(instance_id)
        if inst is None:
            raise NotFoundError(f"unknown hardware instance {instance_id!r}")
        self._instances.pop(instance_id, None)
        self._save()
        return {"instance_id": instance_id, "removed": True}

    def shutdown(self) -> None:
        """Release capture resources. Hardware instances have no subprocess."""
        self._instances.clear()

    def __len__(self) -> int:
        return len(self._instances)
