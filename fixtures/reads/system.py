"""An entry that reads a file of the workspace (the staleness guard of 009-D45)."""
# reads: note.txt

from pathlib import Path

from basix_config import *

machine = Machine(
    name=(Path(__file__).parent / "note.txt").read_text().strip(),
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
)
