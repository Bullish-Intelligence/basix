"""The commands of `basix`: `apply`, `undo`, `update`, and `explain`. Each returns the exit code.

A command gets a `Context`: the runner, the clock, the output, the environment, and the files of the host. The CLI
builds the real one. The tests build one with fakes. `basix install` belongs to the install (P4), not to this file.
"""

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pynix_engine import (
    EXIT_FAILED,
    EXIT_STOPPED,
    Clock,
    Engine,
    Fail,
    Layer,
    Runner,
    Session,
    SubprocessRunner,
    SystemClock,
    steps,
)
from pynix_native import DiscoveryError, discover

from basix.compile import Compiler, InputList, ensure_ignored, make_input_packages, stub_url
from basix.explain import explain as explain_name
from basix.layer import Host, basix_layer
from basix.nix import NixRunner
from basix.release import (
    changelog_sections,
    display_name,
    locked_node,
    nixpkgs_ages,
    option_paths,
    read_lock,
    read_release,
    relevant_lines,
    source_expr,
    tag_name,
)
from basix.workspace import environment_home, find_workspace, tilde
from basix_config.stub import DEFAULT_BASIX_URL


def say_flush(text: str) -> None:
    print(text, flush=True)


@dataclass
class Context:
    """Everything that a command reaches outside itself."""

    home: Path
    cwd: Path
    env: Mapping[str, str]
    runner: Runner = field(default_factory=SubprocessRunner)
    clock: Clock = field(default_factory=SystemClock)
    say: Callable[[str], None] = say_flush
    host: Host = field(default_factory=Host)  # the files of the running system
    has_terminal: bool = True  # a person can type a password
    wall_time: Callable[[], float] = time.time  # for the age of a nixpkgs
    read_inputs: InputList | None = None  # None: read the inputs of a flake input through Nix
    explain: Callable[..., int] | None = None  # None: `basix.explain.explain`

    @property
    def nix(self) -> Runner:
        """The runner that adds the Nix features to every `nix` command (009-D29) and shortens a conflict (009-D20)."""
        return NixRunner(self.runner)


def real_context() -> Context:
    env = dict(os.environ)
    return Context(home=environment_home(env), cwd=Path.cwd(), env=env, has_terminal=sys.stdin.isatty())


# --- apply and undo ---------------------------------------------------------------------------------
def workspace_layer(ctx: Context, ws: Path) -> Layer:
    """The layer of this workspace, with the folder name in plain words."""
    return basix_layer(
        ws,
        host=ctx.host,
        has_terminal=ctx.has_terminal,
        folder=tilde(ws, ctx.home),
        compile=Compiler(ctx.host, ctx.has_terminal, ctx.read_inputs),
        preflight=ensure_ignored,  # before the stage step, which refuses the link in `.basix/`
    )


def run_engine(ctx: Context, explicit: Path | None, verbose: bool, action: str) -> int:
    try:
        ws = find_workspace(explicit, ctx.cwd, ctx.home, ctx.env)
        layer = workspace_layer(ctx, ws)
    except Fail as failure:
        ctx.say(str(failure))
        return failure.code
    engine = Engine(layer, ws, runner=ctx.nix, clock=ctx.clock, say=ctx.say, env=ctx.env, verbose=verbose)
    return engine.apply() if action == "apply" else engine.undo()


def apply(ctx: Context, directory: Path | None = None, verbose: bool = False) -> int:
    """Compile `system.py`, build the system, switch to it, check it, then save the files (CONCEPT 4.1)."""
    return run_engine(ctx, directory, verbose, "apply")


def undo(ctx: Context, directory: Path | None = None, verbose: bool = False) -> int:
    """Drop an edit that Basix did not apply; else go back to the save before the last one. Then apply (L25)."""
    return run_engine(ctx, directory, verbose, "undo")


# --- explain ----------------------------------------------------------------------------------------
def explain(ctx: Context, name: str, directory: Path | None = None) -> int:
    """Print the value of a field, a capability, or an option, and the lines that set it (009-D14)."""
    try:
        ws = find_workspace(directory, ctx.cwd, ctx.home, ctx.env)
    except Fail as failure:
        ctx.say(str(failure))
        return failure.code
    run: Callable[..., int] = ctx.explain or explain_name
    return run(ws, name, runner=ctx.nix)


# --- update -----------------------------------------------------------------------------------------
def locked_source(session: Session, lock: Mapping[str, Any], *names: str) -> str | None:
    """The store path of the source that `lock` holds for the input at `names`, or None when Nix cannot give it.

    The source is in the store when the machine has used it, so this needs no network.
    """
    expr = source_expr(lock, *names)
    if expr is None:
        return None
    result = session.run(["nix", "eval", "--json", "--expr", expr], quiet=True)
    try:
        path = json.loads(result.out[result.out.index('"') :]) if result.code == 0 else None
    except ValueError:
        return None
    return path if isinstance(path, str) else None


