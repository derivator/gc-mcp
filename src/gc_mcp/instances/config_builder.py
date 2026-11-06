"""Per-instance Dolphin configuration writer.

Each Dolphin instance is isolated by its own ``--user`` data directory. This
module writes the configuration files Dolphin needs to present a *stock*,
unmodified emulator as a GameCube with the peripherals we want:

* ``Config/Dolphin.ini``  - which GameCube SI ports have a controller attached,
  and the SP1 (Broadband Adapter / BBA) device and its settings.
* ``Config/GCPadNew.ini`` - maps each GameCube controller port to a Dolphin
  "Pipes" device (``Pipe/0/ctrlN``) so we can drive it over named FIFOs.
* ``Config/Hotkeys.ini``  - emulator hotkeys (pause, frame-advance, save/load
  state). Written as a *complete* set because Dolphin loads it all-or-nothing.
* ``Config/GFX.ini``      - video backend / renderer settings.
* ``Config/Logger.ini``   - enables Dolphin's OSREPORT log so ``SYS_Report``
  output (e.g. a ROM's debug/console messages) is written to the log file.
* ``Pipes/``              - the named FIFOs the Pipes backend scans at startup.

The config values are taken directly from Dolphin's own ``ConfigInfo`` /
``MainSettings`` definitions, so they are correct for stock Dolphin.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import ConfigError
from ..keyboard import DEFAULT_HOTKEYS

# ---------------------------------------------------------------------------
# Dolphin EXI device types (SerialPort1 / SP1), from EXI_Device.h.
# ---------------------------------------------------------------------------
EXI_DUMMY = 0
EXI_MEMORYCARD = 1
EXI_MASKROM = 2
EXI_AD16 = 3
EXI_MICROPHONE = 4
EXI_ETHERNET = 5  # Broadband Adapter (TAP)
EXI_BASEBOARD = 6
EXI_GECKO = 7
EXI_MEMORYCARD_FOLDER = 8
EXI_AGP = 9
EXI_ETHERNET_XLINK = 10  # Broadband Adapter (XLink Kai)
EXI_ETHERNET_TAPSERVER = 11
EXI_ETHERNET_BUILTIN = 12  # Broadband Adapter (built-in, HLE)
EXI_MODEM_TAPSERVER = 13
EXI_ETHERNET_IPC = 14  # Broadband Adapter (IPC / local mode)
EXI_NONE = 0xFF

#: Mapping from the public BBA mode names to the EXI device integer values.
BBA_MODE_EXI = {
    "ipc": EXI_ETHERNET_IPC,
    "hle": EXI_ETHERNET_BUILTIN,
    "xlink": EXI_ETHERNET_XLINK,
    "tap": EXI_ETHERNET,
    "tapserver": EXI_ETHERNET_TAPSERVER,
    "modem": EXI_MODEM_TAPSERVER,
}

# ---------------------------------------------------------------------------
# Dolphin SI device types (SIDevice0..3), from SI_Device.h.
# ---------------------------------------------------------------------------
SI_DEVICE_NONE = 0
SI_DEVICE_GC_CONTROLLER = 6

#: Valid MAC prefixes Dolphin accepts for a BBA.
VALID_MAC_PREFIXES = ("00:09:bf", "00:17:ab")


def normalize_bba_mode(mode: str | None) -> int | None:
    """Convert a BBA mode string to the EXI device value, or None to disable."""
    if mode is None:
        return None
    mode = str(mode).strip().lower()
    if mode in ("", "none", "off", "disabled"):
        return None
    if mode not in BBA_MODE_EXI:
        raise ConfigError(
            f"unknown BBA mode {mode!r}; expected one of "
            f"{', '.join(BBA_MODE_EXI)} (or none/off to disable)"
        )
    return BBA_MODE_EXI[mode]


def normalize_mac(mac: str | None, default_prefix: str = "00:09:bf") -> str:
    """Validate / generate a BBA MAC address.

    Dolphin requires the MAC to begin with ``00:09:bf`` or ``00:17:ab``. If
    ``mac`` is None, generate one using the default prefix.
    """
    if mac is None or str(mac).strip() == "":
        prefix = default_prefix if default_prefix in VALID_MAC_PREFIXES else "00:09:bf"
        # A stable, unique-enough MAC derived from a counter kept by the caller
        # is preferable; here we fall back to a random host part.
        import random

        host = "%02x:%02x:%02x" % (
            random.randint(0, 255),
            random.randint(0, 255),
            random.randint(0, 255),
        )
        return f"{prefix}:{host}"
    mac = str(mac).strip().lower()
    if not mac.startswith(VALID_MAC_PREFIXES):
        raise ConfigError(
            f"invalid BBA MAC {mac!r}; must start with one of "
            f"{', '.join(VALID_MAC_PREFIXES)}"
        )
    return mac


@dataclass
class InstanceConfig:
    """Everything needed to write one instance's Dolphin configuration."""

    user_dir: Path
    #: Which controller ports (0-3) should have a standard GameCube controller.
    controller_ports: tuple[int, ...] = (0,)
    #: BBA mode; None disables the Broadband Adapter.
    bba_mode: str | None = None
    bba_mac: str | None = None
    bba_ip: str | None = None
    bba_dns: str | None = None
    bba_xlink_ip: str | None = None
    bba_tapserver_dest: str | None = None
    #: Graphics backend Dolphin should use (also settable via ``-v``).
    video_backend: str | None = None
    internal_resolution: int | None = None
    #: Extra Dolphin.ini ``[Core]`` overrides.
    extra_core: dict[str, str] = field(default_factory=dict)
    #: Extra GCPadNew.ini overrides keyed by ``(port, key)``.
    extra_pad: dict[tuple[int, str], str] = field(default_factory=dict)

    @property
    def config_dir(self) -> Path:
        return self.user_dir / "Config"

    @property
    def pipes_dir(self) -> Path:
        return self.user_dir / "Pipes"


