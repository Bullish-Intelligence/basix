import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "workdir.py"
SPEC = importlib.util.spec_from_file_location("workdir", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
workdir = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = workdir
SPEC.loader.exec_module(workdir)


def test_the_folder_is_under_the_cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASIX_WORK_BASE", raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    assert workdir.default_work("v2") == tmp_path / "cache" / "basix" / "v2"
    assert workdir.default_work("a/b") == tmp_path / "cache" / "basix" / "a" / "b"


def test_the_base_variable_moves_every_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("BASIX_WORK_BASE", str(tmp_path / "big"))
    assert workdir.default_work("v2/apply") == tmp_path / "big" / "v2" / "apply"


def test_cache_default_is_under_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BASIX_WORK_BASE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert workdir.default_work("v2") == tmp_path / ".cache" / "basix" / "v2"


def test_the_function_makes_no_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    workdir.default_work("v2")
    assert list(tmp_path.iterdir()) == []
