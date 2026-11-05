"""Memory backend abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MemoryBackend(ABC):
    """Abstract GameCube memory read/write backend.

    Addresses are Dolphin "emulated" addresses (e.g. ``0x80000000`` for the
    start of MEM1 / main RAM).
    """

    @abstractmethod
    def is_ready(self) -> bool:
        """Whether the backend can currently perform read/write operations."""
        ...

    @abstractmethod
    def read(self, address: int, length: int) -> bytes:
        ...

    @abstractmethod
    def write(self, address: int, data: bytes) -> None:
        ...

    def read_u16(self, address: int) -> int:
        return int.from_bytes(self.read(address, 2), "big")

    def read_u32(self, address: int) -> int:
        return int.from_bytes(self.read(address, 4), "big")

    def read_u64(self, address: int) -> int:
        return int.from_bytes(self.read(address, 8), "big")

    def write_u16(self, address: int, value: int) -> None:
        self.write(address, int(value).to_bytes(2, "big"))

    def write_u32(self, address: int, value: int) -> None:
        self.write(address, int(value).to_bytes(4, "big"))

    def write_u64(self, address: int, value: int) -> None:
        self.write(address, int(value).to_bytes(8, "big"))

    def close(self) -> None:
        """Release resources. No-op by default."""
