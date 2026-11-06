"""Tests for the per-PID memory backend's address mapping."""

from __future__ import annotations

import pytest

from gc_mcp.errors import MemoryError
from gc_mcp.memory.process_vm import (
    ARAM_FAKESIZE,
    ARAM_START,
    MEM1_SIZE,
    MEM1_START,
    MEM2_START,
    ProcessVMMemory,
    _parse_maps_lines,
)


class TestDolphinAddrToOffset:
    def test_mem1_start(self) -> None:
        # MEM1 start maps to offset ARAM_FAKESIZE (when considering ARAM).
        assert ProcessVMMemory.dolphin_addr_to_offset(MEM1_START) == ARAM_FAKESIZE

    def test_mem1_region(self) -> None:
        addr = MEM1_START + 0x1000
        assert ProcessVMMemory.dolphin_addr_to_offset(addr) == ARAM_FAKESIZE + 0x1000

    def test_mem1_without_aram(self) -> None:
        addr = MEM1_START + 0x200
        assert ProcessVMMemory.dolphin_addr_to_offset(addr, consider_aram=False) == 0x200

    def test_aram_start(self) -> None:
        assert ProcessVMMemory.dolphin_addr_to_offset(ARAM_START) == 0

    def test_mem2_start(self) -> None:
        # MEM2 start maps to offset (MEM2_START - MEM1_START).
        assert ProcessVMMemory.dolphin_addr_to_offset(MEM2_START) == (MEM2_START - MEM1_START)

    def test_out_of_range(self) -> None:
        for bad in (0x00000000, 0x81800000, 0x7D000000, 0x94000000):
            with pytest.raises(MemoryError):
                ProcessVMMemory.dolphin_addr_to_offset(bad)


class TestOffsetToHost:
    def _make(self, *, mem1=0x700000000000, aram=0x710000000000, mem2=0x720000000000,
              mem2_present=False, aram_accessible=True) -> ProcessVMMemory:
        inst = ProcessVMMemory(pid=1234)
        inst._regions = {
            "mem1": mem1,
            "aram": aram,
            "mem2": mem2,
            "mem2_present": mem2_present,
            "aram_accessible": aram_accessible,
        }
        return inst

    def test_mem1_maps_into_mem1(self) -> None:
        m = self._make()
        offset = ProcessVMMemory.dolphin_addr_to_offset(MEM1_START + 0x1000)
        assert m._offset_to_host(offset) == 0x700000000000 + 0x1000

    def test_aram_maps_into_aram(self) -> None:
        m = self._make()
        offset = ProcessVMMemory.dolphin_addr_to_offset(ARAM_START + 0x800)
        assert m._offset_to_host(offset) == 0x710000000000 + 0x800

    def test_mem2_maps_into_mem2(self) -> None:
        m = self._make(mem2_present=True, aram_accessible=False)
        offset = ProcessVMMemory.dolphin_addr_to_offset(MEM2_START + 0x40)
        assert m._offset_to_host(offset) == 0x720000000000 + 0x40

    def test_mem2_missing_raises(self) -> None:
        m = self._make(mem2_present=False, aram_accessible=False)
        offset = ProcessVMMemory.dolphin_addr_to_offset(MEM2_START)
        with pytest.raises(MemoryError):
            m._offset_to_host(offset)

    def test_mem1_maps_into_mem1_without_aram(self) -> None:
        # Regression: when ARAM is not separately mapped, a MEM1 offset must
        # still land in MEM1. The old fallback re-added ARAM_FAKESIZE and read
        # 32 MB past the base (returning zeros).
        m = self._make(aram_accessible=False)
        offset = ProcessVMMemory.dolphin_addr_to_offset(MEM1_START + 0x1000)
        assert m._offset_to_host(offset) == 0x700000000000 + 0x1000

    def test_aram_missing_raises(self) -> None:
        # Without a detectable ARAM region there is no host mapping for ARAM.
        m = self._make(aram_accessible=False)
        offset = ProcessVMMemory.dolphin_addr_to_offset(ARAM_START)
        with pytest.raises(MemoryError):
            m._offset_to_host(offset)

    def test_mem1_size_constant(self) -> None:
        assert MEM1_SIZE == 0x1800000


class TestParseMapsLines:
    def _line(self, start, end, offset, name="dolphin-emu.1234") -> str:
        return (
            f"{start:x}-{end:x} rw-s {offset:08x} 00:98 19 "
            f"/dev/shm/{name} (deleted)"
        )

    def test_mem1_and_aram_gamecube(self) -> None:
        # A GameCube-style layout: MEM1 at offset 0, ARAM at an offset with bit
        # 0x40000 set (the real Dolphin offset is 0x4040000).
        lines = [
            self._line(0x7f00000000, 0x7f04000000, 0x00000000),
            self._line(0x7f10000000, 0x7f12000000, 0x04040000),
        ]
        regions = _parse_maps_lines(lines)
        assert regions["mem1"] == 0x7F00000000
        assert regions["aram_accessible"] is True
        assert regions["aram"] == 0x7F10000000
        assert regions["mem2_present"] is False

    def test_mem2_wii(self) -> None:
        # A Wii-style layout: MEM2 is a 64 MB region at an offset with bit
        # 0x40000 set.
        lines = [
            self._line(0x7f00000000, 0x7f04000000, 0x00000000),
            self._line(0x7f10000000, 0x7f14000000, 0x04040000),
        ]
        regions = _parse_maps_lines(lines)
        assert regions["mem1"] == 0x7F00000000
        assert regions["mem2_present"] is True
        assert regions["mem2"] == 0x7F10000000
        assert regions["aram_accessible"] is False

    def test_aram_not_detected_without_flag(self) -> None:
        # A region whose offset lacks the 0x40000 bit is not ARAM/MEM2.
        lines = [
            self._line(0x7f00000000, 0x7f04000000, 0x00000000),
            self._line(0x7f10000000, 0x7f12000000, 0x04000000),
        ]
        regions = _parse_maps_lines(lines)
        assert regions["mem1"] == 0x7F00000000
        assert regions["aram_accessible"] is False
        assert regions["mem2_present"] is False

    def test_ignores_non_dolphin_lines(self) -> None:
        lines = ["/usr/bin/dolphin-emu r-xp 00000000 00:56 3829039"]
        regions = _parse_maps_lines(lines)
        assert regions["mem1"] == 0
        assert regions["aram_accessible"] is False
