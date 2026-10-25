"""The Nix calls of Basix (009-D29): every `nix` command gets the two experimental features that Basix needs.

A failed `nix` command may hold a conflict between two learner lines. This runner then returns the one plain line
(009-D20) in place of the Nix text. The log still gets the Nix text, because the lines go to `on_line` as they come.
"""

from collections.abc import Callable, Sequence
from pathlib import Path

from pynix_engine import Result, Runner

from basix.errors import conflict_line
from basix.layer import NIX_FEATURES, WORDS


def with_features(argv: Sequence[str]) -> list[str]:
    """Add the features after the program name of a `nix` command. Leave every other command as it is."""
    words = list(argv)
    if words[:1] == ["nix"]:
        return [words[0], *NIX_FEATURES, *words[1:]]
    return words


class NixRunner:
    """A runner that wraps another one. The engine, the compile step, and `update` all run `nix` through it."""

    def __init__(self, inner: Runner, entry: str = "system.py") -> None:
        self.inner = inner
        self.entry = entry

    def run(
        self, argv: Sequence[str], *, cwd: Path | None = None, on_line: Callable[[str], None] | None = None
    ) -> Result:
        result = self.inner.run(with_features(argv), cwd=cwd, on_line=on_line)
        if result.code != 0 and list(argv[:1]) == ["nix"]:
            line = conflict_line(result.out, self.entry, WORDS)
            if line:
                return Result(result.code, line + "\n")
        return result
