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

__all__ = ["Capability", "Component", "ComponentError", "Profile", "learner_frame", "learner_site"]

_HERE = Path(os.path.abspath(__file__)).parent


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
    """`file:line` of the learner line, as `learner_frame` finds it (the file name has no folder)."""
    found = learner_frame()
    return f"{found[0].name}:{found[1]}" if found else "unknown"


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
