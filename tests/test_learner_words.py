"""The learner word is "capability", never "component" (BSX-386).

"Component" stays the builder word. This check reads the text that a learner can see: the strings of the Python
source (docstrings, error lines, and messages), the written entry and overview in `template/`, and the `lesson.md`
files. It fails on the word "component" there. The files in `BUILDER` are the builder files: they may use the word.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from conftest import ROOT

#: The builder files, as paths from the root of the repository, with the reason. Add a path only for builder text.
BUILDER = {
    "src/basix_config/component.py": "the base classes Component, Profile, and Capability",
    "src/basix_config/capabilities/__init__.py": "the layout of a capability folder (component.py and its link)",
}
#: Names of code that carry the word: a file, a module, and an error class. They are not text for the learner.
CODE_NAMES = re.compile(r"component\.py|basix_config\.component|ComponentError")
WORD = re.compile(r"component", re.IGNORECASE)


def strings(path: Path) -> list[tuple[int, str]]:
    """Every string of a Python file, with its line. Comments are not strings, so they do not count."""
    tree = ast.parse(path.read_text())
    return [(n.lineno, n.value) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def learner_text(root: Path) -> dict[Path, list[tuple[int, str]]]:
    """The learner-visible text of a repository: a list of (line, text) for each file."""
    found: dict[Path, list[tuple[int, str]]] = {}
    python = [*root.glob("src/**/*.py"), *root.glob("capabilities/*/component.py")]
    for path in python:
        if path.is_symlink():  # the link reaches a file that the glob of `capabilities/` already holds
            continue
        found[path] = strings(path)
    plain = [*root.glob("template/**/*"), *root.glob("capabilities/*/lesson.md"), *root.glob("profiles/*/lesson.md")]
    for path in plain:
        if path.is_file():
            found[path] = list(enumerate(path.read_text().splitlines(), 1))
    return found


def offenders(root: Path, builder: dict[str, str] = BUILDER) -> list[str]:
    """The lines with the word "component" in learner text, as `path:line: text`."""
    found: list[str] = []
    for path, items in sorted(learner_text(root).items()):
        relative = path.relative_to(root).as_posix()
        if relative in builder:
            continue
        for line, text in items:
            for part in text.splitlines():
                if WORD.search(CODE_NAMES.sub("", part)):
                    found.append(f"{relative}:{line}: {part.strip()}")
    return found


def test_learner_text_has_no_component() -> None:
    assert offenders(ROOT) == []


def test_the_check_reads_the_files_it_should() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in learner_text(ROOT)}
    assert "src/basix_config/__init__.py" in names
    assert "capabilities/remote-login/component.py" in names
    assert "capabilities/remote-login/lesson.md" in names
    assert set(BUILDER) <= names  # a builder path that names no file is a stale entry


def test_the_check_finds_the_word(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text('"""A Component of the machine."""\nx = "see component.py"\n')
    (tmp_path / "src" / "b.py").write_text('def f():\n    """Each component."""\n')
    (tmp_path / "capabilities" / "fans").mkdir(parents=True)
    (tmp_path / "capabilities" / "fans" / "lesson.md").write_text("A component.\n")
    assert offenders(tmp_path) == [
        "capabilities/fans/lesson.md:1: A component.",
        "src/a.py:1: A Component of the machine.",
        "src/b.py:2: Each component.",
    ]
    assert offenders(tmp_path, {"src/a.py": "builder", "src/b.py": "builder"}) == [
        "capabilities/fans/lesson.md:1: A component."
    ]
