"""The core machine: one guarded disk and one account (v1 fixture `fx-target`)."""

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
)
