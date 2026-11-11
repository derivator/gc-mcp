"""Swiss netboot client.

A custom build of Swiss (see ``~/swiss-gc/NETBOOT_USAGE.md``) runs a tiny
line-based TCP listener on the console's BBA. Sending a one-line command boots a
ROM from the console's own media — no controller or host-side media access is
needed. This module is the host-side client for that listener.

Protocol (one command per TCP connection on port 4242):

    PING               ->  PONG
    STATUS             ->  READY | BUSY
    BOOT <path>        ->  OK | ERR <reason>
    <anything else>    ->  ERR unknown command

``<path>`` must be a console-side path starting with ``sdc:/`` (SD2SP2) or
``fsp:/`` (the configured FSP file server) and must not contain a ``..``
component.
"""

from __future__ import annotations

import socket

from .errors import NetbootError

#: The netboot listener port (see NETBOOT_USAGE.md).
NETBOOT_PORT = 4242

#: Default per-connection timeout (seconds).
DEFAULT_TIMEOUT = 5.0

#: Console-side device prefixes the ``BOOT`` path may use.
_ALLOWED_PREFIXES = ("sdc:/", "fsp:/")

#: Maximum reply we bother reading (the protocol replies are tiny).
_MAX_REPLY = 1024


def validate_rom_path(path: str) -> str:
    """Validate a Swiss netboot ``BOOT`` path.

    The path must be a non-empty string starting with ``sdc:/`` or ``fsp:/`` and
    must not contain a ``..`` path component. Returns the stripped path, or
    raises :class:`ValueError` with a clear reason.
    """
    if not isinstance(path, str):
        raise ValueError("rom_path must be a string")
    path = path.strip()
    if not path:
        raise ValueError("rom_path is empty")
    if not path.startswith(_ALLOWED_PREFIXES):
        raise ValueError(
            "rom_path must start with 'sdc:/' or 'fsp:/' "
            "(a console-side Swiss path), e.g. 'sdc:/games/Foo.iso'"
        )
    for part in path.split("/"):
        if part == "..":
            raise ValueError("rom_path must not contain '..'")
    return path


class NetbootClient:
    """A one-shot client for a console's Swiss netboot listener.

    Each command opens a fresh TCP connection to the BBA IP, sends one line and
    reads the single-line reply, exactly as the protocol requires.
    """

    def __init__(self, host: str | None, port: int = NETBOOT_PORT,
                 timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = int(port)
        self.timeout = float(timeout)

    # ------------------------------------------------------------------
    # Low-level
    # ------------------------------------------------------------------
    def _command(self, line: str) -> str:
        """Send one line and return the stripped reply.

        Raises :class:`NetbootError` if the console cannot be reached or the
        listener is down (e.g. the console is inside a game).
        """
        if not self.host:
            raise NetbootError(
                "this hardware instance has no BBA IP configured; set it with "
                "gc_hardware_update(instance_id, bba_ip='<console-ip>')"
            )
        try:
            with socket.create_connection((self.host, self.port), self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(line.encode("ascii") + b"\n")
                data = sock.recv(_MAX_REPLY)
        except OSError as exc:
            raise NetbootError(
                f"could not reach the Swiss netboot listener at "
                f"{self.host}:{self.port}: {exc}"
            ) from exc
        return data.decode("ascii", errors="replace").strip()

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    def ping(self) -> str:
        """Liveness check. Returns ``PONG`` (or raises on failure)."""
        return self._command("PING")

    def status(self) -> str:
        """Return the listener state: ``READY`` or ``BUSY``."""
        return self._command("STATUS")

    def boot(self, rom_path: str) -> str:
        """Request a boot. Returns ``OK`` or ``ERR <reason>``.

        ``rom_path`` is validated first; a non-console-side path raises
        :class:`ValueError` before anything is sent.
        """
        path = validate_rom_path(rom_path)
        return self._command(f"BOOT {path}")

    def reachable(self) -> bool:
        """Whether the netboot listener is currently answering.

        Returns ``False`` if the console cannot be reached (e.g. it is inside a
        game and has torn the listener down, or the host is unreachable).
        """
        try:
            self.ping()
            return True
        except NetbootError:
            return False

    def __repr__(self) -> str:
        return f"<NetbootClient host={self.host!r} port={self.port}>"
