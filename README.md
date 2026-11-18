# gc-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server that
drives one or more **GameCube targets** — stock, unmodified **Dolphin** emulator
instances, or a **real GameCube console** — through a single, unified tool
interface.

Each target ("instance") gets its own controller ports, screen capture and (for
the emulator) memory access, so an AI agent can boot a game, press buttons,
inspect state and take screenshots, all over MCP. No patched Dolphin build is
required.

## Capabilities

- **Multiple instances** — run several isolated Dolphin emulators at once, each
  with its own data directory and configuration.
- **One unified interface** — the same `gc_*` tools drive emulators and real
  hardware. Features that only make sense for the emulator live under a
  `gc_dolphin_` prefix.
- **Controller input** — all four ports, with digital buttons, analog sticks and
  triggers, atomic taps and stick flicks, and timed input sequences.
- **Screen capture** — save the current frame to PNG from a headless Xvfb
  display, Dolphin's frame dump, or a Video4Linux2 capture card.
- **Memory access** — read and write emulated GameCube RAM, with `struct`-based
  helpers for typed values.
- **Emulator control** — pause, single-frame step, and save/load state.
- **Real hardware** — register a physical console with a capture card and serial
  controller adapters, boot games over Swiss netboot, and drive its controller.
- **Diagnostics** — inspect the server, GPU and instance logs to debug rendering
  or crashes.

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended)
- **Dolphin** (`dolphin-emu`) on `PATH`, for emulator instances
- For headless capture: **Xvfb** and **ffmpeg**
- Optional: a GPU plus [VirtualGL](https://virtualgl.org/) (`vglrun`) for
  hardware rendering, [OpenCV](https://opencv.org/) for faster V4L2 capture, and
  `dolphin-memory-engine` for the alternate memory backend

The server requires **mcp 2.x** (it uses `MCPServer`).

## Installation

```bash
uv sync
```

This creates the virtual environment and installs the `gc-mcp` entry point.
To add the optional memory backend:

```bash
uv sync --extra memory
```

## Usage

Run the server over stdio:

```bash
uv run gc-mcp
```

Then point an MCP client at that command. For example, in an `opencode`
config (`opencode.jsonc`):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "gc-mcp": {
        "type": "local",
        "command": ["uv", "run", "gc-mcp"]
      }
    }
  }
}
```

### Boot a game (emulator)

```python
gc_instance_start("game-1", "/roms/Zelda.iso")
```

That is enough to get going: if no instance named `game-1` exists,
`gc_instance_start` creates and starts one with the server defaults. To customise
it first (controller ports, BBA, video backend, …), create it explicitly and then
start it:

```python
gc_instance_create_emulator("game-1", "/roms/Zelda.iso", controller_ports=[0, 1])
gc_instance_start("game-1", "/roms/Zelda.iso")
gc_capture_screenshot("game-1")
```

`gc_instance_start` also **relaunches** an existing instance with a new ROM.
Note that it stops whatever is currently running first.

### Drive the controller

```python
gc_controller_press_once("game-1", 0, "A")       # tap A on port 0
gc_controller_flick("game-1", 0, "main", 0, 1)    # push stick up, then re-centre
gc_controller_press("game-1", 0, "B")             # hold B…
gc_controller_release("game-1", 0, "B")           # …and release
```

For precise timing use `gc_controller_sequence`, which runs on a background
thread:

```python
gc_controller_sequence("game-1", 0, [
    {"t": 0,   "type": "press",   "button": "START"},
    {"t": 200, "type": "release", "button": "START"},
    {"t": 500, "type": "stick",   "stick": "main", "x": 0, "y": 1},
    {"t": 700, "type": "reset"},
])
```

### Inspect memory

```python
gc_memory_read("game-1", 0x80000000, 16)              # hex bytes
gc_memory_read_struct("game-1", 0x80000000, ">12f")   # 12 big-endian floats
```

The GameCube is big-endian, so prefix `struct` formats with `>` (or `!`).

### Manage instances

```python
gc_instance_list()
gc_instance_status("game-1")
gc_instance_stop("game-1")
gc_instance_remove("game-1")
```

## Real GameCube hardware

A physical console is registered once and remembered until removed. It needs a
Video4Linux2 **capture device** for video and, optionally, up to four **serial
devices** that act as the controller ports.

```python
gc_hardware_devices()                        # find capture nodes
gc_instance_create_hardware(
    "tv-rig",
    capture_device="/dev/video0",
    serial_devices=["/dev/ttyUSB0"],
    bba_ip="192.0.2.10",                     # for Swiss netboot
)
gc_capture_screenshot("tv-rig")
gc_controller_press_once("tv-rig", 0, "A")
```

**ROM loading** uses a custom Swiss build that listens for netboot on TCP 4242 of
the console's own BBA IP. Paths are console-side (`sdc:/` for SD2SP2, `fsp:/`
for the file server), not host paths:

```python
gc_instance_start("tv-rig", "sdc:/games/Foo.iso")
```

If the console is already inside a game, `gc_instance_start` returns to Swiss by
pressing A+Z+START over the serial controller on port 0, then boots the new ROM.
Loading is asynchronous: the call returns when Swiss *accepts* the boot, not when
the game reaches its title screen — poll `gc_instance_status` or
`gc_capture_screenshot` to confirm.

Memory access and emulator controls (pause, step, save states) are emulator-only
and report "not supported" for hardware.

## Configuration

Everything is configured through environment variables (all optional).
Per-instance settings passed to `gc_instance_create_emulator` override the
emulation defaults below.

### Emulation

| Variable | Default | Description |
|---|---|---|
| `GC_MCP_DOLPHIN_BINARY` | `dolphin-emu` | Dolphin executable |
| `GC_MCP_DATA_DIR` | `~/.gc-mcp` | Root for instance data, captures and states |
| `GC_MCP_VIDEO_BACKEND` | `OGL` | `OGL`, `Vulkan` or `Software Renderer` |
| `GC_MCP_BBA_MODE` | `ipc` | Broadband Adapter mode: `ipc`, `hle`, `xlink`, `tap`, `tapserver`, `none` |
| `GC_MCP_BBA_MAC_PREFIX` | `00:09:bf` | BBA MAC prefix (`00:09:bf` or `00:17:ab`) |
| `GC_MCP_CONTROLLER` | `standard` | Controller type |
| `GC_MCP_IPL_DIR` | empty | Folder of `IPL.bin` files (`USA/`, `JAP/`, `EUR/`) to enable original IPL boot |
| `GC_MCP_STARTUP_TIMEOUT` | `20` | Seconds to wait for an instance to become ready |
| `GC_MCP_FIFO_RETRIES` | `200` | Controller FIFO open retries |
| `GC_MCP_FIFO_RETRY_DELAY` | `0.05` | Delay between FIFO retries (seconds) |

### Capture and rendering

| Variable | Default | Description |
|---|---|---|
| `GC_MCP_CAPTURE_BACKEND` | `auto` | `auto`, `xvfb` or `framedump` |
| `GC_MCP_NATIVE_RES` | `640x480` | GameCube native framebuffer size (`WxH`) |
| `GC_MCP_GPU_ACCEL` | `auto` | `auto`, `none`, `software`, `vgl`, `vgl-egl`, `vgl-glx` |
| `GC_MCP_GPU_DEVICE` | auto-detected | DRM device, e.g. `/dev/dri/card2` |
| `GC_MCP_VGL_3D_DISPLAY` | empty | 3D X display for VirtualGL's GLX backend, e.g. `:0.0` |
| `GC_MCP_MEMORY_BACKEND` | `process_vm` | `process_vm` or `dolphin_engine` |

### Real hardware defaults

| Variable | Default | Description |
|---|---|---|
| `GC_MCP_HW_CAPTURE_DEVICE` | `/dev/video0` | Default capture device |
| `GC_MCP_HW_CAPTURE_WIDTH` | `640` | Default capture width |
| `GC_MCP_HW_CAPTURE_HEIGHT` | `480` | Default capture height |
| `GC_MCP_HW_CAPTURE_FORMAT` | `mjpg` | Pixel format (`mjpg`, `yuyv`, …) |
| `GC_MCP_HW_CAPTURE_FPS` | `30` | Capture frame rate |
| `GC_MCP_HW_CAPTURE_FLUSH` | `5` | Stale buffers flushed before a fresh frame |
| `GC_MCP_HW_CAPTURE_WARMUP` | `5` | Seconds to stream a cold device before capturing (`0` disables) |
| `GC_MCP_HW_CAPTURE_WARMUP_TTL` | `60` | Seconds a warmed device is trusted before re-warming |
| `GC_MCP_HW_BBA_IP` | empty | Default console BBA IP |
| `GC_MCP_HW_BBA_PORT` | `4242` | Swiss netboot listener port |

Use `gc_server_info` and `gc_gpu_info` to see the effective configuration.

## Docker

A `Dockerfile` is included that installs Dolphin, Xvfb, Mesa, ffmpeg and Python,
then runs the server over stdio — the intended shape for a headless container. It
is provided as a starting point rather than a tested image.

## Tool reference

All tools that act on a target take an `instance_id`.

**Server & diagnostics**
- `gc_server_info`, `gc_gpu_info`
- `gc_platform_list`, `gc_platform_capabilities`

**Instances**
- `gc_instance_create_emulator`, `gc_instance_create_hardware`
- `gc_instance_start`, `gc_instance_list`, `gc_instance_status`,
  `gc_instance_stop`, `gc_instance_remove`

**Capture**
- `gc_capture_backends`, `gc_capture_configure`, `gc_capture_screenshot`

**Controllers**
- `gc_controller_press`, `gc_controller_release`, `gc_controller_press_once`,
  `gc_controller_flick`, `gc_controller_set_stick`, `gc_controller_set_trigger`
- `gc_controller_sequence`, `gc_controller_sequence_cancel`
- `gc_controller_get_state`, `gc_controller_reset`

**Memory (emulator)**
- `gc_memory_read`, `gc_memory_write`
- `gc_memory_read_struct`, `gc_memory_write_struct`

**Emulator control (emulator only)**
- `gc_dolphin_toggle_pause`, `gc_dolphin_step_frame`
- `gc_dolphin_save_state`, `gc_dolphin_load_state`
- `gc_dolphin_freeze`, `gc_dolphin_unfreeze`

**Dolphin configuration (emulator only)**
- `gc_dolphin_config_get`, `gc_dolphin_config_set`,
  `gc_dolphin_config_apply_profile`
- `gc_dolphin_hotkey_get`, `gc_dolphin_hotkey_set`

**Hardware & logs**
- `gc_hardware_devices`, `gc_hardware_update`
- `gc_log`

## Development

```bash
uv sync
uv run pytest
```

The end-to-end tests that launch real emulators or hardware need ROM paths.
Provide them through environment variables or a git-ignored `.e2e.env` file at
the repo root (see `tests/e2e_config.py`):

```ini
GC_MCP_TEST_ROM=/path/to/game.iso
GC_MCP_BUTTON_COUNTER_ROM=/path/to/button_counter.dol
```

## License

Released under the terms in [`LICENSE`](LICENSE) (The Machine-Generated Code
Disclaimer and Release, MGCDR 1.0).
