"""The documentation check skips the records and fails on a link into them (BSX-418)."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_docs.py"


def run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), str(root)], capture_output=True, text=True, check=False)


def test_a_tree_without_records_passes(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Plain text: .loci/CURRENT.md\n")
    assert run(tmp_path).returncode == 0


def test_a_broken_link_in_the_records_is_skipped(tmp_path: Path) -> None:
    (tmp_path / ".loci").mkdir()
    (tmp_path / ".loci" / "note.md").write_text("[gone](missing.md)\n")
    assert run(tmp_path).returncode == 0


def test_a_link_into_the_records_fails(tmp_path: Path) -> None:
    (tmp_path / ".loci").mkdir()
    (tmp_path / ".loci" / "CURRENT.md").write_text("x\n")
    (tmp_path / "README.md").write_text("[now](.loci/CURRENT.md)\n")
    result = run(tmp_path)
    assert result.returncode == 1
    assert "link into the records" in result.stderr
