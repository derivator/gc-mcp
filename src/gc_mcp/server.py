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

from .capture import detect_video_devices
from .config import ServerConfig, shm_usage
from .errors import NotFoundError, OcrError
from .instances import HardwareManager, InstanceManager
from .ocr import extract_text
from .ocr import status as ocr_status
from .platform import resolve_platform
from .platform.dolphin import DolphinPlatform
from .platform.gamecube_hw import GameCubeHWPlatform

__all__ = ["config", "hardware", "main", "manager", "mcp"]

#: Global server state.
config = ServerConfig()
manager = InstanceManager(config)
#: Persistent registry of real GameCube hardware instances.
hardware = HardwareManager(config)


def _startup_instances() -> None:
    """Verify the controller serial devices of registered GameCube hardware.

    Called when the server's lifespan starts (i.e. when the stdio connection
    opens and the MCP server comes up). Each hardware instance with serial
    devices is probed with the firmware ``ID`` command so a dead or wrong device
    is caught early, and its buttons are reset so nothing is left held.
    """
    try:
        hardware.startup()
    except Exception:  # noqa: BLE001, S110 - a startup probe failure must not stop the server coming up
        # Never let a startup probe failure prevent the server coming up.
        pass


def _shutdown_instances() -> None:
    """Best-effort stop of every running Dolphin subprocess and release of any
    real GameCube hardware controller resources.

    Called from the server's lifespan ``__aexit__`` (i.e. when the stdio
    connection closes and the MCP server is asked to shut down), from a
    SIGTERM/SIGINT handler, and from ``atexit`` as a last resort. ``shutdown()``
    is idempotent, so calling it more than once is safe.

    For hardware instances this releases the controller backends, which resets
    any held buttons on the serial devices before closing them.
    """
    try:
        manager.shutdown()
    except Exception:  # noqa: BLE001, S110 - cleanup failures must not mask shutdown
        # Never let cleanup failures mask the shutdown path.
        pass
    try:
        hardware.shutdown()
    except Exception:  # noqa: BLE001, S110 - cleanup failures must not mask shutdown
        # Never let cleanup failures mask the shutdown path.
        pass


@asynccontextmanager
async def _server_lifespan(app: MCPServer) -> AsyncIterator[None]:
    """Server lifespan: verify hardware serials on start, stop everything on exit."""
    _startup_instances()
    try:
        yield
    finally:
        _shutdown_instances()


