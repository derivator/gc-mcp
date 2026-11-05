"""Per-PID GameCube memory access via ``process_vm_readv``/``process_vm_writev``.

``py-dolphin-memory-engine`` attaches to the *first* matching Dolphin process,
which breaks when several instances run at once. This backend replicates the
same underlying mechanism but targets a specific PID, so each instance can have
its own memory backend.

Dolphin places its emulated RAM in a shared-memory mapping under
``/dev/shm/dolphinmem*`` (or ``/dev/shm/dolphin-emu*``). We find that mapping in
``/proc/<pid>/maps`` to obtain the host address of MEM1 (and ARAM / MEM2 when
present), then convert emulated addresses to host addresses and read/write with
the ``process_vm_*`` syscalls.
"""

from __future__ import annotations

import ctypes
import errno
import os
import threading
from pathlib import Path

from ..errors import MemoryError
from .base import MemoryBackend

# ---------------------------------------------------------------------------
# Dolphin emulated address-space constants (GameCube).
# ---------------------------------------------------------------------------
MEM1_START = 0x80000000
MEM1_SIZE = 0x1800000  # 24 MB
MEM1_END = MEM1_START + MEM1_SIZE

MEM2_START = 0x90000000  # Wii-only, kept for completeness
MEM2_SIZE = 0x4000000  # 64 MB
MEM2_END = MEM2_START + MEM2_SIZE

ARAM_START = 0x7E000000
ARAM_END = 0x7F000000
ARAM_SIZE = 0x1000000  # 16 MB real
ARAM_FAKESIZE = 0x2000000  # 32 MB "fake" vMem used for the speedhack region

#: File-path prefixes Dolphin uses for its shared memory mappings.
_DOLPHIN_SHM_PREFIXES = ("/dev/shm/dolphinmem", "/dev/shm/dolphin-emu")

# The shared memory layout offset where ARAM / MEM2 begin (after a 0x40000 gap).
_ARAM_OFFSET = MEM1_SIZE + 0x40000


class _IOVec(ctypes.Structure):
    _fields_ = [("iov_base", ctypes.c_void_p), ("iov_len", ctypes.c_size_t)]


_libc = ctypes.CDLL("libc.so.6", use_errno=True)
_libc.process_vm_readv.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(_IOVec),
    ctypes.c_ulong,
    ctypes.POINTER(_IOVec),
    ctypes.c_ulong,
    ctypes.c_ulong,
]
_libc.process_vm_readv.restype = ctypes.c_ssize_t
_libc.process_vm_writev.argtypes = [
    ctypes.c_int,
    ctypes.POINTER(_IOVec),
    ctypes.c_ulong,
    ctypes.POINTER(_IOVec),
    ctypes.c_ulong,
    ctypes.c_ulong,
]
_libc.process_vm_writev.restype = ctypes.c_ssize_t


def _parse_maps(pid: int) -> dict:
    """Parse /proc/<pid>/maps for Dolphin's shared-memory regions.

    Returns a dict with the host start addresses of MEM1, ARAM and MEM2 (0 if
    absent) plus whether ARAM is accessible.
    """
    maps_path = Path(f"/proc/{pid}/maps")
    if not maps_path.exists():
        raise MemoryError(f"process {pid} is not running")

    result = {
        "mem1": 0,
        "aram": 0,
        "mem2": 0,
        "mem2_present": False,
        "aram_accessible": False,
    }

    try:
        lines = maps_path.read_text().splitlines()
    except OSError as exc:
        raise MemoryError(f"could not read {maps_path}: {exc}") from exc

    for line in lines:
        tokens = line.split()
        if len(tokens) < 3:
            continue
        if not any(tok.startswith(_DOLPHIN_SHM_PREFIXES) for tok in tokens):
            continue

        start_end = tokens[0]
        try:
            offset = int(tokens[2], 16)
            start = int(start_end.split("-")[0], 16)
            end = int(start_end.split("-")[1], 16)
        except (ValueError, IndexError):
            continue

        region_size = end - start

        # MEM1: at offset 0, sized at least the 24 MB main RAM.
        if offset == 0 and region_size >= MEM1_SIZE:
            result["mem1"] = start
        # ARAM / MEM2: at the offset after the 0x40000 gap.
        elif offset == _ARAM_OFFSET:
            if region_size >= MEM2_SIZE:
                result["mem2"] = start
                result["mem2_present"] = True
            elif region_size >= MEM1_SIZE:
                result["aram"] = start
                result["aram_accessible"] = True

    return result


