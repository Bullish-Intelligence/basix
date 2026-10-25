"""A machine with no trusted link that opens SSH on its own links (010-D45): `RemoteLogin()` still announces the machine there."""

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    capabilities=[RemoteLogin()],
    user_ports=[Port(22, interface="eno1"), Port(22, interface="wlp2s0"), Port(80, interface="enp3s0")],
)