#: Best-practice guidance surfaced to the model as server ``instructions`` (the
#: MCP-native way to tell the model how to drive this server effectively).
SERVER_INSTRUCTIONS = (
    "You are controlling GameCube targets. Each instance is either a Dolphin "
    "emulator running headless under Xvfb (``dolphin:<id>``) or a registered "
    "real GameCube console (``gamecube-hw:<id>``, see gc_instance_create_hardware).\n\n"
    "Efficiency and latency:\n"
    "- Input actions have real latency (a keystroke/tap reaches the game only on "
    "a later frame). Prefer batching: use gc_controller_sequence to press a run "
    "of buttons in a single atomic call instead of many gc_controller_press / "
    "gc_controller_release round-trips.\n"
    "- For analog menu navigation, use gc_controller_flick (the stick counterpart "
    "of gc_controller_press_once) to push the stick and return it to centre in "
    "one call.\n"
    "- Screen capture is slow and expensive (render + encode). Prefer doing "
    "several inputs *blind* (e.g. a whole menu-selection sequence) before "
    "re-checking the screen with gc_capture_screenshot. One capture per "
    "meaningful state change, not per button.\n\n"
    "Memory:\n"
    "- GameCube is big-endian. When reading/writing memory with "
    "gc_memory_read_struct / gc_memory_write_struct, prefix the format with '>' "
    "(or '!'), e.g. \">12f\" for a 3x4 float matrix.\n\n"
    "Screen reading:\n"
    "- Pass ocr=True to gc_capture_screenshot to extract on-screen text with "
    "RapidOCR; it returns recognised lines plus per-word boxes. OCR is "
    "optional (needs the 'ocr' extra); if unavailable the screenshot is still "
    "returned with ocr.available=false and an error.\n\n"
    "Emulator control:\n"
    "- Use gc_dolphin_toggle_pause to pause (e.g. before inspecting memory), "
    "gc_dolphin_step_frame to advance one frame while paused, and "
    "gc_dolphin_save_state / gc_dolphin_load_state (slots 1-8) to checkpoint. "
    "gc_dolphin_save_state also copies the state file into the states folder "
    "(<data>/states, or pass copy_dir) so it survives instance removal.\n"
    "- These are driven by Dolphin hotkeys injected via xdotool; they need the "
    "instance's X display and xdotool installed. gc_dolphin_freeze / "
    "gc_dolphin_unfreeze are a coarse OS-level pause fallback.\n"
    "- Use gc_log (optionally tail=/grep=) to inspect the instance's Dolphin log "
    "for crashes or ROM console output (OSReport).\n\n"
    "IPL boot:\n"
    "- gc_instance_create_emulator(..., boot_ipl=True) creates a Dolphin "
    "instance that boots the original GameCube IPL (disables 'Skip System "
    "Menu'), so the boot logo/menu plays before the disc. It only works for a "
    "GameCube disc ('.iso'/'.gcm') and only if the server has an IPL folder "
    "configured (see gc_server_info).\n\n"
    "Real GameCube hardware:\n"
    "- Register a connected console with gc_instance_create_hardware(capture_device=...). "
    "Screen capture is implemented via the 'v4l2' backend; ROM loading via "
    "Swiss's netboot listener; controller and memory operations are not yet "
    "supported. gc_capture_screenshot on a hardware instance grabs a frame from "
    "the capture device. Use gc_hardware_devices / gc_capture_backends to list "
    "available devices and pick one whose 'can_capture' is true (some nodes "
    "expose only UVC metadata and cannot produce a video stream).\n\n"
    "ROM loading on real hardware:\n"
    "- The console runs a custom Swiss build with a netboot listener on TCP 4242 "
    "of its own BBA IP. Set that IP with gc_hardware_update(instance_id, "
    "bba_ip='<console-ip>') (it is stored on the hardware instance).\n"
    "- gc_instance_start(instance_id, rom_path) boots a ROM. For hardware the "
    "path must be a console-side path starting with 'sdc:/' (SD2SP2) or 'fsp:/' "
    "(the configured FSP file server), e.g. 'sdc:/games/Foo.iso'. It stops the "
    "game currently running (returns to Swiss) and the game may take a while to "
    "load after the BOOT is accepted.\n"
    "- If the console is inside a game the netboot listener is down; "
    "gc_instance_start first tries to return to Swiss with the A+Z+START combo, "
    "which needs hardware button pressing (not implemented yet), so it raises "
    "'not implemented' until then.\n"
    "- gc_instance_start on a Dolphin instance instead stops and relaunches it "
    "booting a host-side ROM path.\n"
)


mcp = MCPServer("gc-mcp", instructions=SERVER_INSTRUCTIONS, lifespan=_server_lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _instance(instance_id: str) -> GameCubeHWPlatform | DolphinPlatform:
    """Resolve an ``instance_id`` to a platform (Dolphin or real hardware).

    A bare id is checked against the running Dolphin instances first, then the
    registered hardware instances. This lets the unified ``gc_*`` tools accept
    either kind of instance id without a prefix.
    """
    dol = manager.get_or_none(instance_id)
    if dol is not None:
        return DolphinPlatform(dol)
    hw = hardware.get_or_none(instance_id)
    if hw is not None:
        return GameCubeHWPlatform(hw)
    raise NotFoundError(f"unknown instance {instance_id!r}")


def _captures_dir() -> Path:
    config.ensure_dirs()
    return config.captures_dir


def _ocr_result(path: Path, lang: str | None) -> dict:
    """Run OCR on a saved capture, reporting failures inside the result.

    The screenshot has already been written, so an unavailable OCR dependency
    should not discard a successful capture; return the error as part of the
    result instead of raising.
    """
    try:
        return extract_text(path, lang=lang or config.ocr_lang)
    except OcrError as exc:
        return {"available": False, "error": str(exc)}


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
        "ipl_available": config.ipl_available(),
        "ipl_regions": config.ipl_regions(),
        "ipl_dir": str(config.ipl_dir) if config.ipl_dir else None,
        "ocr": ocr_status(),
        "ocr_lang": config.ocr_lang,
        "hardware_file": str(config.hardware_file),
        "hardware_instances": len(hardware),
        "instances": len(manager),
    }


