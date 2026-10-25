"""An entry with a flake input. `scripts/check_eval.py` uses the stored `system.json` of this fixture, because the
input is a stand-in with no source: it tests the guard lines of `lib.machine` (009-D48, 009-D49) and nothing else."""

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
)
