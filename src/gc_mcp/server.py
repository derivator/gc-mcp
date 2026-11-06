"""MCP server: control multiple stock Dolphin instances as GameCubes.

The tool surface is deliberately split into two groups:

* ``gc_*``  - the unified interface that also works (or will work) on real
  GameCube hardware.
* ``dolphin_*`` - Dolphin-emulator-only operations (e.g. editing Dolphin's own
  configuration) that cannot apply to hardware.

The server runs over stdio, which is what an MCP client such as opencode uses.
"""

from __future__ import annotations

import atexit
import base64
import os
import signal
import struct
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .config import ServerConfig, shm_usage
from .instances import InstanceManager
from .platform import resolve_platform
from .platform.dolphin import DolphinPlatform

__all__ = ["main", "mcp", "manager", "config"]

#: Global server state.
config = ServerConfig()
manager = InstanceManager(config)


def _shutdown_instances() -> None:
    """Best-effort stop of every running Dolphin subprocess.

    Called from the server's lifespan ``__aexit__`` (i.e. when the stdio
    connection closes and the MCP server is asked to shut down), from a
    SIGTERM/SIGINT handler, and from ``atexit`` as a last resort. ``shutdown()``
    is idempotent, so calling it more than once is safe.
    """
    try:
        manager.shutdown()
    except Exception:
        # Never let cleanup failures mask the shutdown path.
        pass


@asynccontextmanager
async def _server_lifespan(app: MCPServer) -> AsyncIterator[None]:
    """Server lifespan: stop all Dolphin subprocesses on exit."""
    try:
        yield
    finally:
        _shutdown_instances()


#: Best-practice guidance surfaced to the model as server ``instructions`` (the
#: MCP-native way to tell the model how to drive this server effectively).
SERVER_INSTRUCTIONS = (
    "You are controlling a Dolphin GameCube emulator. Each instance is a "
    "separate game running headless under Xvfb.\n\n"
    "Efficiency and latency:\n"
    "- Input actions have real latency (a keystroke/tap reaches the game only on "
    "a later frame). Prefer batching: use gc_controller_sequence to press a run "
    "of buttons in a single atomic call instead of many gc_controller_press / "
    "gc_controller_release round-trips.\n"
    "- Screen capture is slow and expensive (render + encode). Prefer doing "
    "several inputs *blind* (e.g. a whole menu-selection sequence) before "
    "re-checking the screen with gc_capture_screenshot. One capture per "
    "meaningful state change, not per button.\n\n"
    "Memory:\n"
    "- GameCube is big-endian. When reading/writing memory with "
    "gc_memory_read_struct / gc_memory_write_struct, prefix the format with '>' "
    "(or '!'), e.g. \">12f\" for a 3x4 float matrix.\n\n"
    "Emulator control:\n"
    "- Use gc_emulator_toggle_pause to pause (e.g. before inspecting memory), "
    "gc_emulator_step_frame to advance one frame while paused, and "
    "gc_emulator_save_state / gc_emulator_load_state (slots 1-8) to checkpoint.\n"
    "- These are driven by Dolphin hotkeys injected via xdotool; they need the "
    "instance's X display and xdotool installed. gc_emulator_freeze / "
    "gc_emulator_unfreeze are a coarse OS-level pause fallback.\n"
)


mcp = MCPServer("gc-mcp", instructions=SERVER_INSTRUCTIONS, lifespan=_server_lifespan)


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
        "gpu_device": config.gpu_device,
        "gpu_accel": config.gpu_accel,
        "effective_gpu_accel": config.effective_gpu_accel(),
        "vglrun_available": config.vglrun_available(),
        "native_resolution": config.native_resolution,
        "default_video_backend": config.default_video_backend,
        "default_bba_mode": config.default_bba_mode,
        "capture_backend": config.capture_backend,
        "memory_backend": config.memory_backend,
        "instances": len(manager),
    }