@mcp.tool()
def gc_gpu_info() -> dict:
    """Report GPU rendering diagnostics for this host.

    Shows whether instances render on a real GPU or fall back to software, and
    reports shared-memory (``/dev/shm``) usage.
    """
    # llvmpipe means the host fell back to Mesa software rendering. A too-small
    # /dev/shm can SIGBUS Dolphin when it backs emulated RAM with shared memory.
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
    """List available platforms: running Dolphin instances and registered hardware."""
    platforms = []
    for inst in manager.list():
        platforms.append(
            {
                "platform": f"dolphin:{inst['instance_id']}",
                "kind": "dolphin",
                "instance_id": inst["instance_id"],
                "info": inst.get("info", ""),
                "running": inst["running"],
                "pid": inst["pid"],
            }
        )
    for hw in hardware.list():
        platforms.append(
            {
                "platform": f"gamecube-hw:{hw['instance_id']}",
                "kind": "gamecube-hw",
                "instance_id": hw["instance_id"],
                "info": hw.get("info", ""),
                "capture_device": hw["capture_device"],
                "capture_ready": hw["capture_ready"],
                "serial_devices": hw["serial_devices"],
                "bba_ip": hw["bba_ip"],
                "bba_port": hw["bba_port"],
            }
        )
    if not platforms:
        platforms.append(
            {
                "platform": "gamecube-hw",
                "kind": "gamecube-hw",
                "supported": False,
                "note": "No instances registered. Start a Dolphin instance with "
                "gc_instance_start or register real hardware with "
                "gc_instance_create_hardware.",
            }
        )
    return platforms


@mcp.tool()
def gc_platform_capabilities(platform: str) -> dict:
    """Describe what a platform supports. ``platform`` is ``dolphin:<id>`` or ``gamecube-hw:<id>``."""
    plat = resolve_platform(platform, manager, hardware)
    return plat.describe()


# ---------------------------------------------------------------------------
# Instance lifecycle
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_instance_start(instance_id: str, rom_path: str) -> dict:
    """Boot or reload a ROM on an existing instance.

    For a Dolphin instance this restarts it with ``rom_path`` (a host-side
    .iso/.gcm/.dol/.elf path). For real hardware, ``rom_path`` is a console-side
    Swiss path (``sdc:/`` or ``fsp:/``). If no instance with ``instance_id``
    exists, a default Dolphin instance is created and started.
    """
    # Dolphin: stop any running game and relaunch this instance with the new ROM
    # (keeping its id, data dir and configuration). Hardware: send a BOOT to the
    # console's Swiss netboot listener (TCP 4242 on its bba_ip); the call returns
    # as soon as Swiss accepts it, not when the game finishes loading.
    dol = manager.get_or_none(instance_id)
    if dol is not None:
        return dol.relaunch(rom_path)
    hw = hardware.get_or_none(instance_id)
    if hw is not None:
        return GameCubeHWPlatform(hw).rom_load(rom_path)
    # Unknown id: create a new Dolphin instance with server defaults.
    return manager.start(instance_id, rom_path)


@mcp.tool()
def gc_instance_create_emulator(
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
    boot_ipl: bool = False,
    info: str | None = None,
) -> dict:
    """Create a Dolphin instance and its configuration without booting it.

    Registers the instance so it appears in ``gc_instance_list`` /
    ``gc_instance_status``. Use it to pre-configure an instance (e.g. with
    ``gc_dolphin_config_set``) and boot it later with ``gc_instance_start``.
    ``rom_path`` is a host-side .iso/.gcm/.dol/.elf path.
    """
    return manager.create(
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
        boot_ipl=boot_ipl,
        info=info,
    )


@mcp.tool()
def gc_instance_create_hardware(
    instance_id: str,
    capture_device: str | None = None,
    capture: dict[str, Any] | None = None,
    serial_devices: list[str] | None = None,
    bba_ip: str | None = None,
    bba_port: int | None = None,
    info: str | None = None,
) -> dict:
    """Register a real GameCube hardware instance.

    The instance is persisted until removed with ``gc_instance_remove``. Set
    ``capture_device`` to a V4L2 node, ``serial_devices`` for controller ports
    (0-3), and ``bba_ip``/``bba_port`` for the Swiss netboot listener. ``info``
    is a free-form annotation.
    """
    # ``capture`` accepts V4L2 options: {width, height, format, fps,
    # flush_frames, warmup_seconds, warmup_ttl}. ``warmup_seconds`` streams a
    # cold capture card before grabbing a frame (0 disables); prefer a
    # ``capture_device`` with can_capture: true from gc_hardware_devices.
    return hardware.register(
        instance_id,
        capture_device=capture_device,
        capture=capture,
        serial_devices=serial_devices,
        bba_ip=bba_ip,
        bba_port=bba_port,
        info=info,
    )


