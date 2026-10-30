"""The base classes of a profile and a capability (CONCEPT 6.1, 6.2).

A component is a frozen, strict Pydantic model with typed fields and an `emit` method. `emit` returns values of the
generated models (`pynix_os`) and, where a rule must read the final configuration, a `NativeModule`. Python does not
merge anything: each value becomes one fragment, and NixOS merges the fragments.
"""

from __future__ import annotations

import inspect
import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from nixdantic import Model, Plain
from pydantic import ConfigDict, PrivateAttr
from pynix_native import NativeModule

if TYPE_CHECKING:
    from basix_config.machine import Machine

__all__ = [
    "Capability",
    "Component",
    "ComponentError",
    "Profile",
    "learner_frame",
    "learner_path",
    "learner_site",
    "set_workspace",
    "site_name",
]

_HERE = Path(os.path.abspath(__file__)).parent
#: The folder of the entry file. The compile sets it. A site is a path below it.
_WORKSPACE: Path | None = None
#: A folder name that marks library code or a tool folder, never learner code.
_NOT_LEARNER = frozenset(
    {"pydantic", "nixdantic", "site-packages", "dist-packages", ".venv", ".devenv", ".direnv", ".git", ".jj", ".gitman"}
)


def set_workspace(root: Path | None) -> None:
    """Name the folder of the entry file, so a site shows a path below it. `None` clears it."""
    global _WORKSPACE
    _WORKSPACE = root


def _below(path: Path, root: Path) -> Path | None:
    """The part of `path` below `root`. Try the path as written, then with the links followed."""
    for candidate in (path, Path(os.path.realpath(path))):
        try:
            return candidate.relative_to(root)
        except ValueError:
            continue
    return None


def learner_path(path: Path, root: Path | None = None) -> str | None:
    """The name of a file of the learner, or `None` for any other file.

    Learner code is a file below the workspace folder. The name is the path below that folder, with `/` between the
    parts (`helpers/desk.py`). Code of an input package is a file below `.basix/pynix`. Its name is the path below that
    folder (`comps/desk.py`). The generated `pynix_*` packages, the libraries of Basix, and tool folders are not
    learner code.
    """
    root = root or _WORKSPACE
    if root is None or path.name.startswith("<") or _HERE in path.parents:
        return None
    below = _below(path, root)
    if below is None or _NOT_LEARNER & set(below.parts):
        return None
    if below.parts[0] == ".basix":
        if below.parts[1:2] != ("pynix",) or len(below.parts) < 4 or below.parts[2].startswith("pynix_"):
            return None
        below = Path(*below.parts[2:])
    return below.as_posix()


def site_name(path: Path) -> str:
    """The name of a file in a `file:line` site: the path below the workspace folder, else the file name."""
    return learner_path(path) or path.name


def learner_frame() -> tuple[Path, int] | None:
    """The file and the line of the first caller outside this package and Pydantic: the line of the learner.

    A capability file is a symlink into this package (010-D10). Python names it by the link path, so the check
    compares the path as written, without following the link.
    """
    for frame in inspect.stack()[2:]:
        path = Path(os.path.abspath(frame.filename))
        if _HERE in path.parents or {"pydantic", "nixdantic"} & set(path.parts) or path.name.startswith("<"):
            continue
        return path, frame.lineno
    return None


def learner_site() -> str:
    """`file:line` of the learner line, as `learner_frame` finds it (the file is a path below the workspace folder)."""
    found = learner_frame()
    return f"{site_name(found[0])}:{found[1]}" if found else "unknown"


class Component(Plain):
    """The base of a profile and a capability. A component is always an instance."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    _where: str = PrivateAttr(default="")

    def model_post_init(self, context: object, /) -> None:
        self._where = learner_site()

    def emit(self, machine: Machine) -> Iterator[Model | NativeModule]:
        """The values of this component. Override it."""
        return iter(())


class Profile(Component):
    """A profile: the base of a machine."""


class Capability(Component):
    """A capability: one function of a machine."""


class ComponentError(Exception):
    """A value that a component emits is not valid. It names the learner line of the component."""

    def __init__(self, where: str, label: str, error: Exception):
        super().__init__(f"{where} ({label})")
        self.where, self.label, self.error = where, label, error
