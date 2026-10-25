"""A trusted link (L125): `trusted_interfaces` admits all traffic on that link, and `RemoteLogin()` opens port 22 on it only."""

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    capabilities=[RemoteLogin()],
    trusted_interfaces=["enp1s0"],
)