@mcp.tool()
def gc_instance_list() -> list[dict]:
    """List all known Dolphin and real-hardware instances with their status."""
    entries: list[dict] = []
    for inst in manager.list():
        entries.append({**inst, "kind": "dolphin"})
    for hw in hardware.list():
        entries.append({**hw, "kind": "gamecube-hw"})
    return entries


@mcp.tool()
def gc_instance_status(instance_id: str) -> dict:
    """Get detailed status for one instance (Dolphin or real hardware)."""
    dol = manager.get_or_none(instance_id)
    if dol is not None:
        return manager.status(instance_id)
    hw = hardware.get_or_none(instance_id)
    if hw is not None:
        return hardware.status(instance_id)
    raise NotFoundError(f"unknown instance {instance_id!r}")


@mcp.tool()
def gc_instance_stop(instance_id: str, timeout: float = 5.0) -> dict:
    """Stop a running instance.

    Dolphin: stops the emulator process. Hardware: returns the console to the
    Swiss menu (a real console cannot be powered off remotely).
    """
    # A real console can only be returned to Swiss; it cannot be powered off
    # over the netboot listener, so hardware stops via A+Z+START.
    dol = manager.get_or_none(instance_id)
    if dol is not None:
        return manager.stop(instance_id, timeout=timeout)
    hw = hardware.get_or_none(instance_id)
    if hw is not None:
        return hw.return_to_swiss()
    raise NotFoundError(f"unknown instance {instance_id!r}")


@mcp.tool()
def gc_instance_remove(instance_id: str) -> dict:
    """Stop and delete an instance.

    Dolphin: stops the emulator and deletes its data directory. Hardware: forgets
    the registered console; the console itself is unaffected.
    """
    dol = manager.get_or_none(instance_id)
    if dol is not None:
        return manager.remove(instance_id)
    hw = hardware.get_or_none(instance_id)
    if hw is not None:
        return hardware.remove(instance_id)
    raise NotFoundError(f"unknown instance {instance_id!r}")


# ---------------------------------------------------------------------------
# Real GameCube hardware instances
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_hardware_devices() -> list[dict]:
    """List the Video4Linux2 capture devices on this host.

    Each entry shows the node, card name, formats, and whether it can capture
    video (``can_capture``). Prefer a node with ``can_capture: true`` as a
    hardware instance's ``capture_device``.
    """
    # Some nodes expose only UVC metadata and cannot produce a video stream,
    # so ``can_capture`` filters those out.
    return detect_video_devices()


@mcp.tool()
def gc_hardware_update(
    instance_id: str,
    capture_device: str | None = None,
    capture: dict[str, Any] | None = None,
    serial_devices: list[str] | None = None,
    bba_ip: str | None = None,
    bba_port: int | None = None,
    info: str | None = None,
) -> dict:
    """Update a registered hardware instance's settings and persist them.

    Use it to set ``bba_ip``/``bba_port`` for netboot, or change the capture
    device, serial ports, or ``info``.
    """
    return hardware.update(
        instance_id,
        capture_device=capture_device,
        capture=capture,
        serial_devices=serial_devices,
        bba_ip=bba_ip,
        bba_port=bba_port,
        info=info,
    )


@mcp.tool()
def gc_log(instance_id: str, tail: int | None = None, grep: str | None = None) -> dict:
    """Read an instance's log (crashes, ROM console output, warnings).

    ``grep`` keeps lines containing the (case-insensitive) substring; ``tail``
    limits to the last N matching lines. Returns the lines plus metadata.
    """
    # The log lives at <user_dir>/dolphin.log.
    inst = _instance(instance_id)
    return inst.read_log(tail=tail, grep=grep)


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
            {
                "name": "v4l2",
                "description": "Capture a frame from a Video4Linux2 device "
                "(e.g. a capture card) for a real GameCube hardware instance. "
                "Uses OpenCV (cv2) if installed, else ffmpeg.",
            },
        ],
        "video_devices": detect_video_devices(),
        "auto_resolution": config.resolve_capture_backend(),
    }


@mcp.tool()
def gc_capture_configure(instance_id: str, **opts: Any) -> dict:
    """Configure an instance's capture backend (display, screen_size, format, ...).

    For hardware this updates the V4L2 capture device/resolution/format and
    persists it; ``opts`` may include a ``capture`` dict of options.
    """
    inst = _instance(instance_id)
    return inst.capture_configure(**opts)


