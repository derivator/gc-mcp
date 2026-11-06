"""End-to-end multi-instance isolation test against the REAL gc-mcp server.

Spawns the actual ``gc-mcp`` server binary over stdio, starts TWO instances of
the button-counter ROM (each with its own Xvfb display and Pipes controller),
and verifies that controller input and Dolphin hotkeys (pause, save/load) do
NOT interfere across instances.

The button-counter ROM prints ``Controller N: <button> pressed (total N)`` to
each instance's ``dolphin.log`` via ``SYS_Report``, so we can read each
instance's press counter deterministically from its own log file -- no
screenshots required.

What we verify:
  1. Each instance gets its own X display number.
  2. Controller input is isolated: pressing A on instance A only bumps A.
  3. Controller input is isolated in the other direction: pressing A on B only
     bumps B.
  4. Hotkey (pause) is isolated: pausing A stops A from processing input while
     B keeps running and processing input.
  5. Hotkey (save/load) is isolated: saving/loading state on A only reverts A's
     counter, leaving B untouched.

Run manually:
    .venv/bin/python tests/test_button_counter_multi_e2e.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / ".venv" / "bin" / "gc-mcp"
ROM = "/home/coder/gc-button-counter/gc-button-counter-gamecube.dol"

#: A press line: ``Controller 1: A pressed (total N)``. Capture the running total.
_PRESS_RE = re.compile(r"Controller \d+: .+ pressed \(total (\d+)\)")
#: The line printed once ``main()`` reaches its render loop.
_STARTED_RE = re.compile(r"GC Button Counter started")

#: How long to poll for a counter to reach an expected value before giving up.
_COUNTER_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Log / counter helpers
# ---------------------------------------------------------------------------
def _read_log(log_path: Path) -> tuple[int | None, int | None, bool]:
    """Return (last_total, max_total, started) parsed from ``dolphin.log``.

    ``last_total`` is the running total from the most recent press line;
    ``max_total`` is the largest total ever logged. After a save-state load the
    counter can *decrease*, so ``last_total < max_total`` indicates a reversion.
    ``started`` is True once ``main()`` reached its render loop.
    """
    if not log_path.exists():
        return (None, None, False)
    last: int | None = None
    max_total: int | None = None
    started = False
    for line in log_path.read_text(errors="replace").splitlines():
        if _STARTED_RE.search(line):
            started = True
        m = _PRESS_RE.search(line)
        if m:
            total = int(m.group(1))
            last = total
            max_total = total if max_total is None else max(max_total, total)
    return (last, max_total, started)


def _counter(log_path: Path) -> int:
    """The current counter (most recent press total), or 0 if none logged yet."""
    last, _, _ = _read_log(log_path)
    return last if last is not None else 0


def _max_counter(log_path: Path) -> int:
    """The largest press total ever logged for this instance."""
    _, max_total, _ = _read_log(log_path)
    return max_total if max_total is not None else 0


def _started(log_path: Path) -> bool:
    return _read_log(log_path)[2]


# ---------------------------------------------------------------------------
# MCP call helpers
# ---------------------------------------------------------------------------
async def _call(session: ClientSession, name: str, args: dict):
    """Call an MCP tool and return its parsed JSON payload."""
    result = await session.call_tool(name, args)
    if getattr(result, "isError", False):
        texts = [c.text for c in result.content if hasattr(c, "text")]
        raise RuntimeError(f"tool {name} error: {texts}")
    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}


async def _wait_started(log_path: Path, timeout: float = 30.0) -> bool:
    """Wait until the button counter reaches its render loop."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if _started(log_path):
            return True
        await asyncio.sleep(0.25)
    return False