class ProcessVMMemory(MemoryBackend):
    """Read/write a specific Dolphin process's emulated RAM by PID."""

    def __init__(self, pid: int) -> None:
        self.pid = int(pid)
        self._lock = threading.Lock()
        self._regions: dict | None = None

    # ------------------------------------------------------------------
    def _ensure_regions(self) -> dict:
        if self._regions is None:
            self._regions = _parse_maps(self.pid)
            if self._regions["mem1"] == 0:
                # The process is running but the emulator hasn't allocated RAM
                # (game not started). Cache it as not-ready; allow retry later.
                self._regions = None
                raise MemoryError(
                    f"emulated RAM not yet available for process {self.pid} "
                    "(is the emulator running a game?)"
                )
        return self._regions

    def is_ready(self) -> bool:
        try:
            self._ensure_regions()
            return True
        except MemoryError:
            return False

    # ------------------------------------------------------------------
    # Address conversion
    # ------------------------------------------------------------------
    @staticmethod
    def dolphin_addr_to_offset(addr: int, consider_aram: bool = True) -> int:
        if ARAM_START <= addr < ARAM_END:
            return addr - ARAM_START
        if MEM1_START <= addr < MEM1_END:
            offset = addr - MEM1_START
            if consider_aram:
                offset += ARAM_FAKESIZE
            return offset
        if MEM2_START <= addr < MEM2_END:
            return addr - MEM2_START + (MEM2_START - MEM1_START)
        raise MemoryError(f"address 0x{addr:X} is outside the emulated RAM range")

    def _offset_to_host(self, offset: int) -> int:
        regions = self._ensure_regions()
        if regions["aram_accessible"]:
            if offset >= ARAM_FAKESIZE:
                return regions["mem1"] + offset - ARAM_FAKESIZE
            return regions["aram"] + offset
        if offset >= (MEM2_START - MEM1_START):
            if not regions["mem2_present"]:
                raise MemoryError("this GameCube has no MEM2 (Wii-only region)")
            return regions["mem2"] + offset - (MEM2_START - MEM1_START)
        return regions["mem1"] + offset

    # ------------------------------------------------------------------
    # Read / write
    # ------------------------------------------------------------------
    def read(self, address: int, length: int) -> bytes:
        if length <= 0:
            return b""
        offset = self.dolphin_addr_to_offset(address)
        host = self._offset_to_host(offset)
        buf = ctypes.create_string_buffer(length)
        local = _IOVec(ctypes.cast(buf, ctypes.c_void_p), length)
        remote = _IOVec(host, length)

        with self._lock:
            nread = _libc.process_vm_readv(
                self.pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0
            )

        if nread < 0:
            err = ctypes.get_errno()
            raise MemoryError(
                f"process_vm_readv failed (errno {err}): {os.strerror(err)}",
                details={"pid": self.pid, "address": hex(address), "length": length},
            )
        if nread != length:
            raise MemoryError(
                f"short read: expected {length} bytes, got {nread}",
                details={"pid": self.pid, "address": hex(address)},
            )
        return buf.raw[:length]

    def write(self, address: int, data: bytes) -> None:
        length = len(data)
        if length == 0:
            return
        offset = self.dolphin_addr_to_offset(address)
        host = self._offset_to_host(offset)

        buf = ctypes.create_string_buffer(data, length)
        local = _IOVec(ctypes.cast(buf, ctypes.c_void_p), length)
        remote = _IOVec(host, length)

        with self._lock:
            nwritten = _libc.process_vm_writev(
                self.pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0
            )

        if nwritten < 0:
            err = ctypes.get_errno()
            raise MemoryError(
                f"process_vm_writev failed (errno {err}): {os.strerror(err)}",
                details={"pid": self.pid, "address": hex(address), "length": length},
            )
        if nwritten != length:
            raise MemoryError(
                f"short write: expected {length} bytes, wrote {nwritten}",
                details={"pid": self.pid, "address": hex(address)},
            )
