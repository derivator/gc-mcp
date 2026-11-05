"""Memory backends."""

from .base import MemoryBackend
from .process_vm import ProcessVMMemory

__all__ = ["MemoryBackend", "ProcessVMMemory"]


def create_memory_backend(name: str, pid: int | None = None) -> MemoryBackend:
    """Factory for memory backends.

    ``process_vm`` targets a specific PID (the default, multi-instance safe).
    ``dolphin_engine`` wraps the optional library (single-instance only).
    """
    if name == "dolphin_engine":
        from .dolphin_engine import DolphinEngineMemory

        return DolphinEngineMemory()
    if pid is None:
        raise ValueError("process_vm memory backend requires a PID")
    return ProcessVMMemory(pid)
