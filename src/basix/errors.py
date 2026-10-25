"""The conflict line (009-D20, BSX-385): two learner values that NixOS cannot merge.

NixOS prints a conflict as the option name and one line for each definition: ``- In `system.py:7 (RemoteLogin)': "no"``.
The fragments of the compiler carry the learner line as their file name (`_file`, for example `system.py:7 (RemoteLogin)`).
So when every definition comes from a learner line, one plain line names them all. The Nix text stays in the log.
"""

import re

from basix.layer import BasixWords

CONFLICT = re.compile(
    r"The (?:unique )?option [`'](?P<option>[^'`]+)' (?:has conflicting definition values|is defined multiple times)"
)
DEFINITION = re.compile(r"^\s*- In [`'](?P<file>.+?)'(?::|$)", re.MULTILINE)
END_OF_LIST = re.compile(r"Use `lib\.|^\s*error:", re.MULTILINE)


def conflict_line(text: str, entry: str, words: BasixWords) -> str | None:
    """One plain line for a conflict between two learner lines, or None when the error is something else.

    One line can make both values: an item of `options=[...]` carries the line of the `Machine` call. Then the line
    says that this line gives two values. A definition from a file that is not a learner line (the core,
    a native module) gives None: the learner cannot remove that line, so the general error text serves better.
    """
    found = CONFLICT.search(text)
    if found is None:
        return None
    rest = text[found.end() :]
    stop = END_OF_LIST.search(rest)
    files = DEFINITION.findall(rest[: stop.start()] if stop else rest)
    learner = re.compile(rf"{re.escape(entry)}:(\d+) \(")
    numbered: dict[str, int] = {}
    for place in dict.fromkeys(files):
        match = learner.match(place)
        if match is None:
            return None
        numbered[place] = int(match[1])
    if not numbered or (len(numbered) == 1 and len(files) < 2):
        return None
    option = f"NixOS option {found['option']}"
    if len(numbered) == 1:
        return words.conflict_one.format(places=next(iter(numbered)), option=option)
    places = sorted(numbered, key=numbered.__getitem__)
    return words.conflict.format(places=join(places), option=option)


def join(places: list[str]) -> str:
    """`a and b`, or `a, b, and c`."""
    if len(places) == 2:
        return f"{places[0]} and {places[1]}"
    return f"{', '.join(places[:-1])}, and {places[-1]}"
