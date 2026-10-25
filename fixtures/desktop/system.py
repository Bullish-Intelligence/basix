"""The desktop machine: the core fixture with the profile `Desktop()` (L66, P6)."""

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    profile=Desktop(),
)
