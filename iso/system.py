"""The live Basix system that runs the installer."""

from basix_config import *

machine = Machine(
    name="basix-iso",
    user="nixos",
    disk=None,
    serial_console="ttyS0",
)
