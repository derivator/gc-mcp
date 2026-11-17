"""Instance lifecycle & configuration (Dolphin emulators and real hardware)."""

from .config_builder import (
    BBA_MODE_EXI,
    EXI_ETHERNET,
    EXI_ETHERNET_BUILTIN,
    EXI_ETHERNET_IPC,
    EXI_ETHERNET_TAPSERVER,
    EXI_ETHERNET_XLINK,
    EXI_MODEM_TAPSERVER,
    EXI_NONE,
    InstanceConfig,
    InstanceConfigBuilder,
    normalize_bba_mode,
    normalize_mac,
)
from .dolphin_instance import DolphinInstance, InstanceSpec
from .hardware_instance import (
    MAX_SERIAL_DEVICES,
    HardwareInstance,
    HardwareInstanceSpec,
)
from .hardware_manager import HardwareManager
from .manager import InstanceManager

__all__ = [
    "BBA_MODE_EXI",
    "EXI_ETHERNET",
    "EXI_ETHERNET_BUILTIN",
    "EXI_ETHERNET_IPC",
    "EXI_ETHERNET_TAPSERVER",
    "EXI_ETHERNET_XLINK",
    "EXI_MODEM_TAPSERVER",
    "EXI_NONE",
    "MAX_SERIAL_DEVICES",
    "DolphinInstance",
    "HardwareInstance",
    "HardwareInstanceSpec",
    "HardwareManager",
    "InstanceConfig",
    "InstanceConfigBuilder",
    "InstanceManager",
    "InstanceSpec",
    "normalize_bba_mode",
    "normalize_mac",
]
