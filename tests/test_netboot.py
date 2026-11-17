"""Tests for the Swiss netboot client (TCP 4242)."""

from __future__ import annotations

import pytest

import gc_mcp.netboot as nb
from gc_mcp.errors import NetbootError


class FakeSocket:
    """Stand-in for a connected socket context manager."""

    def __init__(self, reply: bytes = b"") -> None:
        self._reply = reply
        self.sent = b""
        self.timeout = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, timeout) -> None:
        self.timeout = timeout

    def sendall(self, data) -> None:
        self.sent += data

    def recv(self, _n) -> bytes:
        return self._reply


def _patch_socket(monkeypatch, reply: bytes = b"PONG\n"):
    sock = FakeSocket(reply)
    monkeypatch.setattr(nb.socket, "create_connection", lambda *a, **k: sock)
    return sock


# ---------------------------------------------------------------------------
# validate_rom_path
# ---------------------------------------------------------------------------
class TestValidateRomPath:
    def test_accepts_sdc(self):
        assert nb.validate_rom_path("sdc:/games/Foo.iso") == "sdc:/games/Foo.iso"

    def test_accepts_fsp(self):
        assert nb.validate_rom_path("fsp:/games/Foo.iso") == "fsp:/games/Foo.iso"

    def test_strips_whitespace(self):
        assert nb.validate_rom_path("  sdc:/games/Foo.iso\n") == "sdc:/games/Foo.iso"

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            nb.validate_rom_path("")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            nb.validate_rom_path(123)

    def test_rejects_bad_prefix(self):
        with pytest.raises(ValueError):
            nb.validate_rom_path("/host/path/Foo.iso")

    def test_rejects_dotdot(self):
        with pytest.raises(ValueError):
            nb.validate_rom_path("sdc:/games/../secret.iso")


# ---------------------------------------------------------------------------
# NetbootClient
# ---------------------------------------------------------------------------
class TestNetbootClient:
    def test_ping_sends_line_and_returns_reply(self, monkeypatch):
        sock = _patch_socket(monkeypatch, b"PONG\n")
        client = nb.NetbootClient("192.0.2.10")
        assert client.ping() == "PONG"
        assert sock.sent == b"PING\n"

    def test_status(self, monkeypatch):
        sock = _patch_socket(monkeypatch, b"READY\n")
        client = nb.NetbootClient("192.0.2.10")
        assert client.status() == "READY"
        assert sock.sent == b"STATUS\n"

    def test_boot(self, monkeypatch):
        sock = _patch_socket(monkeypatch, b"OK\n")
        client = nb.NetbootClient("192.0.2.10")
        assert client.boot("sdc:/games/Foo.iso") == "OK"
        assert sock.sent == b"BOOT sdc:/games/Foo.iso\n"

    def test_boot_invalid_path_raises_before_sending(self, monkeypatch):
        sock = _patch_socket(monkeypatch, b"OK\n")
        client = nb.NetbootClient("192.0.2.10")
        with pytest.raises(ValueError):
            client.boot("/host/path.iso")
        assert sock.sent == b""

    def test_reachable_true(self, monkeypatch):
        _patch_socket(monkeypatch, b"PONG\n")
        assert nb.NetbootClient("192.0.2.10").reachable() is True

    def test_reachable_false_on_connection_error(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(nb.socket, "create_connection", boom)
        assert nb.NetbootClient("192.0.2.10").reachable() is False

    def test_connection_failure_raises_netboot_error(self, monkeypatch):
        def boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(nb.socket, "create_connection", boom)
        with pytest.raises(NetbootError):
            nb.NetbootClient("192.0.2.10").ping()

    def test_no_host_raises_netboot_error(self):
        with pytest.raises(NetbootError):
            nb.NetbootClient(None).ping()
