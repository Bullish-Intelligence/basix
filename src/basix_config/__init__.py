"""Basix curated tier (CONCEPT 3.1, 6.1): the names a learner writes in `system.py`.

A learner writes one `Machine` in `system.py`. A capability is a Python class: a frozen Pydantic model with its
own typed settings and an `emit` method. `emit` returns values of the generated models (`pynix_os`) and, where a rule
must read the final configuration, a `NativeModule`. Python does not merge anything: each value becomes one fragment,
and NixOS merges the fragments.

The star import of the written entry gives the learner names only (009-D23). The module exports no priority wrapper
(`Force`, `Before`, `After`, `Order`): an advanced file imports them from `nixdantic` (L128).
"""

from __future__ import annotations

from nixdantic import ConfigRef, Native, Package, Text
from pynix_native import OWN, Input, NativeModule, NixpkgsPin
from pynix_os import all as nixos
from pynix_pkgs import pkgs

from basix_config.cache import Cache
from basix_config.capabilities.remote_login import RemoteLogin
from basix_config.component import Capability, ComponentError, Profile
from basix_config.desktop import Desktop
from basix_config.machine import Console, Machine, Program
from basix_config.ports import Port, Ports

__all__ = [
    "OWN",
    "Cache",
    "ConfigRef",
    "Console",
    "Desktop",
    "Input",
    "Machine",
    "Native",
    "NativeModule",
    "NixpkgsPin",
    "Package",
    "Port",
    "Ports",
    "Program",
    "RemoteLogin",
    "Text",
    "nixos",
    "pkgs",
]

# Names for the compiler and the tests. They are not learner names.
_ = (Capability, ComponentError, Profile)
