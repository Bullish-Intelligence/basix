"""`basix undo` (L25): a dirty tree goes back to the last save; a clean tree goes back one save, and it commits."""

from pathlib import Path

import pytest
from test_cmd_support import Setup, initialized

from basix import commands


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return initialized(tmp_path)


def apply(setup: Setup) -> int:
    return commands.apply(setup.ctx)


def edit(setup: Setup, text: str) -> None:
    (setup.ws / "system.py").write_text(text)


def test_undo_of_a_dirty_tree_drops_the_edit_and_commits_nothing(setup: Setup) -> None:
    assert apply(setup) == 0
    saved = (setup.ws / "system.py").read_text()
    edit(setup, "machine = 'edited'\n")
    setup.lines.clear()
    assert commands.undo(setup.ctx) == 0
    assert (setup.ws / "system.py").read_text() == saved
    assert setup.commits() == ["apply: system.json", "install"]
    assert setup.lines[0] == "undo: dropped the edit that basix did not apply (a copy stays in the workspace history)"
    assert setup.git("status", "--porcelain") == ""


def test_undo_of_a_dirty_tree_keeps_a_copy_of_the_edit(setup: Setup) -> None:
    assert apply(setup) == 0
    edit(setup, "machine = 'edited'\n")
    assert commands.undo(setup.ctx) == 0
    dropped = setup.git("rev-parse", "refs/basix/dropped").strip()
    assert "machine = 'edited'" in setup.git("show", f"{dropped}:system.py")


def test_undo_of_a_clean_tree_restores_the_files_of_the_save_before(setup: Setup) -> None:
    assert apply(setup) == 0
    edit(setup, "machine = 'second'\n")
    setup.world.next_system = "sys-2"
    assert apply(setup) == 0
    first = setup.git("rev-parse", "--short", "HEAD~1").strip()
    setup.lines.clear()
    setup.world.next_system = "sys-1"
    assert commands.undo(setup.ctx) == 0, setup.lines
    assert "machine = 'second'" not in (setup.ws / "system.py").read_text()
    assert setup.commits()[0] == f"undo to {first}"
    assert setup.lines[0] == "undo: restoring the files of the save before the last one"
    assert setup.lines[-1] == "saved: the files of the earlier save are back"
    assert setup.world.running() == str(setup.world.tmp / "store" / "sys-1")
    assert setup.world.boot_default() == str(setup.world.tmp / "store" / "sys-1")


def test_a_second_undo_goes_back_again(setup: Setup) -> None:
    assert apply(setup) == 0
    edit(setup, "machine = 'second'\n")
    setup.world.next_system = "sys-2"
    assert apply(setup) == 0
    setup.world.next_system = "sys-1"
    assert commands.undo(setup.ctx) == 0
    setup.world.next_system = "sys-2"
    assert commands.undo(setup.ctx) == 0
    assert "machine = 'second'" in (setup.ws / "system.py").read_text()


def test_undo_with_one_save_only_refuses(setup: Setup) -> None:
    assert commands.undo(setup.ctx) == 1
    assert setup.lines == ["There is only one save. There is nothing to undo."]
    assert setup.trace() == []
    assert setup.commits() == ["install"]


def test_undo_applies_the_restored_files_with_the_same_rules(setup: Setup) -> None:
    assert apply(setup) == 0
    edit(setup, "machine = 'edited'\n")
    setup.world.install("sys-9")  # a later system runs
    setup.ctx.has_terminal = False
    setup.world.ticket = False
    assert commands.undo(setup.ctx) == 2  # the switch needs a password and there is no terminal
    assert "there is no terminal" in setup.lines[-1]
