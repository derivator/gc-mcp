"""Dolphin "Pipes" controller backend.

Writes newline-delimited commands to named FIFOs in the instance's ``Pipes/``
directory. Each GameCube port maps to one FIFO, and the device is mapped in
``GCPadNew.ini`` as ``Pipe/0/ctrlN``.

Dolphin scans the ``Pipes/`` directory at startup, so the FIFOs must exist (and
the read ends opened) before the emulator scans them. We open the write ends
non-blocking and retry on ``ENXIO`` until Dolphin opens the read end.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from pathlib import Path

from ..errors import ControllerError
from . import pad_state as ps
from .base import ControllerBackend


class DolphinPipesController(ControllerBackend):
    """Writes Pipes commands for a single Dolphin instance."""

    def __init__(self, pipes_dir: Path, ports: int = 4, open_retries: int = 200,
                 retry_delay: float = 0.05) -> None:
        super().__init__()
        self.pipes_dir = Path(pipes_dir)
        self.ports = ports
        self.open_retries = open_retries
        self.retry_delay = retry_delay
        self._writers: dict[int, int] = {}
        self._state: dict[int, ps.PadState] = {}
        self._io_lock = threading.Lock()
        self._state_lock = threading.Lock()
        for port in range(ports):
            self._state[port] = ps.PadState(port=port)

    # ------------------------------------------------------------------
    # FIFO plumbing
    # ------------------------------------------------------------------
    def ensure_fifos(self) -> None:
        """Create the Pipes directory and FIFOs if they do not yet exist."""
        self.pipes_dir.mkdir(parents=True, exist_ok=True)
        for port in range(self.ports):
            path = self._fifo_path(port)
            if not path.exists():
                os.mkfifo(path)

    def _fifo_path(self, port: int) -> Path:
        # Port 0 -> ctrl1, port 1 -> ctrl2, etc. (matches GCPadNew.ini).
        return self.pipes_dir / f"ctrl{port + 1}"

    def _open_writer(self, port: int) -> int:
        path = self._fifo_path(port)
        last_err: OSError | None = None
        for _ in range(self.open_retries):
            try:
                return os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            except OSError as exc:
                if exc.errno == errno.ENXIO:
                    # No reader yet (Dolphin has not opened the read end).
                    last_err = exc
                    time.sleep(self.retry_delay)
                    continue
                raise
        raise ControllerError(
            f"could not open FIFO {path} for writing (no reader yet)",
            details={"path": str(path)},
        )

    def _reopen(self, port: int) -> None:
        old = self._writers.pop(port, None)
        if old is not None:
            try:
                os.close(old)
            except OSError:
                pass
        self._writers[port] = self._open_writer(port)

    def probe(self, port: int) -> bool:
        """Cheap, non-blocking check whether Dolphin has opened a FIFO's read end.

        Returns True if a reader is present (so a write would not get ``ENXIO``).
        Unlike :meth:`_open_writer`, this does a single attempt and never retries,
        so it is safe to poll frequently during startup.
        """
        try:
            fd = os.open(self._fifo_path(port), os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                return False
            raise
        os.close(fd)
        return True

    def _write(self, port: int, command: str) -> None:
        payload = (command + "\n").encode("ascii")
        with self._io_lock:
            fd = self._writers.get(port)
            if fd is None:
                self._reopen(port)
                fd = self._writers[port]
            try:
                os.write(fd, payload)
            except OSError as exc:
                if exc.errno in (errno.EPIPE, errno.ENXIO):
                    # Reader disappeared (Dolphin stopped / restarted): reopen.
                    self._reopen(port)
                    os.write(self._writers[port], payload)
                elif exc.errno == errno.EAGAIN:
                    # Non-blocking write would block; retry briefly.
                    for _ in range(10):
                        time.sleep(0.01)
                        try:
                            os.write(fd, payload)
                            return
                        except OSError as retry_exc:
                            if retry_exc.errno == errno.EAGAIN:
                                continue
                            raise
                    raise ControllerError(f"timed out writing to FIFO for port {port}")
                else:
                    raise

    # ------------------------------------------------------------------
    # Primitive actions
    # ------------------------------------------------------------------
    def press(self, port: int, button: str) -> None:
        port = ps.validate_port(port)
        token = ps.validate_button(button)
        self._write(port, f"PRESS {token}")
        with self._state_lock:
            self._state[port].buttons.add(token)

    def release(self, port: int, button: str) -> None:
        port = ps.validate_port(port)
        token = ps.validate_button(button)
        self._write(port, f"RELEASE {token}")
        with self._state_lock:
            self._state[port].buttons.discard(token)

    def set_stick(self, port: int, stick: str, x: float, y: float) -> None:
        port = ps.validate_port(port)
        stick = ps.validate_stick(stick)
        xp = ps.api_stick_to_pipe(x)
        yp = ps.api_stick_to_pipe(y)
        token = ps._STICK_TOKENS[stick]
        self._write(port, f"SET {token} {xp:.4f} {yp:.4f}")
        with self._state_lock:
            if stick == "main":
                self._state[port].main = (float(x), float(y))
            else:
                self._state[port].c = (float(x), float(y))

    def set_trigger(self, port: int, trigger: str, value: float) -> None:
        port = ps.validate_port(port)
        trigger = ps.validate_trigger(trigger)
        value = ps.api_trigger_to_pipe(value)
        self._write(port, f"SET {trigger} {value:.4f}")
        with self._state_lock:
            if trigger == "L":
                self._state[port].l = value
            else:
                self._state[port].r = value

    def get_state(self, port: int) -> ps.PadState:
        port = ps.validate_port(port)
        with self._state_lock:
            state = self._state[port]
            # Return a copy so callers can't mutate internal state.
            return ps.PadState(
                port=state.port,
                buttons=set(state.buttons),
                main=state.main,
                c=state.c,
                l=state.l,
                r=state.r,
            )

    def reset(self, port: int) -> None:
        port = ps.validate_port(port)
        for button in ps.BUTTONS:
            self._write(port, f"RELEASE {button}")
        self._write(port, "SET MAIN 0.5000 0.5000")
        self._write(port, "SET C 0.5000 0.5000")
        self._write(port, "SET L 0.0000")
        self._write(port, "SET R 0.0000")
        with self._state_lock:
            self._state[port] = ps.PadState(port=port)

    def close(self) -> None:
        super().close()
        with self._io_lock:
            for fd in self._writers.values():
                try:
                    os.close(fd)
                except OSError:
                    pass
            self._writers.clear()