@mcp.tool()
def gc_capture_screenshot(instance_id: str, filename: str | None = None,
                          include_base64: bool = False,
                          ocr: bool = False,
                          ocr_lang: str | None = None) -> dict:
    """Capture the current frame of an instance to a PNG.

    Returns the path to the saved PNG, and optionally base64-encoded image data.
    Set ``ocr=True`` to also extract on-screen text with RapidOCR (optional;
    requires the ``ocr`` extra). ``ocr_lang`` is a RapidOCR language code
    (common Tesseract codes work too), defaulting to ``GC_MCP_OCR_LANG``
    (``en``).
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
    if ocr:
        # OCR runs on the saved PNG, so it works the same for every backend
        # (xvfb, frame dump or a V4L2 capture card).
        result["ocr"] = _ocr_result(path, ocr_lang)
    return result


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_controller_press(instance_id: str, port: int, button: str) -> dict:
    """Press and hold a button on a controller port.

    Buttons: A, B, X, Y, Z, START, L, R, D_UP, D_DOWN, D_LEFT, D_RIGHT. Release
    it with gc_controller_release (or use gc_controller_sequence).
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
    """Tap a button once: press, hold ``hold_duration`` ms, then release.

    A single atomic action — no need to pair press/release, and the button is
    always released. ``hold_duration`` defaults to 200 ms; use a larger value to
    hold through a loading screen.
    """
    inst = _instance(instance_id)
    inst.controller_press_once(port, button, hold_duration)
    return {"port": port, "button": button, "hold_duration": hold_duration, "state": "released"}


@mcp.tool()
def gc_controller_flick(instance_id: str, port: int, stick: str,
                        x: float, y: float, hold_duration: float = 200.0) -> dict:
    """Flick an analog stick: push to (x, y), hold, then return to centre.

    The analog counterpart to gc_controller_press_once — one atomic action for
    menu navigation. ``stick`` is 'main' or 'c'; x/y are in -1..1 (0,0 = centre);
    ``hold_duration`` defaults to 200 ms. The stick is always re-centred.
    """
    inst = _instance(instance_id)
    inst.controller_flick(port, stick, x, y, hold_duration)
    return {"port": port, "stick": stick, "x": x, "y": y, "hold_duration": hold_duration,
            "state": "centered"}


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
    Use gc_controller_sequence_cancel to stop it.
    """
    # The sequence runs on a background thread; the returned sequence_id is used
    # to cancel it.
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

    ``address`` is an emulated address (e.g. 0x80000000 = start of MEM1). Returns
    the data as a hex string.
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

    ``data`` is a hex string (e.g. "00010203"); ``address`` is an emulated
    address.
    """
    inst = _instance(instance_id)
    raw = bytes.fromhex(data)
    inst.memory_write(address, raw)
    return {"address": hex(address), "length": len(raw), "written": len(raw)}


@mcp.tool()
def gc_memory_read_struct(instance_id: str, address: int, fmt: str) -> dict:
    """Read memory and unpack it with a Python ``struct`` format.

    ``fmt`` is a :mod:`struct` format string. GameCube is big-endian, so prefix
    it with ``>`` (or ``!``), e.g. ``">12f"`` for twelve floats or ``">II"`` for
    two u32s. Returns the values in format order.
    """
    # Without a '>'/'!' prefix struct uses the host's native byte order/sizes,
    # which is almost never right for a GameCube; always prefix with '>'.
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

    ``fmt`` is a :mod:`struct` format string (prefix with ``>`` for big-endian;
    see ``gc_memory_read_struct``). ``values`` are packed in format order.
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
def gc_dolphin_toggle_pause(instance_id: str) -> dict:
    """Toggle the emulator's pause state (Dolphin's Toggle Pause, F10).

    Pause a game (e.g. before inspecting memory or screen), then resume. When
    paused, ``gc_dolphin_step_frame`` advances exactly one frame.
    """
    # Injects the hotkey keystroke into the instance's X display via xdotool, so
    # it needs the xvfb capture backend and xdotool. Dolphin's
    # [Input] BackgroundInput = True keeps the input gate open without focus.
    inst = _instance(instance_id)
    return inst.emulator_toggle_pause()


@mcp.tool()
def gc_dolphin_step_frame(instance_id: str) -> dict:
    """Advance exactly one video frame while paused.

    Uses Dolphin's Frame Advance hotkey (F8 by default; see
    ``gc_dolphin_hotkey_get`` / ``gc_dolphin_hotkey_set``). Returns ``ok=False``
    if the hotkey is unbound.
    """
    # Frame Advance steps one *video frame*, unlike Step Into (a CPU-debugger
    # step). If unbound, no frame advances.
    inst = _instance(instance_id)
    return inst.emulator_step_frame()


