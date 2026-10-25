"""A machine that sets its own values for the core defaults (SG31, EX1B): no `mkForce` and no removed line."""

from basix_config import *
from pynix_os.nix.gc import Gc
from pynix_os.time.__options__ import Time

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    state_version="26.05",
    options=[
        Time(timeZone="America/New_York"),
        Gc(options="--delete-older-than 14d"),
    ],
)
