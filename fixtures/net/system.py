"""A machine with a hardware report (`facter.json`) that lists one Ethernet interface (BSX-297, v1 fixture `fx-net`)."""

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
)
