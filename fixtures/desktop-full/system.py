"""The desktop machine with every setting of `Desktop(...)` (L66, S18 `deskbox`, P6)."""

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    profile=Desktop(
        greeting="Welcome to Basix, Ada!",
        keyboard_layout="de",
        locale="en_US.UTF-8",
        extra_programs=[Program(pkgs.htop)],
    ),
)