def print_changelog(
    ctx: Context,
    session: Session,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    pynix_source: str | None,
) -> None:
    """The part of the `pynix` changelog, between the old and the new tag, that names an option that system.py sets."""
    old_node, new_node = locked_node(before, "basix", "pynix"), locked_node(after, "basix", "pynix")
    old_tag = tag_name(old_node.get("original", {}).get("ref")) or tag_name(old_node.get("locked", {}).get("ref"))
    new_tag = tag_name(new_node.get("original", {}).get("ref")) or tag_name(new_node.get("locked", {}).get("ref"))
    if not new_node or old_node.get("locked", {}).get("rev") == new_node.get("locked", {}).get("rev"):
        return
    try:
        text = (Path(pynix_source or "") / "CHANGELOG.md").read_text()
    except OSError:
        ctx.say("pynix changed, but its changelog could not be read")
        return
    try:
        compiled = json.loads((session.ws / "system.json").read_text())
    except (OSError, ValueError):
        compiled = {}
    paths = option_paths(compiled)
    shown = False
    for tag, body in changelog_sections(text, old_tag):
        lines = relevant_lines(body, paths)
        if lines:
            ctx.say(f"{tag} changes options that system.py sets:")
            for line in lines:
                ctx.say(f"  {line}")
            shown = True
    if not shown:
        ctx.say(f"{old_tag or 'unknown'} -> {new_tag or 'unknown'}: no change to an option that system.py sets")


def move_to_channel(ctx: Context, ws: Path) -> str | None:
    """Point a stub that follows a `path:` copy at the release channel. Return the old stub text, or None if it stayed.

    The installer locks its own copy of Basix as a `path:` input, so the first `basix apply` works offline. That
    copy never moves. The first `basix update` is the point where the machine joins the release channel (011 bug 2).
    """
    url = stub_url(ws)
    if url is None or not url.startswith("path:"):
        return None
    stub = ws / "flake.nix"
    old = stub.read_text()
    stub.write_text(old.replace(f'"{url}"', f'"{DEFAULT_BASIX_URL}"', 1))
    ctx.say("This machine used the copy of Basix from the installer. It now follows the release channel.")
    return old


def update(ctx: Context, name: str = "basix", directory: Path | None = None, verbose: bool = False) -> int:
    """Move the lock of `basix` (or of the input `name`), print what changed, and make the input packages.

    It prints the old and new release names, the age of each pinned nixpkgs, and the part of the `pynix` changelog
    that names an option of this workspace (009-D10, 009-D54). It does not switch: the learner runs `apply`.
    """
    try:
        ws = find_workspace(directory, ctx.cwd, ctx.home, ctx.env)
        layer = workspace_layer(ctx, ws)
        session = Session(layer, ws, ctx.nix, ctx.clock, ctx.say, ctx.env, verbose)
    except Fail as failure:
        ctx.say(str(failure))
        return failure.code
    lock = session.try_lock()  # the lock of `apply`: one command at a time
    if lock is None:
        ctx.say(session.word("busy"))
        return EXIT_FAILED
    session.open_log()
    try:
        lock_before = read_lock(ws / "flake.lock")
        before = read_release(lock_before, name, locked_source(session, lock_before, name))
        old_stub = move_to_channel(ctx, ws) if name == "basix" else None
        result = session.run(["nix", "flake", "update", name, "--no-warn-dirty"])
        if result.code != 0:
            if old_stub is not None:
                (ws / "flake.nix").write_text(old_stub)
            ctx.say(f"The update failed. The lock did not change.\n{steps.plain(steps.trim_nix_error(result.out))}")
            return EXIT_STOPPED
        lock_after = read_lock(ws / "flake.lock")
        after = read_release(lock_after, name, locked_source(session, lock_after, name))
        label = display_name(name)
        if before.nar_hash and before.nar_hash == after.nar_hash:
            ctx.say(f"{label} is up to date ({after.label})")
        else:
            ctx.say(f"{label} {before.named(after)} -> {after.named(before)}")
        for line in nixpkgs_ages(lock_after, ctx.wall_time()):
            ctx.say(line)
        print_changelog(ctx, session, lock_before, lock_after, locked_source(session, lock_after, "basix", "pynix"))
        try:
            make_input_packages(session, discover(ws / "system.py"))
        except (DiscoveryError, OSError):
            pass  # the next apply reports a system.py that cannot be read
        except Fail as failure:
            ctx.say(str(failure))
            return failure.code
        ctx.say("next: run basix apply to use it")
        return 0
    finally:
        lock.close()
        session.close()
