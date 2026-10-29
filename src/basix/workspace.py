"""The workspace of Basix: where it is, and the plain names that the learner sees."""

from collections.abc import Mapping
from pathlib import Path

from pynix_engine import Fail

from basix.layer import DEFAULT_FOLDER, WORDS, WORKSPACE_VARIABLE


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


def expand_home(text: str, home: Path) -> Path:
    """`~` and `~/x` mean the home folder of this run. Any other text is a path as written."""
    if text == "~":
        return home
    return home / text[2:] if text.startswith("~/") else Path(text)


def environment_workspace(env: Mapping[str, str], cwd: Path, home: Path) -> Path | None:
    """The folder that `BASIX_WORKSPACE` names, or None when the variable is unset or empty.

    A folder with no `system.py` is an error: the variable was set on purpose, so Basix does not fall back.
    """
    value = env.get(WORKSPACE_VARIABLE, "")
    if not value:
        return None
    folder = cwd / expand_home(value, home)
    if not (folder / "system.py").is_file():
        raise Fail(WORDS.bad_workspace_variable.format(variable=WORKSPACE_VARIABLE, value=value, entry="system.py"))
    return folder.resolve()


def find_workspace(explicit: Path | None, cwd: Path, home: Path, env: Mapping[str, str] | None = None) -> Path:
    """The Basix workspace for this run.

    `--dir` wins. Else the repository that holds `cwd` counts when it has a `system.py`. A minix workspace gets its
    own line (009-D15). Else the folder that `BASIX_WORKSPACE` names. Else `~/basix`.
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
    if (named := environment_workspace(env or {}, cwd, home)) is not None:
        return named
    default = home / "basix"
    if (default / ".git").exists():
        return default
    raise Fail(
        WORDS.no_workspace.format(
            path=tilde(cwd, home), name="basix", folder=DEFAULT_FOLDER, variable=WORKSPACE_VARIABLE
        )
    )


def environment_home(env: Mapping[str, str]) -> Path:
    return Path(env["HOME"]) if env.get("HOME") else Path.home()
