"""Checked field types of the curated tier (CONCEPT 3.1, 009-D33, 009-D39).

Each type carries its own plain error text. `Expect` replaces the error of a wrong kind of value with `must be TEXT`.
A validator after it keeps its own text.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, get_args

from nixdantic import Expect
from pydantic import AfterValidator, BaseModel, StringConstraints
from pydantic_core import PydanticCustomError

__all__ = ["Account", "ConsoleDevice", "DiskId", "Interface", "Name", "StateVersion", "interface_names"]


def _disk_id(value: str) -> str:
    """A message for each way a disk path is wrong."""
    if value.startswith("/dev/disk/by-id/") and len(value) > len("/dev/disk/by-id/"):
        return value
    if re.match(r"/dev/(sd|hd|vd|nvme|mmcblk)", value):
        raise PydanticCustomError(
            "disk_kernel_name",
            "must not be a kernel name: kernel names move between boots. "
            "Use a path below /dev/disk/by-id/ (list them with `ls -l /dev/disk/by-id/`)",
        )
    raise PydanticCustomError("disk_id", "must be a path below /dev/disk/by-id/, for example /dev/disk/by-id/nvme-NAME")


def _interface(value: str) -> str:
    """The interface rule (Basix owns it): 1 to 15 letters, digits, `.`, `-`, or `_`. `@` and `:` get a reason."""
    if "@" in value:
        raise PydanticCustomError(
            "interface_peer", "must not hold '@': `ip link` prints NAME@PEER; use only the part before @"
        )
    if ":" in value:
        raise PydanticCustomError("interface_alias", "must not hold ':': an alias label is not an interface")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", value):
        raise PydanticCustomError(
            "interface_name", "must be an interface name (1 to 15 letters, digits, '_', '.', or '-')"
        )
    return value


Name = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]*$"),
    Expect("a machine name (a lower case letter, then lower case letters, digits, or '-')"),
]
Account = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,31}$"),
    Expect("an account name (a lower case letter, then up to 31 lower case letters, digits, or '-')"),
]
DiskId = Annotated[str, AfterValidator(_disk_id)]
Interface = Annotated[str, AfterValidator(_interface)]
ConsoleDevice = Annotated[
    str,
    StringConstraints(pattern=r"^(tty[A-Za-z]*[0-9]+|hvc[0-9]+)$"),
    Expect("a kernel console device such as ttyS0 or tty0"),
]
StateVersion = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]{2}\.(05|11)$"),
    Expect('a release name such as "26.05" (two digits for the year, then .05 or .11)'),
]


def _marks_interface(item: object) -> bool:
    """True when a type, or a part of it, is `Interface`."""
    if isinstance(item, AfterValidator):
        return item.func is _interface
    return any(_marks_interface(part) for part in get_args(item))


def interface_names(model: BaseModel) -> list[str]:
    """The interface names that a model holds in any field of the type `Interface`, once each, in order.

    The walk goes into lists and into nested models, so `trusted_interfaces`, `Port(..., interface=...)`, and a
    field of a capability all count.
    """
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, BaseModel):
            for name, info in type(value).model_fields.items():
                item = getattr(value, name, None)
                if _marks_interface(info.annotation) or any(_marks_interface(m) for m in info.metadata):
                    found.extend(i for i in (item if isinstance(item, list | tuple) else [item]) if isinstance(i, str))
                else:
                    walk(item)
        elif isinstance(value, list | tuple):
            for part in value:
                walk(part)

    walk(model)
    return list(dict.fromkeys(found))
