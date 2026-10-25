"""Shared helpers: compile an entry in a scratch workspace.

`run_compile` makes the child runs inside the test process (fast). `run_command` starts the real command.

Tests that run real git set their own identity (the Testee checkout has no persona). The compile tests run no git.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

#: The libraries stay loaded between two in-process runs. A generated input package or name list does not.
KEEP_MODULES = ("basix_config", "nixdantic", "pydantic", "pynix_native", "pynix_os", "pynix_pkgs")
ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Compiled:
    """The result of one compile: the exit code, the text of stderr, and the folder of the workspace."""

    code: int
    err: str
    folder: Path

    @property
    def lines(self) -> list[str]:
        return self.err.splitlines()

    @property
    def data(self) -> dict:
        return json.loads((self.folder / "system.json").read_text())


def run_command(folder: Path, name: str = "system.py") -> Compiled:
    """Run `python -m basix_config.compile NAME system.json` in `folder`, as the command does (a real process)."""
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "PYTHONPATH", "VIRTUAL_ENV", "HOME"}}
    done = subprocess.run(
        [sys.executable, "-P", "-m", "basix_config.compile", name, "system.json"],
        cwd=folder,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return Compiled(done.returncode, done.stderr, folder)


def run_compile(folder: Path, name: str = "system.py") -> Compiled:
    """The same compile, with the two child runs made inside this process (about 20 times faster).

    The parent steps are the real ones (`discover`, the pair check, the compare, the merge). Only the process
    boundary of `_run_child` is replaced: each run keeps the state of `sys.path`, `sys.modules`, the cache warnings,
    and the `NixpkgsPin` registry apart. The tests of `run_command` cover the boundary itself.
    """
    from pynix_native import reset_nixpkgs

    from basix_config import cache
    from basix_config import compile as compiler

    def child(source: Path, final: Path, tmp: Path, number: int) -> tuple[int, str, bytes]:
        out = tmp / f"run{number}.json"
        saved_path, saved_modules, saved_cwd = list(sys.path), set(sys.modules), os.getcwd()
        buffer = io.StringIO()
        os.chdir(source.resolve().parent)
        cache.WARNINGS.clear()  # a test that built a `Cache` in this process left its warnings here
        try:
            with contextlib.redirect_stderr(buffer):
                code = compiler.run_entry(source, out, final)
        finally:
            os.chdir(saved_cwd)
            sys.path[:] = saved_path
            for module in set(sys.modules) - saved_modules:
                if not module.startswith(KEEP_MODULES):
                    del sys.modules[module]
            cache.WARNINGS.clear()
            reset_nixpkgs()
        return code, buffer.getvalue(), out.read_bytes() if out.exists() else b""

    buffer = io.StringIO()
    with mock.patch.object(compiler, "_run_child", child), contextlib.redirect_stderr(buffer):
        code = compiler.compile_entry(folder / name, folder / "system.json")
    return Compiled(code, buffer.getvalue(), folder)


@pytest.fixture
def workspace(tmp_path: Path):
    """A function that writes `system.py` (and other files) into a fresh folder and compiles it."""

    def make(text: str, files: dict[str, str] | None = None, name: str = "system.py") -> Compiled:
        (tmp_path / name).write_text(text)
        for relative, content in (files or {}).items():
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return run_compile(tmp_path, name)

    return make


@pytest.fixture
def command(tmp_path: Path):
    """Like `workspace`, with a real process: `python -m basix_config.compile`, as `basix apply` runs it."""

    def make(text: str, files: dict[str, str] | None = None) -> Compiled:
        (tmp_path / "system.py").write_text(text)
        for relative, content in (files or {}).items():
            (tmp_path / relative).write_text(content)
        return run_command(tmp_path)

    return make


MACHINE = """\
from basix_config import *

machine = Machine(
    name="pybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-basix-disk",
    {extra}
)
"""


def machine_text(extra: str = "") -> str:
    """The text of a `system.py` with the given extra fields."""
    return MACHINE.format(extra=extra)
