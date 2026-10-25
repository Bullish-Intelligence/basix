"""A machine that uses the advanced import (L128, BSX-396): `Force` wins over a normal value, `Before` and `After` order a list."""

from nixdantic import After, Before, Force
from pynix_os.networking.__options__ import Networking
from pynix_os.time.__options__ import Time

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    state_version="26.05",
    options=[
        Time(timeZone=Force("America/New_York")),
        Networking(nameservers=Before(["1.1.1.1"])),
        Networking(nameservers=After(["8.8.8.8"])),
    ],
)
