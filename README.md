# gc-mcp

An MCP (Model Context Protocol) server that drives **multiple, stock, unmodified
Dolphin emulator instances** as if each were a real Nintendo GameCube. It launches
ROM/DOL/ELF instances with custom per-instance configuration (Broadband Adapter
and other peripherals), captures rendered output inside a Docker container, and
manipulates all four GameCube controller ports.

The tool surface is designed around a **unified `gc_` interface** that works on
both the emulator and (eventually) real hardware. Anything that can only ever
apply to the emulator (e.g. changing Dolphin's own configuration) is exposed
under a `dolphin_` prefix.

## Why

The model that drives an agent is often too slow to hold a button press/release
pair together, so controller tools support **momentary actions and timed input
sequences**. Memory read/write is provided so the agent can inspect or patch
game state.

## Concepts

- **Platform** — an abstraction over a target: `dolphin` (an emulator instance)
  or `gamecube-hw` (real hardware, currently a stub that returns "unsupported").
- **Instance** — one running Dolphin process, isolated by its own `--user` data
  directory, with its own controllers, capture backend and memory backend.
- **Controller port** — one of the four GameCube controller ports (`0`–`3`).
- **Capture backend** — how rendered frames are grabbed:
  - `xvfb` (default): Dolphin runs under a virtual X server (Xvfb) using OpenGL
    (hardware GPU if `/dev/dri` is present, else Mesa `llvmpipe`); frames are
    captured with `ffmpeg`/`xwd` into PNG.
  - `framedump`: Dolphin's built-in frame dump (`Movie.DumpFrames`) to PNG, as a
    fallback that needs no display server.

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
| `GC_MCP_MEMORY_BACKEND` | `process_vm` | `process_vm` or `dolphin_engine` |

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
- `gc_controller_press`, `gc_controller_release`, `gc_controller_set_stick`, `gc_controller_set_trigger`, `gc_controller_sequence`, `gc_controller_sequence_cancel`, `gc_controller_get_state`, `gc_controller_reset`
- `gc_memory_read`, `gc_memory_write`
- `dolphin_config_get`, `dolphin_config_set`, `dolphin_config_apply_profile`
- `gc_server_info`
