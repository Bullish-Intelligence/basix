"""One folder per capability (009-D17, BSX-381, 010-D10).

`capabilities/NAME/component.py` holds the class. The package reaches it through a symlink
`src/basix_config/capabilities/NAME.py` (with `_` for `-`). The names of the classes come from Python, never from a
listing of the directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import ROOT

import basix_config

FOLDERS = ROOT / "capabilities"
LINKS = ROOT / "src" / "basix_config" / "capabilities"


def problems(folders: Path, links: Path) -> list[str]:
    """The faults of the layout: a component with no symlink, a symlink with no folder, or a link that is wrong."""
    found: list[str] = []
    names = {p.parent.name for p in folders.glob("*/component.py")}
    stems = {p.stem for p in links.glob("*.py") if p.name != "__init__.py"}
    for name in sorted(names):
        if name.replace("-", "_") not in stems:
            found.append(f"capabilities/{name}/component.py has no symlink in {links.name}/")
    for stem in sorted(stems):
        link = links / f"{stem}.py"
        if stem.replace("_", "-") not in names:
            found.append(f"the symlink {stem}.py has no folder capabilities/{stem.replace('_', '-')}/component.py")
        elif not link.is_symlink():
            found.append(f"{stem}.py must be a symlink, not a copy")
        elif os.path.isabs(os.readlink(link)):
            found.append(f"{stem}.py must be a relative link")
        elif link.resolve() != (folders / stem.replace("_", "-") / "component.py").resolve():
            found.append(f"{stem}.py points at the wrong file")
    return found


def test_the_layout_of_the_repository_is_right() -> None:
    assert problems(FOLDERS, LINKS) == []
    assert (FOLDERS / "remote-login" / "component.py").is_file()  # the check is not empty


def test_a_component_with_no_symlink_fails(tmp_path: Path) -> None:
    (tmp_path / "caps" / "fans").mkdir(parents=True)
    (tmp_path / "caps" / "fans" / "component.py").write_text("")
    (tmp_path / "links").mkdir()
    assert problems(tmp_path / "caps", tmp_path / "links") == [
        "capabilities/fans/component.py has no symlink in links/"
    ]


def test_a_symlink_with_no_folder_fails(tmp_path: Path) -> None:
    (tmp_path / "caps").mkdir()
    (tmp_path / "links").mkdir()
    (tmp_path / "links" / "fans_extra.py").symlink_to("../caps/fans-extra/component.py")
    assert problems(tmp_path / "caps", tmp_path / "links") == [
        "the symlink fans_extra.py has no folder capabilities/fans-extra/component.py"
    ]


def test_a_copy_and_a_wrong_link_fail(tmp_path: Path) -> None:
    for name in ("fans", "lamps"):
        (tmp_path / "caps" / name).mkdir(parents=True)
        (tmp_path / "caps" / name / "component.py").write_text("")
    (tmp_path / "links").mkdir()
    (tmp_path / "links" / "fans.py").write_text("")  # a copy
    (tmp_path / "links" / "lamps.py").symlink_to("../caps/fans/component.py")  # the wrong file
    assert problems(tmp_path / "caps", tmp_path / "links") == [
        "fans.py must be a symlink, not a copy",
        "lamps.py points at the wrong file",
    ]


def components() -> dict[str, Path]:
    return {p.parent.name: p for p in sorted(FOLDERS.glob("*/component.py"))}


def test_each_capability_has_its_files() -> None:
    for name in components():
        for needed in ("lesson.md",):
            assert (FOLDERS / name / needed).is_file(), f"capabilities/{name}/{needed} is missing"


def test_lesson_fits_one_screen() -> None:
    """BSX-324: a lesson is a short overview, one screen."""
    for name in components():
        lines = (FOLDERS / name / "lesson.md").read_text().splitlines()
        assert len(lines) <= 40, f"capabilities/{name}/lesson.md has {len(lines)} lines"


def test_a_learner_class_per_capability() -> None:
    """The learner name of a capability is a Python class, not the name of the folder."""
    assert basix_config.RemoteLogin.__module__ == "basix_config.capabilities.remote_login"


def test_the_nix_module_named_by_a_component_exists() -> None:
    """`RemoteLogin` names `capabilities/remote-login/module.nix`: the file must exist in the repository."""
    compiled = basix_config.Machine(name="a", user="b", disk=None, capabilities=[basix_config.RemoteLogin()]).compile()
    modules = compiled["modules"]
    assert modules and all((ROOT / m["file"]).is_file() for m in modules if m.get("root") != "workspace")
