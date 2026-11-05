"""MCP server: control multiple stock Dolphin instances as GameCubes.

The tool surface is deliberately split into two groups:

* ``gc_*``  - the unified interface that also works (or will work) on real
  GameCube hardware.
* ``dolphin_*`` - Dolphin-emulator-only operations (e.g. editing Dolphin's own
  configuration) that cannot apply to hardware.

The server runs over stdio, which is what an MCP client such as opencode uses.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import ServerConfig
from .instances import InstanceManager
from .platform import resolve_platform
from .platform.dolphin import DolphinPlatform

__all__ = ["main", "mcp", "manager", "config"]

#: Global server state.
config = ServerConfig()
manager = InstanceManager(config)

mcp = MCPServer("gc-mcp")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _instance(instance_id: str) -> DolphinPlatform:
    return DolphinPlatform(manager.get(instance_id))


def _captures_dir() -> Path:
    config.ensure_dirs()
    return config.captures_dir


# ---------------------------------------------------------------------------
# Server info
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_server_info() -> dict:
    """Return information about this gc-mcp server and its environment."""
    return {
        "name": "gc-mcp",
        "version": "0.1.0",
        "dolphin_binary": config.dolphin_binary,
        "base_dir": str(config.base_dir),
        "gpu_available": config.gpu_available,
        "default_video_backend": config.default_video_backend,
        "default_bba_mode": config.default_bba_mode,
        "capture_backend": config.capture_backend,
        "memory_backend": config.memory_backend,
        "instances": len(manager),
    }


# ---------------------------------------------------------------------------
# Platform
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_platform_list() -> list[dict]:
    """List the platforms currently available to control."""
    platforms = []
    for inst in manager.list():
        platforms.append(
            {
                "platform": f"dolphin:{inst['instance_id']}",
                "kind": "dolphin",
                "instance_id": inst["instance_id"],
                "running": inst["running"],
                "pid": inst["pid"],
            }
        )
    platforms.append(
        {
            "platform": "gamecube-hw",
            "kind": "gamecube-hw",
            "supported": False,
            "note": "Real GameCube hardware control is not implemented yet.",
        }
    )
    return platforms


@mcp.tool()
def gc_platform_capabilities(platform: str) -> dict:
    """Describe the capabilities of a platform.

    ``platform`` may be ``dolphin:<instance_id>`` or ``gamecube-hw``.
    """
    plat = resolve_platform(platform, manager)
    return plat.describe()


# ---------------------------------------------------------------------------
# Instance lifecycle
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_instance_start(
    instance_id: str,
    rom_path: str,
    controller_ports: list[int] | None = None,
    bba_mode: str | None = None,
    bba_mac: str | None = None,
    video_backend: str | None = None,
    capture_backend: str | None = None,
    memory_backend: str | None = None,
    internal_resolution: int | None = None,
    extra_core: dict[str, str] | None = None,
) -> dict:
    """Start a new Dolphin instance booting the given ROM/DOL/ELF.

    Args:
        instance_id: Unique id for the instance.
        rom_path: Path to a .iso/.gcm/.dol/.elf file.
        controller_ports: Which GameCube ports (0-3) get a controller (default [0]).
        bba_mode: Broadband Adapter mode: ipc, hle, xlink, tap, tapserver, modem,
            or None/empty to disable.
        bba_mac: MAC address for the BBA (must start with 00:09:bf or 00:17:ab).
        video_backend: OGL, Vulkan, Null, or Software Renderer.
        capture_backend: auto, xvfb, or framedump.
        memory_backend: process_vm or dolphin_engine.
        internal_resolution: 1x/2x/... internal resolution.
        extra_core: Extra Dolphin.ini [Core] overrides.
    """
    return manager.start(
        instance_id,
        rom_path,
        controller_ports=controller_ports,
        bba_mode=bba_mode,
        bba_mac=bba_mac,
        video_backend=video_backend,
        capture_backend=capture_backend,
        memory_backend=memory_backend,
        internal_resolution=internal_resolution,
        extra_core=extra_core,
    )


@mcp.tool()
def gc_instance_list() -> list[dict]:
    """List all known instances and their status."""
    return manager.list()


@mcp.tool()
def gc_instance_status(instance_id: str) -> dict:
    """Get detailed status for one instance."""
    return manager.status(instance_id)


@mcp.tool()
def gc_instance_stop(instance_id: str, timeout: float = 5.0) -> dict:
    """Stop a running Dolphin instance."""
    return manager.stop(instance_id, timeout=timeout)


@mcp.tool()
def gc_instance_remove(instance_id: str) -> dict:
    """Stop and delete an instance and its data directory."""
    return manager.remove(instance_id)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_capture_backends() -> dict:
    """List the available capture backends and how to use them."""
    return {
        "backends": [
            {
                "name": "xvfb",
                "description": "Capture the X display (Xvfb) with ffmpeg/xwd. "
                "Needs Xvfb installed.",
            },
            {
                "name": "framedump",
                "description": "Use Dolphin's built-in frame dump to PNG. "
                "No display server needed.",
            },
        ],
        "auto_resolution": config.resolve_capture_backend(),
    }


@mcp.tool()
def gc_capture_configure(instance_id: str, **opts: Any) -> dict:
    """Configure the capture backend of an instance (e.g. display, screen_size)."""
    inst = _instance(instance_id)
    result = inst.instance.capture.configure(**opts)
    return result


@mcp.tool()
def gc_capture_screenshot(instance_id: str, filename: str | None = None,
                          include_base64: bool = False) -> dict:
    """Capture the current frame of an instance to a PNG.

    Returns the path to the saved PNG, and optionally base64-encoded image data.
    """
    inst = _instance(instance_id)
    captures = _captures_dir()
    if filename:
        dest = captures / filename
        if not dest.suffix:
            dest = dest.with_suffix(".png")
    else:
        dest = captures / f"{instance_id}-{int(time.time() * 1000)}.png"
    path = inst.capture_screenshot(dest)
    result: dict[str, Any] = {"path": str(path), "filename": path.name}
    if include_base64:
        result["data_base64"] = base64.b64encode(path.read_bytes()).decode("ascii")
        result["mime_type"] = "image/png"
    return result


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_controller_press(instance_id: str, port: int, button: str) -> dict:
    """Press (hold) a button on a GameCube controller port.

    Buttons: A, B, X, Y, Z, START, L, R, D_UP, D_DOWN, D_LEFT, D_RIGHT.
    Remember to call gc_controller_release (or use gc_controller_sequence).
    """
    inst = _instance(instance_id)
    inst.controller_press(port, button)
    return {"port": port, "button": button, "state": "pressed"}


@mcp.tool()
def gc_controller_release(instance_id: str, port: int, button: str) -> dict:
    """Release a button on a GameCube controller port."""
    inst = _instance(instance_id)
    inst.controller_release(port, button)
    return {"port": port, "button": button, "state": "released"}


@mcp.tool()
def gc_controller_set_stick(instance_id: str, port: int, stick: str,
                            x: float, y: float) -> dict:
    """Set a stick position (analog).

    stick is 'main' or 'c'. x/y are in -1..1 (0,0 = centre).
    """
    inst = _instance(instance_id)
    inst.controller_set_stick(port, stick, x, y)
    return {"port": port, "stick": stick, "x": x, "y": y}


@mcp.tool()
def gc_controller_set_trigger(instance_id: str, port: int, trigger: str,
                              value: float) -> dict:
    """Set an analog trigger value (0..1). trigger is 'L' or 'R'."""
    inst = _instance(instance_id)
    inst.controller_set_trigger(port, trigger, value)
    return {"port": port, "trigger": trigger, "value": value}


@mcp.tool()
def gc_controller_sequence(instance_id: str, port: int, steps: list[dict],
                           loop: bool = False) -> dict:
    """Run a timed input sequence.

    Each step is a dict:
      {"t": ms, "type": "press"|"release", "button": "A"}
      {"t": ms, "type": "stick", "stick": "main", "x": 0.0, "y": 1.0}
      {"t": ms, "type": "trigger", "trigger": "L", "value": 1.0}
      {"t": ms, "type": "wait"}
      {"t": ms, "type": "reset"}
    The sequence runs on a background thread. Use gc_controller_sequence_cancel
    to stop it.
    """
    inst = _instance(instance_id)
    return inst.controller_sequence(port, steps, loop=loop)


@mcp.tool()
def gc_controller_sequence_cancel(instance_id: str, sequence_id: str) -> dict:
    """Cancel a running input sequence."""
    inst = _instance(instance_id)
    cancelled = inst.controller_sequence_cancel(sequence_id)
    return {"sequence_id": sequence_id, "cancelled": cancelled}


@mcp.tool()
def gc_controller_get_state(instance_id: str, port: int) -> dict:
    """Return the current state of a GameCube controller port."""
    inst = _instance(instance_id)
    return inst.controller_get_state(port)


@mcp.tool()
def gc_controller_reset(instance_id: str, port: int) -> dict:
    """Reset a GameCube controller port to its neutral state."""
    inst = _instance(instance_id)
    inst.controller_reset(port)
    return {"port": port, "reset": True}


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_memory_read(instance_id: str, address: int, length: int) -> dict:
    """Read bytes from the emulated GameCube RAM.

    address is a Dolphin emulated address (e.g. 0x80000000 for the start of
    MEM1). Returns a hex string. Fails gracefully if memory is not available.
    """
    inst = _instance(instance_id)
    data = inst.memory_read(address, length)
    return {
        "address": hex(address),
        "length": length,
        "data": data.hex(),
        "bytes": len(data),
    }


@mcp.tool()
def gc_memory_write(instance_id: str, address: int, data: str) -> dict:
    """Write bytes to the emulated GameCube RAM.

    data is a hex string (e.g. "00010203"). address is a Dolphin emulated
    address. Fails gracefully if memory is not available.
    """
    inst = _instance(instance_id)
    raw = bytes.fromhex(data)
    inst.memory_write(address, raw)
    return {"address": hex(address), "length": len(raw), "written": len(raw)}


# ---------------------------------------------------------------------------
# Dolphin-only configuration tools
# ---------------------------------------------------------------------------
@mcp.tool()
def dolphin_config_get(instance_id: str, section: str, key: str) -> dict:
    """Read a single Dolphin.ini configuration value for an instance.

    This is Dolphin-only (real hardware has no such config).
    """
    from .instances.config_builder import InstanceConfig

    inst = _instance(instance_id)
    ini = inst.instance.spec.user_dir / "Config" / "Dolphin.ini"
    import configparser

    cp = configparser.ConfigParser()
    cp.read(ini)
    value = cp.get(section, key, fallback=None)
    return {"instance_id": instance_id, "section": section, "key": key, "value": value}


@mcp.tool()
def dolphin_config_set(instance_id: str, section: str, key: str, value: str) -> dict:
    """Set a single Dolphin.ini configuration value for an instance.

    Writes to the instance's Config/Dolphin.ini. This is Dolphin-only.
    """
    inst = _instance(instance_id)
    ini = inst.instance.spec.user_dir / "Config" / "Dolphin.ini"
    import configparser

    cp = configparser.ConfigParser()
    cp.read(ini)
    if not cp.has_section(section):
        cp.add_section(section)
    cp.set(section, key, value)
    with open(ini, "w", encoding="utf-8") as fh:
        cp.write(fh)
    return {"instance_id": instance_id, "section": section, "key": key, "value": value}


@mcp.tool()
def dolphin_config_apply_profile(instance_id: str, profile: dict[str, dict[str, str]]) -> dict:
    """Apply a whole Dolphin.ini profile (a dict of section -> {key: value}).

    This is Dolphin-only. Existing keys in the same section are preserved unless
    overridden by the profile.
    """
    inst = _instance(instance_id)
    ini = inst.instance.spec.user_dir / "Config" / "Dolphin.ini"
    import configparser

    cp = configparser.ConfigParser()
    cp.read(ini)
    applied = 0
    for section, kv in profile.items():
        if not cp.has_section(section):
            cp.add_section(section)
        for key, value in kv.items():
            cp.set(section, key, value)
            applied += 1
    with open(ini, "w", encoding="utf-8") as fh:
        cp.write(fh)
    return {"instance_id": instance_id, "keys_written": applied}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    """Run the gc-mcp server over stdio."""
    # Ensure the data dirs exist before serving.
    config.ensure_dirs()
    mcp.run()


if __name__ == "__main__":
    main()
