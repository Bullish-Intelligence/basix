"""The option paths that a fragment sets (`basix explain`, 009-D14).

`system.json` holds the key `provenance`: for each Python field of `Machine` and each capability that the entry uses,
the option paths that its fragments set. The command `basix explain` reads the key and asks Nix for the value of each
path and for the line that sets it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

__all__ = ["add", "leaves"]

#: A dictionary with one of these keys is a value (a reference or a literal), not a level of options.
_VALUE_KEYS = frozenset({"__nixdantic_ref__", "__nixdantic_literal__"})


def leaves(path: Iterable[str], value: Any) -> list[str]:
    """The option paths below `path` that `value` sets, each as `a.b.c`.

    A dictionary is a level of options. A list, a text, a number, a reference, an empty dictionary, and a literal are
    the value of one option.
    """
    here = tuple(path)
    if isinstance(value, dict) and value and not _VALUE_KEYS & value.keys():
        return [p for key, inner in value.items() for p in leaves((*here, key), inner)]
    return [".".join(here)] if here else []


def add(table: dict[str, list[str]], owner: str | Mapping[str, str], fragment: Mapping[str, Any]) -> None:
    """Add the option paths of one fragment to `table`.

    `owner` is the name of the field or the capability that made the fragment. A fragment that two fields made together
    (the `basix` options of `user` and `serial_console`) gets a map from the first key of its value to the field.
    """
    path, value = tuple(fragment["path"]), fragment["value"]
    if isinstance(owner, str):
        found = [(owner, p) for p in leaves(path, value)]
    elif isinstance(value, dict):
        found = [(owner[key], p) for key, inner in value.items() for p in leaves((*path, key), inner)]
    else:
        found = []
    for name, option in found:
        if option not in table.setdefault(name, []):
            table[name].append(option)
