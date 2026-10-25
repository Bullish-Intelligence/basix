"""The Basix host of the minix VM check (`MINIX_VM_HOST=basix`): a Basix machine with no disk layout, for a VM (P3)."""

from basix_config import *

machine = Machine(
    name="basix",
    user="learner",
    disk=None,
    state_version="26.05",
    serial_console="ttyS0",
    native_modules=[NativeModule("vm-root.nix")],
)
