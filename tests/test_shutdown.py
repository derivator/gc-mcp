"""Tests for server shutdown cleanup: stopping Dolphin subprocesses on exit."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from gc_mcp import server
from gc_mcp.instances.manager import InstanceManager


class FakeInstance:
    """Minimal stand-in for a DolphinInstance that records stop() calls."""

    def __init__(self, instance_id: str) -> None:
        self.instance_id = instance_id
        self.stopped = 0

    def stop(self, timeout: float = 5.0) -> dict:
        self.stopped += 1
        return {"instance_id": self.instance_id, "stopped": True}


def test_shutdown_instances_stops_all_instances(monkeypatch):
    """manager.shutdown() must stop every tracked instance."""
    mgr = InstanceManager(server.config)
    a, b = FakeInstance("a"), FakeInstance("b")
    mgr._instances["a"] = a  # type: ignore[attr-defined]
    mgr._instances["b"] = b  # type: ignore[attr-defined]

    monkeypatch.setattr(server, "manager", mgr)
    server._shutdown_instances()

    assert a.stopped == 1
    assert b.stopped == 1
    # Instances are cleared after shutdown.
    assert len(mgr._instances) == 0


def test_shutdown_instances_is_idempotent(monkeypatch):
    """Calling shutdown twice must not error and must not double-stop."""
    mgr = InstanceManager(server.config)
    a = FakeInstance("a")
    mgr._instances["a"] = a  # type: ignore[attr-defined]

    monkeypatch.setattr(server, "manager", mgr)
    server._shutdown_instances()
    server._shutdown_instances()

    assert a.stopped == 1


def test_shutdown_instances_survives_exceptions(monkeypatch):
    """A failing instance.stop() must not prevent other instances stopping."""
    mgr = InstanceManager(server.config)

    class Boom(FakeInstance):
        def stop(self, timeout: float = 5.0) -> dict:
            raise RuntimeError("boom")

    a = Boom("a")
    b = FakeInstance("b")
    mgr._instances["a"] = a  # type: ignore[attr-defined]
    mgr._instances["b"] = b  # type: ignore[attr-defined]

    monkeypatch.setattr(server, "manager", mgr)
    # Must not raise.
    server._shutdown_instances()
    assert b.stopped == 1


def test_server_lifespan_calls_shutdown_on_exit(monkeypatch):
    """Exiting the lifespan context manager must stop all instances."""
    import anyio

    mgr = InstanceManager(server.config)
    a = FakeInstance("a")
    mgr._instances["a"] = a  # type: ignore[attr-defined]
    monkeypatch.setattr(server, "manager", mgr)

    async def run() -> None:
        async with server._server_lifespan(server.mcp):
            # The server is "running" while the body executes.
            assert len(mgr._instances) == 1
        # After the context exits, shutdown must have stopped the instance.
        assert a.stopped == 1
        assert len(mgr._instances) == 0

    anyio.run(run)


def test_signal_handler_terminates_via_default_action(monkeypatch):
    """The SIGTERM/SIGINT handler stops instances, then re-raises the signal."""
    calls: list[int] = []

    def fake_shutdown() -> None:
        calls.append(1)

    monkeypatch.setattr(server, "_shutdown_instances", fake_shutdown)

    # Replace os.kill so the test process isn't actually killed.
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(server.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    server._signal_handler(signal.SIGTERM, None)

    assert calls == [1]
    assert killed == [(os.getpid(), signal.SIGTERM)]


def test_signal_handler_in_subprocess_stops_instances(tmp_path):
    """End-to-end: a gc-mcp process stops its instance on SIGTERM.

    Runs the server's signal handler in a child process (so we never kill the
    test runner), sends SIGTERM, and asserts the instance was stopped by the
    time the process exits.
    """
    marker = tmp_path / "stopped.txt"
    script = textwrap.dedent(
        f"""
        import os
        import signal
        import sys
        from pathlib import Path

        from gc_mcp import server

        class FakeInstance:
            def __init__(self, marker):
                self.marker = marker
            def stop(self, timeout=5.0):
                Path(self.marker).write_text("stopped")
                return {{"stopped": True}}

        mgr = server.manager
        mgr._instances["a"] = FakeInstance({str(marker)!r})

        # Register the handlers exactly as main() does.
        signal.signal(signal.SIGTERM, server._signal_handler)
        signal.signal(signal.SIGINT, server._signal_handler)

        os.kill(os.getpid(), signal.SIGTERM)
        # If the handler re-raises, this line is never reached.
        sys.exit(99)
        """
    )
    root = str(Path(__file__).resolve().parents[1])
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env={**os.environ, "PYTHONPATH": root},
        timeout=30,
    )
    # The process is terminated by SIGTERM (default action), so returncode != 0.
    assert proc.returncode != 0
    assert marker.read_text() == "stopped"
