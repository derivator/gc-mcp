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

    def test_mem1_size_constant(self) -> None:
        assert MEM1_SIZE == 0x1800000
