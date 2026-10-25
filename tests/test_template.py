"""The written entry `template/system.py` and the overview `template/README.md` (009-D23, 009-D34, BSX-324, BSX-388)."""

from __future__ import annotations

import ast
from pathlib import Path

from conftest import ROOT, run_compile

from basix_config import Machine

TEMPLATE = ROOT / "template"
#: The fields that the install answers fill (CONCEPT 3.1).
ANSWERS = ("name", "user", "disk", "state_version", "serial_console")
#: The advanced fields that the written entry leaves out (009-D34, L133).
LEFT_OUT = ("options", "native_modules", "caches", "inputs", "values")


def test_the_template_compiles(tmp_path: Path) -> None:
    (tmp_path / "system.py").write_text((TEMPLATE / "system.py").read_text())
    compiled = run_compile(tmp_path)
    assert compiled.code == 0, compiled.err


def test_one_import_line_with_every_learner_name() -> None:
    text = (TEMPLATE / "system.py").read_text()
    imports = [n for n in ast.parse(text).body if isinstance(n, ast.Import | ast.ImportFrom)]
    assert len(imports) == 1 and text.startswith("from basix_config import *\n")


def test_every_field_is_there_except_the_advanced_ones() -> None:
    call = next(n for n in ast.walk(ast.parse((TEMPLATE / "system.py").read_text())) if isinstance(n, ast.Call))
    written = [k.arg for k in call.keywords]
    expected = [name for name in Machine.model_fields if name not in LEFT_OUT]
    assert sorted(written, key=str) == sorted(expected, key=str)
    for name in ANSWERS:
        assert name in written


def test_the_other_fields_are_empty_with_an_example() -> None:
    lines = {
        line.split("=")[0].strip(): line for line in (TEMPLATE / "system.py").read_text().splitlines() if "=" in line
    }
    for name in ("capabilities", "programs", "trusted_interfaces", "user_ports"):
        assert lines[name].split("=", 1)[1].startswith("[],  # "), lines[name]
    assert lines["serial_console"].split("=", 1)[1].startswith("None,  # ")


def test_the_overview_is_one_screen_and_names_home_first() -> None:
    text = (TEMPLATE / "README.md").read_text()
    assert len(text.splitlines()) <= 45
    assert text.index("`home.py`") < text.index("`system.py`")
    assert "options=[...]" in text and "native_modules=" in text  # the hatch order, in one place
    for word in ("vendomat", "basix.yaml", "devenv"):
        assert word not in text.lower()


def test_the_v1_files_are_gone() -> None:
    names = {p.name for p in TEMPLATE.iterdir()}
    assert names == {".gitignore", "README.md", "system.py"}
    assert ".basix/" in (TEMPLATE / ".gitignore").read_text()