class InstanceConfigBuilder:
    """Writes a Dolphin user-data directory for one instance."""

    def __init__(self, cfg: InstanceConfig) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def write(self) -> dict:
        """Write all configuration files and create the FIFOs.

        Returns a dict describing what was written.
        """
        cfg = self.cfg
        try:
            cfg.config_dir.mkdir(parents=True, exist_ok=True)
            self._write_dolphin_ini()
            self._write_gcpad_new_ini()
            self._write_hotkeys_ini()
            self._write_gfx_ini()
            self._write_logger_ini()
            cfg.pipes_dir.mkdir(parents=True, exist_ok=True)
            self._create_fifos()
        except ConfigError:
            raise
        except OSError as exc:
            raise ConfigError(f"failed to write instance config: {exc}") from exc

        return {
            "user_dir": str(cfg.user_dir),
            "controller_ports": list(cfg.controller_ports),
            "bba_mode": cfg.bba_mode,
            "pipes_dir": str(cfg.pipes_dir),
        }

    # ------------------------------------------------------------------
    # Dolphin.ini ([Core])
    # ------------------------------------------------------------------
    def _write_dolphin_ini(self) -> None:
        cfg = self.cfg
        core: dict[str, str] = {}

        # SI devices: each enabled port gets a standard GameCube controller.
        for port in range(4):
            if port in cfg.controller_ports:
                core[f"SIDevice{port}"] = str(SI_DEVICE_GC_CONTROLLER)
            else:
                core[f"SIDevice{port}"] = str(SI_DEVICE_NONE)

        # SP1 / Broadband Adapter.
        bba_exi = normalize_bba_mode(cfg.bba_mode)
        if bba_exi is not None:
            core["SerialPort1"] = str(bba_exi)
            core["BBA_MAC"] = normalize_mac(cfg.bba_mac)
            if cfg.bba_ip:
                core["BBA_BUILTIN_IP"] = cfg.bba_ip
            if cfg.bba_dns:
                core["BBA_BUILTIN_DNS"] = cfg.bba_dns
            if cfg.bba_xlink_ip:
                core["BBA_XLINK_IP"] = cfg.bba_xlink_ip
            if cfg.bba_tapserver_dest:
                core["BBA_TAPSERVER_DESTINATION"] = cfg.bba_tapserver_dest

        core.update(cfg.extra_core)

        # In headless ``--batch`` mode Dolphin's render window never receives
        # focus, so ``Core::UpdateInputGate()`` closes the input gate. With the
        # gate closed, *all* controller input is silently dropped (see
        # InputReference::State() in ControlReference.cpp) and *every* hotkey
        # that needs focus (Toggle Pause, Save/Load State, Frame Advance) is
        # ignored. Force ``[Input] BackgroundInput = True`` so the virtual Pipe
        # controller reaches the game, and ``[General] HotkeysRequireFocus =
        # False`` so the hotkey input gate stays open even without a focused
        # window. (``HotkeysRequireFocus`` is ``MAIN_FOCUSED_HOTKEYS``; default
        # is true, which would silently drop all hotkeys in batch mode.)
        input_section = {"BackgroundInput": "True"}
        general_section = {"HotkeysRequireFocus": "False"}

        self._write_ini_file(
            "Dolphin.ini",
            {"Core": core, "Input": input_section, "General": general_section},
        )

    # ------------------------------------------------------------------
    # GCPadNew.ini
    # ------------------------------------------------------------------
    def _write_gcpad_new_ini(self) -> None:
        cfg = self.cfg
        sections: dict[str, dict[str, str]] = {}

        for port in sorted(cfg.controller_ports):
            # GCPad1 corresponds to SI port 0, GCPad2 to port 1, etc.
            section_name = f"GCPad{port + 1}"
            # Pipes device: backend "Pipe", device index 0, FIFO name ctrlN.
            device = f"Pipe/0/ctrl{port + 1}"
            sections[section_name] = self._pad_section(device, port)

        self._write_ini_file("GCPadNew.ini", sections)

    def _pad_section(self, device: str, port: int) -> dict[str, str]:
        cfg = self.cfg
        pad: dict[str, str] = {
            "Device": device,
            # Force the emulated controller to always be "connected", so input is
            # delivered even if Dolphin can't detect the (virtual) pipe device as a
            # real, present controller. Value is Dolphin's bool ini serialization
            # ("True"/"False"); see Common/StringUtil.cpp ValueToString(bool).
            "Options/Always Connected": "True",
            # Buttons
            "Buttons/A": "`Button A`",
            "Buttons/B": "`Button B`",
            "Buttons/X": "`Button X`",
            "Buttons/Y": "`Button Y`",
            "Buttons/Z": "`Button Z`",
            "Buttons/Start": "`Button START`",
            # D-Pad
            "D-Pad/Up": "`Button D_UP`",
            "D-Pad/Down": "`Button D_DOWN`",
            "D-Pad/Left": "`Button D_LEFT`",
            "D-Pad/Right": "`Button D_RIGHT`",
            # Triggers (digital + analog, so `SET L/R <v>` moves the analog axis)
            "Triggers/L": "`Button L`",
            "Triggers/R": "`Button R`",
            "Triggers/L-Analog": "`Axis L +`",
            "Triggers/R-Analog": "`Axis R +`",
            # Main stick
            "Main Stick/Up": "`Axis MAIN Y +`",
            "Main Stick/Down": "`Axis MAIN Y -`",
            "Main Stick/Left": "`Axis MAIN X -`",
            "Main Stick/Right": "`Axis MAIN X +`",
            # C stick
            "C-Stick/Up": "`Axis C Y +`",
            "C-Stick/Down": "`Axis C Y -`",
            "C-Stick/Left": "`Axis C X -`",
            "C-Stick/Right": "`Axis C X +`",
            # Centers
            "Main Stick/Center": "0.00 0.00",
            "C-Stick/Center": "0.00 0.00",
        }
        # Apply any per-pad overrides.
        for key, value in cfg.extra_pad.items():
            kport, kkey = key
            if kport == port:
                pad[kkey] = value
        return pad

    # ------------------------------------------------------------------
    # Hotkeys.ini
    # ------------------------------------------------------------------
    def _write_hotkeys_ini(self) -> None:
        """Write a complete ``Hotkeys.ini`` so emulator hotkeys work.

        Dolphin loads ``Hotkeys.ini`` all-or-nothing: if the file exists, any
        control not listed is left *unbound*. We therefore write the full default
        binding set (pause, save/load state, etc.) plus a Frame Advance binding,
        so ``gc_emulator_step_frame`` works out of the box.
        """
        from ..keyboard import build_hotkey_ini

        path = self.cfg.config_dir / "Hotkeys.ini"
        path.write_text(build_hotkey_ini(DEFAULT_HOTKEYS), encoding="utf-8")

    # ------------------------------------------------------------------
    # GFX.ini
    # ------------------------------------------------------------------
    def _write_gfx_ini(self) -> None:
        cfg = self.cfg
        settings: dict[str, str] = {}
        if cfg.video_backend:
            settings["GFXBackend"] = cfg.video_backend
        if cfg.internal_resolution:
            settings["InternalResolution"] = str(cfg.internal_resolution)
        if settings:
            self._write_ini_file("GFX.ini", {"Settings": settings})

    # ------------------------------------------------------------------
    # Logger.ini
    # ------------------------------------------------------------------
    def _write_logger_ini(self) -> None:
        """Write a ``Logger.ini`` that enables Dolphin's OSREPORT log.

        Dolphin only forwards a ROM's ``SYS_Report`` / ``OSReport`` calls to the
        log when the ``OSREPORT`` log type is enabled. A fresh ``--user`` data
        directory ships an empty (0-byte) ``Logger.ini``, so the ROM's console
        output never reaches ``Logs/dolphin.log``. We write the file explicitly
        so that a running ROM's ``OSReport`` output is observable.
        """
        self._write_ini_file(
            "Logger.ini",
            {
                "Options": {
                    "WriteToFile": "True",
                    "WriteToConsole": "True",
                    "WriteToWindow": "True",
                    "Verbosity": "4",
                },
                "Logs": {
                    "OSREPORT": "True",
                    "OSREPORT_HLE": "True",
                    "MASTER": "True",
                },
            },
        )

    # ------------------------------------------------------------------
    # FIFOs
    # ------------------------------------------------------------------
    def _create_fifos(self) -> None:
        cfg = self.cfg
        cfg.pipes_dir.mkdir(parents=True, exist_ok=True)
        for port in cfg.controller_ports:
            path = cfg.pipes_dir / f"ctrl{port + 1}"
            if path.exists():
                continue
            try:
                os.mkfifo(path)
            except OSError as exc:
                raise ConfigError(f"could not create FIFO {path}: {exc}") from exc

    # ------------------------------------------------------------------
    # INI helper
    # ------------------------------------------------------------------
    def _write_ini_file(self, filename: str, sections: dict[str, dict[str, str]]) -> None:
        path = self.cfg.config_dir / filename
        lines: list[str] = []
        for section_name, kv in sections.items():
            lines.append(f"[{section_name}]")
            for key, value in kv.items():
                lines.append(f"{key} = {value}")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
