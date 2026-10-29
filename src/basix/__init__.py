"""basix: the system layer of Basix v2. One typed system.py, applied as a NixOS machine."""

from importlib.metadata import version


def __getattr__(name: str) -> str:
    """`__version__` is the version of the installed package, which comes from `pyproject.toml`.

    No second copy can drift. The lookup runs on first use, so a script that imports `basix` from a source folder
    (no installed package) still works.
    """
    if name == "__version__":
        return version("basix")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
