"""Error hierarchy for gc-mcp.

Every tool that can fail raises one of these. The server lets them propagate;
the MCP server (``MCPServer``) turns them into ``is_error`` tool results so the
client sees a useful message instead of a generic crash string.

``GCMCPError`` subclasses mcp's ``ToolError`` so that a raised error keeps its
own message and is surfaced to the client as an anticipated failure (no server
traceback), rather than being swallowed into the generic "Error executing tool"
message.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError


class GCMCPError(ToolError):
    """Base error for all gc-mcp failures."""

    #: A stable machine-readable code (mirrors JSON-RPC / MCP conventions loosely).
    code: int = -32000

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "details": self.details}


class NotFoundError(GCMCPError):
    """An instance or resource does not exist."""

    code = -32001


class AlreadyExistsError(GCMCPError):
    """An instance or resource already exists."""

    code = -32002


class UnsupportedError(GCMCPError):
    """The operation is not supported by the target platform (e.g. real hardware
    endpoints that are stubs)."""

    code = -32003


class ControllerError(GCMCPError):
    """A controller operation failed (e.g. FIFO not writable, invalid button)."""

    code = -32004


class CaptureError(GCMCPError):
    """A screenshot / capture operation failed."""

    code = -32005


class MemoryError(GCMCPError):
    """A memory read/write operation failed."""

    code = -32006


class ConfigError(GCMCPError):
    """Dolphin configuration could not be read or written."""

    code = -32007


class StartupError(GCMCPError):
    """The emulator failed to start or reach a ready state."""

    code = -32008


class SequenceError(GCMCPError):
    """A timed input sequence failed to schedule or run."""

    code = -32009
