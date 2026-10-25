"""Choose the default folder for VM files and scratch.

The folder is `$BASIX_WORK_BASE/NAME`, else `$XDG_CACHE_HOME/basix/NAME` (default `~/.cache/basix/NAME`).
`BASIX_WORK_BASE` moves every folder of this module to a bigger drive. A person who wants one exact folder sets
`BASIX_WORK` (or the variable of one script, such as `BASIX_TRY_WORK`) in the environment.

The module uses the standard library only, so a script that runs in isolated mode can load it after a
`sys.path.insert`. `python workdir.py NAME` prints the folder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def default_work(name: str) -> Path:
    """`$BASIX_WORK_BASE/NAME`, else `$XDG_CACHE_HOME/basix/NAME`, else `~/.cache/basix/NAME`."""
    base = os.environ.get("BASIX_WORK_BASE")
    if base:
        return Path(base) / name
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(cache) / "basix" / name


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: workdir.py NAME")
    print(default_work(sys.argv[1]))
