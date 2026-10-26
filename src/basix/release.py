"""What `basix update` prints: release names, the age of each pinned nixpkgs, and the part of the `pynix` changelog
that names an option path of the workspace (009-D10, 009-D48, 009-D54, 010-D31).

Everything here is a pure function of the lock, the sources, and the compiled machine. The command runs Nix.
"""

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pynix_native import LockError, follow

DAY = 86400


# --- release names ----------------------------------------------------------------------------------
def tag_name(ref: str | None) -> str | None:
    """A release name from a git ref: `refs/tags/v0.1.1` gives `v0.1.1`. A branch gives None."""
    if ref and ref.startswith("refs/tags/"):
        return ref.removeprefix("refs/tags/")
    return ref if ref and re.fullmatch(r"v?\d+(\.\d+)+\S*", ref) else None


def source_version(path: str | None) -> str | None:
    """The `version` of `pyproject.toml` in a locked source, as `v0.1.1`."""
    if not path:
        return None
    try:
        data = tomllib.loads((Path(path) / "pyproject.toml").read_text())
    except (OSError, ValueError):
        return None
    version = data.get("project", {}).get("version")
    return f"v{version}" if version else None


def source_tag(path: str | None) -> str | None:
    """The `tag` key of `provenance/inputs.json` in a locked source: the release job records the tag there (010-D31)."""
    if not path:
        return None
    try:
        data = json.loads((Path(path) / "provenance" / "inputs.json").read_text())
    except (OSError, ValueError):
        return None
    tag = data.get("tag") if isinstance(data, dict) else None
    return tag if isinstance(tag, str) and tag else None


