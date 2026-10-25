from basix_config import *

machine = Machine(
    name="basix",
    user="learner",
    disk="/dev/disk/by-id/nvme-EXAMPLE",
    state_version="26.11",
    serial_console=None,  # "ttyS0"
    profile=Console(),
    capabilities=[],  # RemoteLogin()
    programs=[],  # Program(pkgs.htop)
    trusted_interfaces=[],  # "enp1s0"
    user_ports=[],  # Port(8080), Ports(8000, 8099)
)
