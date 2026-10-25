"""Two items of `options=[...]` that set one option to two values (BSX-385): each item carries its own line."""

from pynix_os.time.__options__ import Time

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    options=[
        Time(timeZone="America/New_York"),
        Time(timeZone="UTC"),
    ],
)
