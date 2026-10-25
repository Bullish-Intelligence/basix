from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-basix-disk",
    profile=Console(),  # Desktop()
    capabilities=[RemoteLogin()],
    programs=[],  # Program(pkgs.htop)
    trusted_interfaces=[],  # "enp1s0"
    user_ports=[],  # Port(8080), Ports(8000, 8099)
    serial_console=None,  # "ttyS0"
    options=[],  # Firewall(allowPing=False)  (import the class from pynix_os)
    native_modules=[],  # NativeModule("fans.nix")
)
