"""The workspace of Basix: where it is, and the plain names that the learner sees."""

from collections.abc import Mapping
from pathlib import Path

from pynix_engine import Fail

from basix.layer import DEFAULT_FOLDER, WORDS


def tilde(path: Path, home: Path) -> str:
    """A path for the learner: `~/basix` instead of the full path, when it lies under the home folder."""
    try:
        relative = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if str(relative) == "." else f"~/{relative}"


def other_layer(folder: Path, home: Path) -> Fail | None:
    """The line for a folder that holds `home.py` and no `system.py`: it belongs to minix (009-D15)."""
    if (folder / "home.py").is_file() and not (folder / "system.py").is_file():
        return Fail(WORDS.minix_workspace.format(path=tilde(folder, home), name="basix", folder=DEFAULT_FOLDER))
    return None


def find_workspace(explicit: Path | None, cwd: Path, home: Path) -> Path:
    """The Basix workspace for this run.

    `--dir` wins. Else the repository that holds `cwd` counts when it has a `system.py`. A minix workspace gets its
    own line (009-D15). Else `~/basix`.
    """
    if explicit is not None:
        folder = explicit.resolve()
        if failure := other_layer(folder, home):
            raise failure
        return folder
    for folder in (cwd, *cwd.parents):
        if not (folder / ".git").exists():
            continue
        if (folder / "system.py").is_file():
            return folder
        if failure := other_layer(folder, home):
            raise failure
        break
    default = home / "basix"
    if (default / ".git").exists():
        return default
    raise Fail(WORDS.not_a_workspace.format(path=tilde(cwd, home), name="basix", folder=DEFAULT_FOLDER))


def environment_home(env: Mapping[str, str]) -> Path:
    return Path(env["HOME"]) if env.get("HOME") else Path.home()