@mcp.tool()
def gc_gpu_info() -> dict:
    """Report GPU rendering diagnostics for this host.

    Useful for deciding whether a GameCube instance will render on a real GPU or
    fall back to Mesa software (llvmpipe), and for diagnosing the ``/dev/shm``
    SIGBUS crash that occurs when the shared-memory filesystem is too small for
    Dolphin's emulated RAM.
    """
    return {
        "gpu_available": config.gpu_available,
        "gpu_device": config.gpu_device,
        "gpu_accel": config.gpu_accel,
        "effective_gpu_accel": config.effective_gpu_accel(),
        "vglrun_available": config.vglrun_available(),
        "vgl_3d_display": config.vgl_3d_display,
        "shm": shm_usage(),
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
    native_resolution: str | None = None,
    gpu_accel: str | None = None,
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
        native_resolution: GameCube native framebuffer "WxH" (default 640x480).
        gpu_accel: auto/none/software/vgl/vgl-egl/vgl-glx (default auto).
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
        native_resolution=native_resolution,
        gpu_accel=gpu_accel,
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
                "The display is sized to the GameCube native resolution so the "
                "output has no black borders. Needs Xvfb installed. Optionally "
                "set `crop` (W:H:X:Y) via gc_capture_configure as an escape hatch.",
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
def gc_controller_press_once(instance_id: str, port: int, button: str,
                             hold_duration: float = 200.0) -> dict:
    """Tap a button once: press, hold for hold_duration ms, then release.

    This is the recommended way to send a button press for agent use. It is a
    single atomic action — you do not need to pair gc_controller_press and
    gc_controller_release. The button is always released afterwards.

    hold_duration defaults to 200 ms. Pass a larger value to hold the button
    longer (e.g. to hold START through a loading screen).
    """
    inst = _instance(instance_id)
    inst.controller_press_once(port, button, hold_duration)
    return {"port": port, "button": button, "hold_duration": hold_duration, "state": "released"}


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


@mcp.tool()
def gc_memory_read_struct(instance_id: str, address: int, fmt: str) -> dict:
    """Read memory at ``address`` and unpack it with a Python ``struct`` format.

    ``fmt`` is a Python :mod:`struct` format string, e.g. ``">12f"`` for twelve
    big-endian floats (a 3x4 transform matrix), ``">II"`` for two big-endian
    u32 values, or ``">4h"`` for four big-endian s16 values.

    GameCube is big-endian, so include a byte-order prefix in ``fmt``: ``>``
    (big-endian, standard sizes) or ``!`` (network order). If you omit it the
    format uses the host's native byte order/sizes, which is almost never what
    you want for a GameCube, so it is recommended to always prefix with ``>``.

    The result ``values`` is a list of the unpacked values in format order.
    """
    inst = _instance(instance_id)
    try:
        size = struct.calcsize(fmt)
    except struct.error as exc:
        return {"address": hex(address), "fmt": fmt, "error": f"invalid struct format: {exc}"}
    data = inst.memory_read(address, size)
    values = struct.unpack(fmt, data)
    return {
        "address": hex(address),
        "fmt": fmt,
        "size": size,
        "values": list(values),
    }


@mcp.tool()
def gc_memory_write_struct(instance_id: str, address: int, fmt: str,
                           values: list[Any]) -> dict:
    """Pack ``values`` with a Python ``struct`` format and write them to memory.

    ``fmt`` is a Python :mod:`struct` format string (see
    ``gc_memory_read_struct`` for the byte-order notes). ``values`` is a list of
    the values to pack, in format order, e.g. for ``">12f"`` pass 12 floats.

    Returns the number of bytes written.
    """
    inst = _instance(instance_id)
    try:
        size = struct.calcsize(fmt)
        packed = struct.pack(fmt, *values)
    except struct.error as exc:
        return {"address": hex(address), "fmt": fmt, "error": f"invalid struct format/values: {exc}"}
    inst.memory_write(address, packed)
    return {"address": hex(address), "fmt": fmt, "size": size, "written": len(packed)}


# ---------------------------------------------------------------------------
# Emulator control (pause / step / save / load)
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_emulator_toggle_pause(instance_id: str) -> dict:
    """Toggle the emulator's pause state (Dolphin's Toggle Pause hotkey, F10).

    Use this to pause a game (e.g. before inspecting memory or screen), then
    resume. When paused, ``gc_emulator_step_frame`` advances exactly one frame.

    This injects a keystroke into the instance's X display via ``xdotool``, so
    it requires the ``xvfb`` capture backend and ``xdotool`` installed. On a
    headless ``--batch`` instance Dolphin's ``[Input] BackgroundInput = True``
    keeps the input gate open so the hotkey is received without window focus.
    """
    inst = _instance(instance_id)
    return inst.emulator_toggle_pause()


@mcp.tool()
def gc_emulator_step_frame(instance_id: str) -> dict:
    """Advance exactly one frame when the emulator is paused.

    Sends Dolphin's Frame Advance hotkey. Unlike ``Step Into`` (a CPU-debugger
    step), Frame Advance steps one *video frame*, which is what a TAS tool wants.

    The instance config binds Frame Advance to ``F8`` by default (see
    ``dolphin_hotkey_get``/``dolphin_hotkey_set``). If it is unbound this tool
    returns ``ok=False`` and no frame advances.
    """
    inst = _instance(instance_id)
    return inst.emulator_step_frame()


@mcp.tool()
def gc_emulator_save_state(instance_id: str, slot: int) -> dict:
    """Save a save-state to ``slot`` (1-8) via Shift+F{slot}.

    Only slots 1-8 have a Dolphin default hotkey binding (see
    ``dolphin_hotkey_get``).
    """
    if not 1 <= slot <= 8:
        return {"ok": False, "error": f"slot must be 1-8, got {slot}"}
    inst = _instance(instance_id)
    return inst.emulator_save_state(slot)


@mcp.tool()
def gc_emulator_load_state(instance_id: str, slot: int) -> dict:
    """Load a save-state from ``slot`` (1-8) via F{slot}.

    Only slots 1-8 have a Dolphin default hotkey binding (see
    ``dolphin_hotkey_get``).
    """
    if not 1 <= slot <= 8:
        return {"ok": False, "error": f"slot must be 1-8, got {slot}"}
    inst = _instance(instance_id)
    return inst.emulator_load_state(slot)


@mcp.tool()
def gc_emulator_freeze(instance_id: str) -> dict:
    """Hard-freeze the Dolphin process with SIGSTOP.

    This is a coarse OS-level pause that always works (no X display or xdotool
    needed). It is *not* compatible with frame-advance (the process is suspended,
    so it cannot process hotkeys); use ``gc_emulator_toggle_pause`` for a
    TAS-friendly pause.
    """
    inst = _instance(instance_id)
    return inst.emulator_freeze()


@mcp.tool()
def gc_emulator_unfreeze(instance_id: str) -> dict:
    """Resume a frozen Dolphin process with SIGCONT."""
    inst = _instance(instance_id)
    return inst.emulator_unfreeze()


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


@mcp.tool()
def dolphin_hotkey_get(instance_id: str, key: str) -> dict:
    """Read a Dolphin emulator hotkey binding.

    ``key`` is the Hotkeys.ini key ``<group>/<control>`` (e.g.
    ``General/Toggle Pause``, ``Save State/Save State Slot 1``). Returns the
    current binding expression (``F10``, ``@(Shift+F1)``), or None if the hotkey
    is unbound. This is Dolphin-only (real hardware has no emulator hotkeys).
    """
    from .keyboard import DEFAULT_HOTKEYS, read_hotkeys_ini

    inst = _instance(instance_id)
    path = inst.instance.spec.user_dir / "Config" / "Hotkeys.ini"
    values = read_hotkeys_ini(path)
    if not path.exists():
        # No Hotkeys.ini means Dolphin loads its built-in defaults.
        values = DEFAULT_HOTKEYS
    binding = values.get(key)
    return {
        "instance_id": instance_id,
        "key": key,
        "binding": binding,
        "bound": bool(binding),
    }


@mcp.tool()
def dolphin_hotkey_set(instance_id: str, key: str, expression: str) -> dict:
    """Set (or clear) a Dolphin emulator hotkey binding.

    ``key`` is the Hotkeys.ini key ``<group>/<control>``. ``expression`` is the
    binding in Dolphin input syntax, e.g. ``F8`` for a single key or
    ``@(Shift+F8)`` for a combination. Pass ``expression=""`` to unbind it.

    Dolphin loads ``Hotkeys.ini`` all-or-nothing, so this preserves every
    existing binding and the built-in default set, then applies ``key``. A
    running instance may need a restart for the change to take effect. This is
    Dolphin-only.

    Common keys (see ``gc_emulator_*`` tools): ``General/Toggle Pause``,
    ``Frame Advance/Frame Advance``, ``Save State/Save State Slot 1..8``,
    ``Load State/Load State Slot 1..8``.
    """
    from .keyboard import DEFAULT_HOTKEYS, read_hotkeys_ini, write_hotkeys_ini

    inst = _instance(instance_id)
    path = inst.instance.spec.user_dir / "Config" / "Hotkeys.ini"
    values = DEFAULT_HOTKEYS.copy()
    if path.exists():
        values.update(read_hotkeys_ini(path))
    if expression:
        values[key] = expression
    else:
        values.pop(key, None)
    write_hotkeys_ini(path, values)
    return {
        "instance_id": instance_id,
        "key": key,
        "binding": expression or None,
        "bound": bool(expression),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def _signal_handler(signum: int, _frame: Any) -> None:
    """Stop running Dolphin subprocesses before exiting on SIGTERM/SIGINT."""
    _shutdown_instances()
    # Re-raise the signal's default action so the process terminates with the
    # conventional exit status for that signal (rather than 0).
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def main() -> None:
    """Run the gc-mcp server over stdio."""
    # Ensure the data dirs exist before serving.
    config.ensure_dirs()

    # Make sure any spawned Dolphin subprocesses are stopped when the server is
    # asked to shut down. ``mcp.run()`` blocks until the stdio connection
    # closes; the lifespan hook above handles that path. Signals (SIGTERM /
    # SIGINT) are handled here because they may bypass the lifespan's exit.
    # ``atexit`` is a final fallback for normal interpreter shutdown.
    atexit.register(_shutdown_instances)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    mcp.run()


if __name__ == "__main__":
    main()
