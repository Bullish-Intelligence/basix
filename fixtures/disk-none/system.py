"""The migration mode (009-D39, SG31 EX1M): `disk=None` writes no disk layout. The machine lists its own file systems."""

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk=None,
    state_version="26.05",
)
