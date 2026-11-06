"""Tests for the struct-format memory read/write tools."""

from __future__ import annotations

import struct

import pytest

import gc_mcp.server as server


class FakeMemory:
    """A tiny in-memory stand-in for a platform's memory backend."""

    def __init__(self, size: int = 4096) -> None:
        self.buf = bytearray(size)
        self.writes: list[tuple[int, bytes]] = []

    def memory_read(self, address: int, length: int) -> bytes:
        if address + length > len(self.buf):
            raise ValueError("read out of range")
        return bytes(self.buf[address : address + length])

    def memory_write(self, address: int, data: bytes) -> None:
        if address + len(data) > len(self.buf):
            raise ValueError("write out of range")
        self.buf[address : address + len(data)] = data
        self.writes.append((address, data))


@pytest.fixture
def fake(monkeypatch):
    mem = FakeMemory()
    monkeypatch.setattr(server, "_instance", lambda instance_id: mem)
    return mem


class TestReadStruct:
    def test_f32_matrix(self, fake):
        floats = [1.0, 2.0, -3.5, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0]
        fake.memory_write(0x100, struct.pack(">12f", *floats))
        res = server.gc_memory_read_struct("test", 0x100, ">12f")
        assert res["size"] == 48
        assert res["values"] == floats

    def test_two_u32(self, fake):
        fake.memory_write(0x200, struct.pack(">II", 0xDEADBEEF, 0x12345678))
        res = server.gc_memory_read_struct("test", 0x200, ">II")
        assert res["values"] == [0xDEADBEEF, 0x12345678]

    def test_four_s16(self, fake):
        fake.memory_write(0x300, struct.pack(">4h", -1, 0, 1, 32767))
        res = server.gc_memory_read_struct("test", 0x300, ">4h")
        assert res["values"] == [-1, 0, 1, 32767]

    def test_byte_order_prefix_required_for_gamecube(self, fake):
        # Native-order formats may not match GameCube big-endian; document that
        # callers should prefix with '>'. Just verify a big-endian read works.
        fake.memory_write(0x400, struct.pack(">H", 0x0102))
        res = server.gc_memory_read_struct("test", 0x400, ">H")
        assert res["values"] == [0x0102]

    def test_invalid_format(self, fake):
        res = server.gc_memory_read_struct("test", 0x100, "not-a-format")
        assert "error" in res
        assert "invalid struct format" in res["error"]


class TestWriteStruct:
    def test_write_f32(self, fake):
        floats = [1.5, -2.0, 3.25]
        res = server.gc_memory_write_struct("test", 0x100, ">3f", floats)
        assert res["written"] == 12
        assert res["size"] == 12
        assert fake.memory_read(0x100, 12) == struct.pack(">3f", *floats)

    def test_write_two_u32(self, fake):
        res = server.gc_memory_write_struct("test", 0x200, ">II", [0x11111111, 0x22222222])
        assert res["written"] == 8
        assert fake.memory_read(0x200, 8) == struct.pack(">II", 0x11111111, 0x22222222)

    def test_roundtrip(self, fake):
        # Write a 3x4 matrix, then read it back as floats.
        floats = [1.0, 0.0, 0.0, 10.0, 0.0, 1.0, 0.0, 20.0, 0.0, 0.0, 1.0, 30.0]
        server.gc_memory_write_struct("test", 0x500, ">12f", floats)
        res = server.gc_memory_read_struct("test", 0x500, ">12f")
        assert res["values"] == floats

    def test_wrong_value_count(self, fake):
        res = server.gc_memory_write_struct("test", 0x100, ">3f", [1.0, 2.0])
        assert "error" in res

    def test_invalid_format(self, fake):
        res = server.gc_memory_write_struct("test", 0x100, "??", [1.0])
        assert "error" in res


class TestPointerDereferencePattern:
    def test_read_pointer_then_struct(self, fake):
        # Common pattern: read a u32 pointer at a static address, then read a
        # struct at the pointee.
        target = 0x400
        server.gc_memory_write_struct("test", 0x800, ">I", [target])
        server.gc_memory_write_struct("test", target, ">3f", [1.0, 2.0, 3.0])

        ptr = server.gc_memory_read_struct("test", 0x800, ">I")["values"][0]
        res = server.gc_memory_read_struct("test", ptr, ">3f")
        assert res["values"] == [1.0, 2.0, 3.0]
