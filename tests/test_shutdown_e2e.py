"""End-to-end shutdown verification against the REAL gc-mcp server.

Spawns the actual ``gc-mcp`` server binary over stdio, starts the ``fzero``
Dolphin instance, captures the Dolphin PID and confirms it is a child of the
spawned server, then SIGTERMs the server and asserts that the Dolphin subprocess
is gone (not orphaned).

Run manually:
    .venv/bin/python tests/test_shutdown_e2e.py
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / ".venv" / "bin" / "gc-mcp"
FZERO = "fzero"
ROM = "/home/coder/test-roms/ic3-fzero.gcm"


def _procs() -> dict[int, dict]:
    """Map pid -> {ppid, cmd} for every process."""
    out: dict[int, dict] = {}
    for pid in Path("/proc").iterdir():
        if not pid.name.isdigit():
            continue
        try:
            cmd = (pid / "cmdline").read_bytes().replace(b"\x00", b" ").decode()
            stat = (pid / "stat").read_text()
            ppid = int(stat.split()[3])
        except OSError:
            continue
        out[int(pid.name)] = {"ppid": ppid, "cmd": cmd}
    return out


def _dolphins(procs: dict[int, dict]) -> dict[int, int]:
    """dolphin pid -> its ppid."""
    return {pid: p["ppid"] for pid, p in procs.items() if "dolphin-emu" in p["cmd"]}


def _servers(procs: dict[int, dict], mypid: int) -> list[int]:
    """gc-mcp server processes that are direct children of ``mypid``."""
    return [
        pid for pid, p in procs.items()
        if "gc-mcp" in p["cmd"] and p["ppid"] == mypid
    ]


async def main() -> int:
    params = StdioServerParameters(command=str(SERVER), cwd=str(ROOT))

    before = set(_dolphins(_procs()))
    print(f"[before] dolphins: {sorted(before)}")

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Start the fzero instance.
            await session.call_tool(
                "gc_instance_start", {"instance_id": FZERO, "rom_path": ROM}
            )
            print("[start] instance_start returned")

            # Wait for the dolphin to appear as a child of the spawned server.
            server_pids: list[int] = []
            dolphin = None
            for _ in range(30):
                procs = _procs()
                server_pids = _servers(procs, os.getpid())
                d = _dolphins(procs)
                new = {p: pp for p, pp in d.items() if p not in before}
                for dp, dpp in new.items():
                    if dpp in server_pids:
                        dolphin = dp
                        break
                if dolphin and server_pids:
                    break
                await asyncio.sleep(1)

            print(f"[start] server pids (child of me): {server_pids}")
            print(f"[start] dolphin pid: {dolphin}")
            if not dolphin or not server_pids:
                print("FAIL: could not confirm dolphin under the spawned server")
                return 1

            server_pid = server_pids[0]
            print(f"[sig] SIGTERM to server {server_pid}")
            os.kill(server_pid, signal.SIGTERM)

            # Poll for dolphin death (up to 12s).
            for _ in range(24):
                await asyncio.sleep(0.5)
                if dolphin not in _dolphins(_procs()):
                    print("PASS: server shutdown stopped its dolphin subprocess")
                    return 0

            print(f"FAIL: dolphin {dolphin} still alive after 12s")
            return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
