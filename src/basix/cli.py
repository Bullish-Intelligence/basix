"""The Basix command line: `install`, `apply`, `undo`, `update`, and `explain`."""

from pathlib import Path
from typing import Annotated

import typer

from basix import __version__, commands
from basix.install import install as install_basix

app = typer.Typer(
    help="basix applies one typed system.py as a NixOS machine.",
    no_args_is_help=True,
    add_completion=False,
)

Folder = Annotated[
    Path | None,
    typer.Option("--dir", help="The workspace folder (default: the current one, else $BASIX_WORKSPACE, else ~/basix)."),
]
Verbose = Annotated[bool, typer.Option("--verbose", help="Show every command and its output.")]


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"basix {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool, typer.Option("--version", callback=_show_version, is_eager=True, help="Print the version and exit.")
    ] = False,
) -> None:
    """basix applies one typed system.py as a NixOS machine."""


def _exit(code: int) -> None:
    raise typer.Exit(code)


@app.command("apply")
def apply_command(directory: Folder = None, verbose: Verbose = False) -> None:
    """Compile system.py, build the system, switch to it, check its services, then save the files."""
    _exit(commands.apply(commands.real_context(), directory, verbose))


@app.command("undo")
def undo_command(directory: Folder = None, verbose: Verbose = False) -> None:
    """Drop an edit that basix did not apply; else go back to the save before the last one. Then apply."""
    _exit(commands.undo(commands.real_context(), directory, verbose))


@app.command("update")
def update_command(
    name: Annotated[str, typer.Argument(help="The input to move (default: basix).")] = "basix",
    directory: Folder = None,
    verbose: Verbose = False,
) -> None:
    """Move the lock of Basix, or of one input of system.py. Run apply to use the new release."""
    _exit(commands.update(commands.real_context(), name, directory, verbose))


@app.command("explain")
def explain_command(
    name: Annotated[str, typer.Argument(help="A field of Machine, a capability, or an option path.")],
    directory: Folder = None,
) -> None:
    """Print the value of a field, a capability, or an option, and the lines of system.py that set it."""
    _exit(commands.explain(commands.real_context(), name, directory))


@app.command("install")
def install_command() -> None:
    """Install Basix on this computer."""
    _exit(install_basix())
