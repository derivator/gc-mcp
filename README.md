# gc-mcp

An MCP (Model Context Protocol) server that drives **multiple, stock, unmodified
Dolphin emulator instances** as if each were a real Nintendo GameCube. It launches
ROM/DOL/ELF instances with custom per-instance configuration (Broadband Adapter
and other peripherals), captures rendered output inside a Docker container, and
manipulates all four GameCube controller ports.

The tool surface is designed around a **unified `gc_` interface** that works on
both the emulator and real GameCube hardware. Anything that can only ever apply
to the emulator (e.g. changing Dolphin's own configuration) is exposed under a
`gc_dolphin_` prefix.

## Why

The model that drives an agent is often too slow to hold a button press/release
pair together, so controller tools support **momentary actions and timed input
sequences**. Memory read/write is provided so the agent can inspect or patch
game state.

## Concepts

- **Platform** — an abstraction over a target: `dolphin` (an emulator instance)
  or `gamecube-hw` (a real console). Hardware instances are **permanently
  remembered** in `<data>/hardware.json` until explicitly removed.
- **Instance** — one running Dolphin process, isolated by its own `--user` data
  directory, with its own controllers, capture backend and memory backend; or a
  registered real GameCube hardware instance.
- **Controller port** — one of the four GameCube controller ports (`0`–`3`).
- **Capture backend** — how rendered frames are grabbed:
  - `xvfb` (default): Dolphin runs under a virtual X server (Xvfb) using OpenGL;
    the display is sized to the GameCube native resolution so the capture has no
    black borders. Frames are captured with `ffmpeg`/`xwd` into PNG. Rendering
    happens on a real GPU (via VirtualGL `vglrun`) when available, else Mesa
    `llvmpipe`.
  - `framedump`: Dolphin's built-in frame dump (`Movie.DumpFrames`) to PNG, as a
    fallback that needs no display server.
  - `v4l2`: grab a frame from a Video4Linux2 capture device (e.g. a capture
    card) for a real GameCube. Uses OpenCV (`cv2`) when installed, else
    `ffmpeg -f v4l2`. A USB capture card only delivers real content after it has
    been fed a continuous stream for a few seconds, so the backend **warms up**
    a cold device (streaming it for `warmup_seconds`) before grabbing a frame.

## GPU rendering

By default `gpu_accel=auto`: if VirtualGL (`vglrun`) is installed and a GPU DRM
device is present under `/dev/dri`, Dolphin is launched under `vglrun -d
<device>` so OpenGL renders on the real GPU instead of Mesa software `llvmpipe`.
The rendered frame is blitted onto the Xvfb display, so captures still come from
`ffmpeg`/`xwd`. Without `vglrun`, it falls back to software rendering.

The `Xvfb` screen is sized to the GameCube native resolution (`640x480` default)
scaled by the instance's `internal_resolution`, so the render window fills the
screen and the capture has no black borders. `crop` (`W:H:X:Y`) is available as
an optional escape hatch via `gc_capture_configure`, but sizing is preferred.

## Controller interface

Dolphin ships a stock **"Pipes"** input backend. The server creates named FIFOs in
the instance's `Pipes/` directory, maps each GameCube port to a device
(`Pipe/0/ctrlN`), and writes newline-delimited commands:

```
PRESS A
RELEASE A
SET MAIN 0.5 0.5
SET C 1.0 0.0
SET L 0.75
```

## Memory interface

Memory read/write is implemented per-PID with `process_vm_readv`/`process_vm_writev`
after locating the instance's emulated RAM (`/dev/shm/dolphinmem*`) in
`/proc/<pid>/maps`. This sidesteps `py-dolphin-memory-engine`'s limitation of
attaching to only the *first* matching process, so multiple instances each get
their own memory backend. `dolphin-memory-engine` is supported as an optional
backend.

Memory operations fail **gracefully**: if the emulator has not yet allocated RAM
or is not running a game, `gc_memory_read`/`gc_memory_write` return an
`is_error` result with a clear message rather than crashing the server.

## Real GameCube hardware

A real console is connected through a capture card / USB capture device exposed
as a Video4Linux2 node (e.g. `/dev/video0`), and up to four serial devices that
will act as the controller ports. Register it once; it is **remembered
permanently** (persisted to `<data>/hardware.json`) until removed:

```bash
gc_hardware_devices            # probe /dev/video* and report capture capability
gc_hardware_register("hw-001", capture_device="/dev/video0")
gc_capture_screenshot("hw-001")   # grab a frame -> PNG
gc_hardware_update("hw-001", serial_devices=["/dev/ttyUSB0", ...])
gc_hardware_status("hw-001")
gc_hardware_remove("hw-001")
```

`gc_hardware_devices` probes each node with `v4l2-ctl` and reports its card
name, the pixel formats it exposes, and whether it can actually **capture video
frames** (`can_capture`). Some nodes expose only UVC **metadata** (a capture
card's second interface) and cannot produce a video stream — prefer a device
with `can_capture: true`.

Only **screen capture** and **ROM loading** are implemented for hardware so far;
controller and memory operations raise "not implemented". Capture uses the `v4l2`
backend, which prefers OpenCV (`cv2`, the same library as `cap_example.py`) for
low-latency capture and falls back to `ffmpeg -f v4l2`.

Some capture cards (e.g. the UGREEN USB one) return a placeholder/black frame
until they have been streamed continuously for a few seconds. To handle this the
backend **warms up** a cold device: on the first capture it streams for
`warmup_seconds` (default `5`) and writes the final frame, then remembers the
device as warm so subsequent captures are fast. A device is re-warmed if it has
not been captured for `warmup_ttl` seconds (default `60`). Tune per instance with
`gc_capture_configure("hw-001", warmup_seconds=5, warmup_ttl=60)`.

### ROM loading (Swiss netboot)

A real console boots a ROM through **Swiss's netboot listener**: a custom Swiss
build listens on TCP **4242** of the console's own **BBA IP**, and a one-line
`BOOT <path>` command starts a game from the console's own media. The path is a
**console-side** path starting with `sdc:/` (SD2SP2) or `fsp:/` (the configured
FSP file server):

```bash
gc_hardware_update("hw-001", bba_ip="192.168.1.61")   # remember the console's IP
gc_rom_load("hw-001", "sdc:/games/Foo.iso")           # boot the game
```

`gc_rom_load` is the **unified** entry point across targets:

* **Real hardware** — sends `BOOT` to the netboot listener. Set `bba_ip` with
  `gc_hardware_register(..., bba_ip=...)` or `gc_hardware_update(...)`; it is
  persisted on the hardware instance. If the console is currently **inside a
  game** the listener is down, so `gc_rom_load` first tries to return to Swiss
  with the A+Z+START combo — which needs hardware button pressing and is **not
  implemented yet** (it raises "not implemented" until then).
* **Dolphin instance** — stops the current process and relaunches it booting a
  **host-side** ROM/DOL/ELF path, keeping the instance id and config.

The netboot protocol is one command per TCP connection (`PING`→`PONG`,
`STATUS`→`READY`/`BUSY`, `BOOT <path>`→`OK`/`ERR <reason>`). See
`~/swiss-gc/NETBOOT_USAGE.md` for the exact protocol and
`~/swiss-gc/NETBOOT_PLAN.md` for the verified setup (console at
`192.168.1.61/24`, `InitNetwork=Yes` in `swiss/settings/global.ini`).

## Running

Requires **mcp v2.x** (the server uses `MCPServer`; `FastMCP` was removed in
mcp 2.x). The project pins `mcp>=1.2.0` but `uv sync` resolves to 2.x; if you
need the v1 API, pin `mcp<2`.

```bash
uv sync
uv run gc-mcp        # stdio transport (what an MCP client / opencode uses)
```

Point your MCP client at `uv run gc-mcp` as a stdio server.

## Configuration

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `GC_MCP_DOLPHIN_BINARY` | `dolphin-emu` | Dolphin executable |
| `GC_MCP_DATA_DIR` | `~/.gc-mcp` | Root for per-instance data & captures |
| `GC_MCP_VIDEO_BACKEND` | `OGL` | `OGL`/`Vulkan`/`Software Renderer` |
| `GC_MCP_BBA_MODE` | `ipc` | BBA mode (`ipc`, `hle`, `xlink`, `tap`, `tapserver`, or empty/`none`) |
| `GC_MCP_BBA_MAC_PREFIX` | `00:09:bf` | Must be `00:09:bf` or `00:17:ab` |
| `GC_MCP_CAPTURE_BACKEND` | `auto` | `auto`, `xvfb`, `framedump` |
| `GC_MCP_NATIVE_RES` | `640x480` | GameCube native framebuffer `WxH` (display/capture sizing) |
| `GC_MCP_GPU_ACCEL` | `auto` | `auto`, `none`, `software`, `vgl`, `vgl-egl`, `vgl-glx` |
| `GC_MCP_GPU_DEVICE` | auto-detected | GPU DRM device path (e.g. `/dev/dri/card2`) |
| `GC_MCP_VGL_3D_DISPLAY` | empty | 3D X server display for VirtualGL's GLX back end (e.g. `:0.0`) |
| `GC_MCP_MEMORY_BACKEND` | `process_vm` | `process_vm` or `dolphin_engine` |
| `GC_MCP_HW_CAPTURE_DEVICE` | `/dev/video0` | Default capture device for real hardware instances |
| `GC_MCP_HW_CAPTURE_WIDTH` | `640` | Default hardware capture width |
| `GC_MCP_HW_CAPTURE_HEIGHT` | `480` | Default hardware capture height |
| `GC_MCP_HW_CAPTURE_FORMAT` | `mjpg` | Default hardware capture pixel format (`mjpg`, `yuyv`, ...) |
| `GC_MCP_HW_CAPTURE_FPS` | `30` | Default hardware capture frame rate |
| `GC_MCP_HW_CAPTURE_FLUSH` | `5` | Stale buffers flushed before reading a fresh frame (OpenCV path) |
| `GC_MCP_HW_CAPTURE_WARMUP` | `5` | Seconds to stream a cold device before grabbing a frame (`0` disables) |
| `GC_MCP_HW_CAPTURE_WARMUP_TTL` | `60` | Seconds a warmed device is trusted before it is warmed again |
| `GC_MCP_HW_BBA_IP` | empty | Default BBA IP for a new hardware instance (the console's own IP, used to reach the Swiss netboot listener) |
| `GC_MCP_HW_BBA_PORT` | `4242` | Default Swiss netboot listener port |

## Docker

A `Dockerfile` is provided that installs Dolphin, Xvfb, Mesa (`llvmpipe`), ffmpeg
and Python, then runs the server over stdio. It is **not** tested in this
environment (no Dolphin display/GPU available), but it is the intended deployment
shape for a headless container.

## Tool reference

All tools take an `instance_id` where relevant.

- `gc_platform_list`, `gc_platform_capabilities`
- `gc_instance_start`, `gc_instance_list`, `gc_instance_status`, `gc_instance_stop`, `gc_instance_remove`
- `gc_capture_backends`, `gc_capture_configure`, `gc_capture_screenshot`
- `gc_hardware_devices`, `gc_hardware_register`, `gc_hardware_update`, `gc_hardware_list`, `gc_hardware_status`, `gc_hardware_remove`
- `gc_controller_press`, `gc_controller_release`, `gc_controller_set_stick`, `gc_controller_set_trigger`, `gc_controller_sequence`, `gc_controller_sequence_cancel`, `gc_controller_get_state`, `gc_controller_reset`
- `gc_memory_read`, `gc_memory_write`
- `gc_dolphin_config_get`, `gc_dolphin_config_set`, `gc_dolphin_config_apply_profile`
- `gc_server_info`, `gc_gpu_info`