def read_lock(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def locked_node(lock: Mapping[str, Any], *names: str) -> Mapping[str, Any]:
    """The lock node that the path `names` reaches from the root (`("basix", "pynix")`), or an empty node."""
    try:
        node = follow(lock, list(names))
    except (LockError, KeyError, TypeError):
        return {}
    found = lock.get("nodes", {}).get(node, {})
    return found if isinstance(found, dict) else {}


@dataclass(frozen=True)
class Release:
    """What the lock says about one input: its release name, and the hash that tells two releases apart."""

    label: str
    nar_hash: str | None
    rev: str | None

    def named(self, other: "Release") -> str:
        """The label; with the short revision when the other release has the same label."""
        if self.label == other.label and self.rev:
            return f"{self.label} ({self.rev[:7]})"
        return self.label


def read_release(lock: Mapping[str, Any], name: str, source: str | None) -> Release:
    """The release of the input `name`.

    For Basix, the name comes from the tag that its source records, else the version of its source. For every input,
    the ref that the lock resolved for the commit comes next, then the short revision (010-D31). The ref that the stub
    requested (`original`) names no commit: a stub can follow a tag or a branch that moved since the lock. It never
    gives a name, so the old side of an update cannot take the name of the new release.
    """
    node = locked_node(lock, name)
    locked = node.get("locked", {})
    rev = locked.get("rev")
    own = (source_tag(source) or source_version(source)) if name == "basix" else None
    label = own or tag_name(locked.get("ref")) or (rev[:7] if rev else None)
    return Release(label or "unknown", locked.get("narHash"), rev)


def source_expr(lock: Mapping[str, Any], *names: str) -> str | None:
    """The Nix expression that gives the store path of the source that the lock holds for the input at `names`.

    It fetches the node by its `locked` attributes, so it names the commit of the lock. It never follows the `url` of
    the stub. A stub that changed (for example from `release-v2` to `release`) would make `nix flake archive` lock the
    input again in memory, and the archive would give the new source in place of the old one (011 bug 1).
    None when the lock holds no such node.
    """
    locked = locked_node(lock, *names).get("locked")
    if not isinstance(locked, dict) or not locked:
        return None
    return f"(builtins.fetchTree (builtins.fromJSON {json.dumps(json.dumps(locked))})).outPath"


def display_name(name: str) -> str:
    return "Basix" if name == "basix" else name


# --- the age of each pinned nixpkgs -----------------------------------------------------------------
def nixpkgs_ages(lock: Mapping[str, Any], now: float) -> list[str]:
    """One line for each pinned nixpkgs: `nixpkgs e7439b6b, 13 days old`. No age limit (009-D54)."""
    refs = dict(lock.get("nodes", {}).get(lock.get("root", "root"), {}).get("inputs", {}))
    refs.setdefault("nixpkgs", ["basix", "nixpkgs"])  # a stub with no `Input` leaves the pin inside the `basix` node
    lines: list[str] = []
    for name in sorted(refs):
        if name != "nixpkgs" and not name.startswith("nixpkgs-"):
            continue
        try:
            locked = lock["nodes"][follow(lock, refs[name])].get("locked", {})
        except (LockError, KeyError, TypeError):
            continue
        rev, stamp = locked.get("rev"), locked.get("lastModified")
        if not rev:
            continue
        if isinstance(stamp, int):
            days = max(0, int((now - stamp) // DAY))
            age = f"{days} day{'s' if days != 1 else ''} old"
        else:
            age = "age unknown"
        lines.append(f"{name} {rev[:8]}, {age}")
    return lines


# --- the changelog of pynix -------------------------------------------------------------------------
SECTION = re.compile(r"^## (?P<tag>\S+)\s*$", re.MULTILINE)
OPTION_PATH = re.compile(r"[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+")


def changelog_sections(text: str, old_tag: str | None) -> list[tuple[str, list[str]]]:
    """The sections of `CHANGELOG.md` that are newer than `old_tag`, newest first, each as (tag, lines of text).

    The file lists the newest tag first. With no known old tag (or an old tag that the file lacks), only the newest
    section counts.
    """
    marks = list(SECTION.finditer(text))
    sections: list[tuple[str, list[str]]] = []
    for position, mark in enumerate(marks):
        if mark["tag"] == old_tag:
            return sections
        end = marks[position + 1].start() if position + 1 < len(marks) else len(text)
        body = [line for line in text[mark.end() : end].splitlines() if line.strip() and not line.startswith("```")]
        sections.append((mark["tag"], body))
    return sections[:1]


def option_paths(compiled: Mapping[str, Any]) -> dict[str, list[str]]:
    """The option paths that the workspace sets, for each Python field or capability name.

    The key `provenance` of `system.json` gives them by field and capability. Without it, the fragments give them by
    the label of the line that made each (`system.py:3 (Machine)` gives `Machine`).
    """
    provenance = compiled.get("provenance")
    if isinstance(provenance, dict) and provenance:
        return {str(name): [str(path) for path in paths] for name, paths in provenance.items()}
    found: dict[str, list[str]] = {}
    for fragment in compiled.get("fragments", []):
        label = re.search(r"\((\w+)", str(fragment.get("source", "")))
        paths = flatten(list(fragment.get("path", [])), fragment.get("value"))
        found.setdefault(label[1] if label else "system.py", []).extend(paths)
    return found


def flatten(path: list[str], value: Any) -> list[str]:
    """The dotted paths of the leaves of a fragment value. A list, a scalar, or an empty set is a leaf."""
    if isinstance(value, dict) and value:
        return [leaf for key, item in value.items() for leaf in flatten([*path, str(key)], item)]
    return [".".join(path)] if path else []


def touches(token: str, path: str) -> bool:
    """A name in the changelog covers an option path when one is the same as, or a parent of, the other."""
    return path == token or path.startswith(token + ".") or token.startswith(path + ".")


def relevant_lines(lines: list[str], paths: Mapping[str, list[str]]) -> list[str]:
    """The lines of the changelog that name an option path of the workspace, with the names that set it."""
    out: list[str] = []
    for line in lines:
        tokens = OPTION_PATH.findall(line)
        names = [name for name, owned in paths.items() if any(touches(t, p) for t in tokens for p in owned)]
        if names:
            out.append(f"{line.strip()}  [{', '.join(names)}]")
    return out