async def _wait_counter(log_path: Path, expected: int, timeout: float = _COUNTER_TIMEOUT) -> bool:
    """Wait until the instance's current counter equals ``expected``."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if _counter(log_path) == expected:
            return True
        await asyncio.sleep(0.25)
    return False


async def _press(session: ClientSession, instance_id: str, log_path: Path,
                 target: int) -> bool:
    """Tap A on ``instance_id`` and wait until its counter reaches ``target``.

    After the counter reaches the target we sleep briefly so the ROM has time to
    process the button *release* (the press → hold → release cycle ends ~200 ms
    after the press is logged, plus one frame to update ``keysHeldOld``). Without
    this grace period a second, immediately-issued press is sent while the ROM
    still sees the button as held and so misses the 0 → 1 press edge.
    """
    await _call(session, "gc_controller_press_once", {
        "instance_id": instance_id, "port": 0, "button": "A",
    })
    ok = await _wait_counter(log_path, target)
    # Let the ROM process the release so the next press is a fresh edge.
    await asyncio.sleep(0.7)
    return ok


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------------------
# Main test flow
# ---------------------------------------------------------------------------
async def main() -> int:
    params = StdioServerParameters(command=str(SERVER), cwd=str(ROOT))
    all_ok = True

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            started: list[str] = []
            try:
                # --- Start two button-counter instances ---------------------
                for iid in ("btn-a", "btn-b"):
                    st = await _call(session, "gc_instance_start", {
                        "instance_id": iid,
                        "rom_path": ROM,
                        "controller_ports": [0],
                        "capture_backend": "xvfb",
                        "gpu_accel": "vgl",
                    })
                    if not st.get("running", False):
                        raise RuntimeError(f"{iid} did not start: {st}")
                    started.append(iid)
                    print(f"[start] {iid} pid={st.get('pid')} display={st.get('display')}")

                sa = await _call(session, "gc_instance_status", {"instance_id": "btn-a"})
                sb = await _call(session, "gc_instance_status", {"instance_id": "btn-b"})
                la = Path(sa["log_path"])
                lb = Path(sb["log_path"])

                # Check 1: distinct displays.
                da, db = sa.get("display"), sb.get("display")
                all_ok &= _check(
                    "distinct X displays per instance",
                    da is not None and db is not None and da != db,
                    f"btn-a={da!r} btn-b={db!r}",
                )

                # Wait for both ROMs to reach their render loop.
                ok_a = await _wait_started(la)
                ok_b = await _wait_started(lb)
                all_ok &= _check("both instances reached render loop", ok_a and ok_b)

                # Baseline: no presses yet.
                all_ok &= _check(
                    "initial counters are 0",
                    _counter(la) == 0 and _counter(lb) == 0,
                    f"btn-a={_counter(la)} btn-b={_counter(lb)}",
                )

                # Check 2: press A on btn-a -> only btn-a bumps.
                ok = await _press(session, "btn-a", la, 1)
                all_ok &= _check(
                    "press A on A bumps only A",
                    ok and _counter(lb) == 0,
                    f"btn-a={_counter(la)} btn-b={_counter(lb)}",
                )

                # Check 3: press A on btn-b -> only btn-b bumps.
                ok = await _press(session, "btn-b", lb, 1)
                all_ok &= _check(
                    "press A on B bumps only B",
                    ok and _counter(la) == 1,
                    f"btn-a={_counter(la)} btn-b={_counter(lb)}",
                )

                # Check 4: pause A (F10) must NOT pause B.
                await _call(session, "gc_emulator_toggle_pause", {"instance_id": "btn-a"})
                await asyncio.sleep(0.6)
                # While A is paused, pressing A on A should NOT register.
                await _call(session, "gc_controller_press_once", {
                    "instance_id": "btn-a", "port": 0, "button": "A",
                })
                await asyncio.sleep(0.8)
                paused_ok = _counter(la) == 1  # still 1, A is paused
                # While A is paused, B keeps running and processes input.
                ok_b = await _press(session, "btn-b", lb, 2)
                all_ok &= _check(
                    "pause A stops A but not B",
                    paused_ok and ok_b,
                    f"btn-a={_counter(la)} (expect 1) btn-b={_counter(lb)} (expect 2)",
                )

                # Resume A; it should process input again.
                await _call(session, "gc_emulator_toggle_pause", {"instance_id": "btn-a"})
                await asyncio.sleep(0.6)
                ok_a = await _press(session, "btn-a", la, 2)
                all_ok &= _check(
                    "resume A lets A process input again",
                    ok_a and _counter(lb) == 2,
                    f"btn-a={_counter(la)} (expect 2) btn-b={_counter(lb)} (expect 2)",
                )

                # Check 5: save/load state on A is isolated from B.
                await _call(session, "gc_emulator_save_state", {
                    "instance_id": "btn-a", "slot": 1,
                })
                await asyncio.sleep(0.8)
                # Bump A from 2 -> 4.
                ok1 = await _press(session, "btn-a", la, 3)
                ok2 = await _press(session, "btn-a", la, 4)
                all_ok &= _check(
                    "A counter advanced to 4 before load",
                    ok1 and ok2 and _counter(lb) == 2,
                    f"btn-a={_counter(la)} (expect 4) btn-b={_counter(lb)} (expect 2)",
                )
                # Load slot 1 (A reverts to 2), then one more press -> 3.
                await _call(session, "gc_emulator_load_state", {
                    "instance_id": "btn-a", "slot": 1,
                })
                await asyncio.sleep(0.8)
                ok_load = await _press(session, "btn-a", la, 3)
                reverted = _max_counter(la) == 4 and _counter(la) == 3
                all_ok &= _check(
                    "load on A reverts only A (4 -> 2 -> press 3)",
                    ok_load and reverted and _counter(lb) == 2,
                    f"btn-a={_counter(la)} (expect 3, max 4) btn-b={_counter(lb)} (expect 2)",
                )

                print()
                print(f"FINAL: btn-a counter={_counter(la)} btn-b counter={_counter(lb)}")
            finally:
                # Clean up: stop and remove both instances.
                for iid in started:
                    try:
                        await _call(session, "gc_instance_remove", {"instance_id": iid})
                        print(f"[cleanup] removed {iid}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"[cleanup] could not remove {iid}: {exc}")

    print()
    print("RESULT:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