@mcp.tool()
def gc_dolphin_save_state(instance_id: str, slot: int,
                          copy_dir: str | None = None) -> dict:
    """Save a save-state to ``slot`` (1-8) and copy it out.

    Without ``copy_dir`` the state is copied to the server's states directory
    (``<data>/states``) so it survives instance removal. The result includes
    ``copied``, ``state_file`` and ``copied_to``.
    """
    # Only slots 1-8 have Dolphin default hotkey bindings (Shift+F{slot}).
    if not 1 <= slot <= 8:
        return {"ok": False, "error": f"slot must be 1-8, got {slot}"}
    inst = _instance(instance_id)
    dest = Path(copy_dir) if copy_dir else None
    return inst.emulator_save_state(slot, dest)


@mcp.tool()
def gc_dolphin_load_state(instance_id: str, slot: int) -> dict:
    """Load a save-state from ``slot`` (1-8)."""
    # Load uses Dolphin's default F{slot} hotkey binding.
    if not 1 <= slot <= 8:
        return {"ok": False, "error": f"slot must be 1-8, got {slot}"}
    inst = _instance(instance_id)
    return inst.emulator_load_state(slot)


@mcp.tool()
def gc_dolphin_freeze(instance_id: str) -> dict:
    """Hard-freeze the Dolphin process.

    A coarse OS-level pause that always works, but frame-advance is unavailable
    while frozen; use ``gc_dolphin_toggle_pause`` for TAS-style stepping.
    """
    # SIGSTOP needs no X display or xdotool, but the suspended process cannot
    # process the frame-advance hotkey.
    inst = _instance(instance_id)
    return inst.emulator_freeze()


@mcp.tool()
def gc_dolphin_unfreeze(instance_id: str) -> dict:
    """Resume a frozen Dolphin process."""
    # SIGCONT resumes a process suspended by gc_dolphin_freeze (SIGSTOP).
    inst = _instance(instance_id)
    return inst.emulator_unfreeze()


# ---------------------------------------------------------------------------
# Dolphin-only configuration tools
# ---------------------------------------------------------------------------
@mcp.tool()
def gc_dolphin_config_get(instance_id: str, section: str, key: str) -> dict:
    """Read a Dolphin.ini configuration value (Dolphin-only)."""

    inst = _instance(instance_id)
    ini = inst.instance.spec.user_dir / "Config" / "Dolphin.ini"
    import configparser

    cp = configparser.ConfigParser()
    cp.read(ini)
    value = cp.get(section, key, fallback=None)
    return {"instance_id": instance_id, "section": section, "key": key, "value": value}


@mcp.tool()
def gc_dolphin_config_set(instance_id: str, section: str, key: str, value: str) -> dict:
    """Set a Dolphin.ini configuration value (Dolphin-only)."""
    # Writes to the instance's Config/Dolphin.ini.
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
def gc_dolphin_config_apply_profile(instance_id: str, profile: dict[str, dict[str, str]]) -> dict:
    """Apply a Dolphin.ini profile (section -> {key: value}; Dolphin-only).

    Existing keys in a section are preserved unless the profile overrides them.
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
def gc_dolphin_hotkey_get(instance_id: str, key: str) -> dict:
    """Read a Dolphin hotkey binding (Dolphin-only).

    ``key`` is ``<group>/<control>`` (e.g. ``General/Toggle Pause``). Returns the
    binding expression (e.g. ``F10``, ``@(Shift+F1)``) or None if unbound.
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
def gc_dolphin_hotkey_set(instance_id: str, key: str, expression: str) -> dict:
    """Set or clear a Dolphin hotkey binding (Dolphin-only).

    ``key`` is ``<group>/<control>``; ``expression`` is Dolphin input syntax
    (``F8``, ``@(Shift+F8)``); pass ``""`` to unbind. A running instance may need
    a restart for the change to take effect.
    """
    # Dolphin loads Hotkeys.ini all-or-nothing, so start from the built-in
    # defaults, layer any existing file on top, then apply this key. Common keys
    # used by gc_* tools: General/Toggle Pause, Frame Advance/Frame Advance,
    # Save State/Save State Slot 1..8, Load State/Load State Slot 1..8.
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
