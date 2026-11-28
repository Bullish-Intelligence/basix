"""Read one JSON answer from the output of a command that may print other lines too.

`nix eval --json` prints its answer as one line on stdout. A runner that joins stderr to stdout adds lines of its own
around it: a fetch note such as ` * [new tag] v1 -> v1` holds a `[` that is not the start of the answer. So the reader
looks at each line, last first, and takes the first line that is a complete JSON value of the kind that the caller
wants. It never cuts the text at the first bracket.
"""

from __future__ import annotations

import json

__all__ = ["last_json_list", "last_json_string"]


def _last_line(out: str, opener: str, kind: type) -> object | None:
    for line in reversed(out.splitlines()):
        text = line.strip()
        if not text.startswith(opener):
            continue
        try:
            value = json.loads(text)
        except ValueError:
            continue
        if isinstance(value, kind):
            return value
    return None


def last_json_list(out: str) -> list[str] | None:
    """The last line of `out` that is a JSON list of strings, or None when no line is."""
    value = _last_line(out, "[", list)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    return None


def last_json_string(out: str) -> str | None:
    """The last line of `out` that is a JSON string, or None when no line is."""
    value = _last_line(out, '"', str)
    return value if isinstance(value, str) else None
