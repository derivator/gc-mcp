"""Optional ``py-dolphin-memory-engine`` backend.

This wraps the ``dolphin_memory_engine`` library, which is a Cython extension.
It provides a simple, well-tested read/write API.

IMPORTANT LIMITATION: ``dolphin_memory_engine`` hooks the *first* Dolphin
process it finds (matching on the process name), so it cannot distinguish
between multiple instances. For multi-instance use, prefer
:class:`gc_mcp.memory.process_vm.ProcessVMMemory`, which targets a specific PID
and is the default backend. This backend is only used when explicitly configured
(``GC_MCP_MEMORY_BACKEND=dolphin_engine``) and there is a single instance.
"""

from __future__ import annotations

from ..errors import MemoryError
from .base import MemoryBackend

try:
    import dolphin_memory_engine as _dme  # type: ignore
except ImportError:  # pragma: no cover - depends on optional extras
    _dme = None


class DolphinEngineMemory(MemoryBackend):
    """Adapter around the ``dolphin_memory_engine`` library (single-instance)."""

    def __init__(self) -> None:
        if _dme is None:
            raise MemoryError(
                "dolphin-memory-engine is not installed; run "
                "`uv sync --extra memory` or `pip install dolphin-memory-engine`"
            )

    def is_ready(self) -> bool:
        if _dme is None:
            return False
        if not _dme.is_hooked():
            try:
                _dme.hook()
            except Exception:  # pragma: no cover
                return False
        return _dme.is_hooked()

    def _ensure_hooked(self) -> None:
        if not self.is_ready():
            raise MemoryError("could not hook a Dolphin process")

    def read(self, address: int, length: int) -> bytes:
        self._ensure_hooked()
        try:
            return _dme.read_bytes(address, length)
        except Exception as exc:  # pragma: no cover - library-specific
            raise MemoryError(f"read failed: {exc}") from exc

    def write(self, address: int, data: bytes) -> None:
        self._ensure_hooked()
        try:
            _dme.write_bytes(address, data)
        except Exception as exc:  # pragma: no cover - library-specific
            raise MemoryError(f"write failed: {exc}") from exc
