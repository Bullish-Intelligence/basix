"""The release script refuses a wrong call before it changes anything. These tests run no Nix."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "release.sh"
# The Testee checkout has no persona, so the tests that run Git set their own.
IDENTITY = {
    "GIT_AUTHOR_NAME": "scratch",
    "GIT_AUTHOR_EMAIL": "scratch@example.invalid",
    "GIT_COMMITTER_NAME": "scratch",
    "GIT_COMMITTER_EMAIL": "scratch@example.invalid",
}


def run(*args: str, script: Path = SCRIPT, log: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **IDENTITY}
    if log is not None:
        env["BASIX_RELEASE_LOG"] = str(log)
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, check=False, env=env)


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env={**os.environ, **IDENTITY}
    )
    return done.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small repository with a copy of the script and a `pyproject.toml` at version 0.1.0."""
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, root / "scripts" / "release.sh")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n\n[tool.other]\nversion = "9.9.9"\n'
    )
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "First commit")
    return root


@pytest.mark.parametrize(
    "tag",
    [
        "pynix-v0.1.0",
        "0.1.0",
        "v0.1",
        "v0.1.0-beta.1",
        "v0.1.0-manifests",
        "manifests/v0.1.0",
        "v1.0.0-rc.",
        "v1.0.0-rc.1.2",
    ],
)
def test_wrong_tag_name_exits_2(tag: str) -> None:
    done = run(tag)
    assert done.returncode == 2
    assert "vX.Y.Z" in done.stderr


def test_no_tag_prints_the_usage_line_and_exits_2() -> None:
    for args in ([], ["--skip-status"]):
        done = run(*args)
        assert done.returncode == 2
        assert done.stderr.startswith("# Usage: scripts/release.sh [--skip-status] TAG")


def test_unknown_option_exits_2() -> None:
    done = run("--force", "v0.1.0")
    assert done.returncode == 2
    assert "unknown option" in done.stderr


def test_two_tags_exit_2() -> None:
    done = run("v0.1.0", "v0.1.1")
    assert done.returncode == 2
    assert "one TAG only" in done.stderr


@pytest.mark.parametrize("tag", ["v0.2.0", "v0.2.0-rc.1", "v1.0.0"])
def test_tag_with_another_version_exits_2(repo: Path, tag: str, tmp_path: Path) -> None:
    done = run("--skip-status", tag, script=repo / "scripts" / "release.sh", log=tmp_path / "log")
    assert done.returncode == 2
    assert "pyproject.toml has version 0.1.0" in done.stderr
    assert git(repo, "tag", "-l") == ""


def test_the_version_comes_from_the_project_table(repo: Path, tmp_path: Path) -> None:
    # `[tool.other]` holds version 9.9.9. The tag for it must fail on the version of `[project]`, not pass.
    done = run("--skip-status", "v9.9.9", script=repo / "scripts" / "release.sh", log=tmp_path / "log")
    assert done.returncode == 2
    assert "pyproject.toml has version 0.1.0" in done.stderr


def test_existing_tag_exits_2(repo: Path, tmp_path: Path) -> None:
    git(repo, "tag", "-a", "-m", "old", "v0.1.0-rc.1")
    done = run("--skip-status", "v0.1.0-rc.1", script=repo / "scripts" / "release.sh", log=tmp_path / "log")
    assert done.returncode == 2
    assert "v0.1.0-rc.1 exists" in done.stderr


def test_dirty_tree_exits_2(repo: Path, tmp_path: Path) -> None:
    (repo / "extra.txt").write_text("not committed\n")
    done = run("--skip-status", "v0.1.0-rc.1", script=repo / "scripts" / "release.sh", log=tmp_path / "log")
    assert done.returncode == 2
    assert "not clean" in done.stderr
    assert git(repo, "tag", "-l") == ""


def test_the_push_line_branch_defaults_to_release() -> None:
    # The owner decision 010-D54: `release` follows v2. `BASIX_RELEASE_BRANCH` overrides it.
    text = SCRIPT.read_text()
    assert "branch=${BASIX_RELEASE_BRANCH:-release}\n" in text
    assert "'$tag^{commit}:refs/heads/$branch'" in text


def test_the_alias_branch_is_off_by_default_and_the_push_goes_to_origin() -> None:
    # 012-D5: the new repository has no alias branch. `BASIX_RELEASE_ALIAS` names one. The job prints `origin` pushes.
    text = SCRIPT.read_text()
    assert "alias_branch=${BASIX_RELEASE_ALIAS-}\n" in text
    assert "git push origin $tag\n" in text
    assert "collection" not in text
    assert "'$tag^{commit}:refs/heads/$alias_branch'" in text
    assert "$alias_line" in text
