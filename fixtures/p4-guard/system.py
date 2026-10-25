"""The test machine for the Basix disk guard."""

from basix_config import *

machine = Machine(
    name="guardvm",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    state_version="26.05",
    serial_console="ttyS0",
)
