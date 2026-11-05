"""Dolphin instance lifecycle & configuration."""

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
    "InstanceConfig",
    "InstanceConfigBuilder",
    "normalize_bba_mode",
    "normalize_mac",
    "DolphinInstance",
    "InstanceSpec",
    "InstanceManager",
]
