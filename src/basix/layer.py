"""The Basix layer: what `basix` passes to the engine of `pynix_engine` (009-D26, CONCEPT section 4).

The engine names no layer. This file holds every fact that is specific to Basix: the file names, the build attribute,
the switch plan (L123), the health query of the system units, and the plain words. Every command that changes the
machine runs through `sudo`, in one prompt.

The plan of a switch (L123):

1. `systemctl reset-failed`, so an old failed unit cannot fail this apply;
2. `switch-to-configuration test`: the new system runs, and the boot default does not change;
3. the health check (8 s, 009-D4; a failed query is a failed check, 010-D9);
4. only then `nix-env --set` and `switch-to-configuration boot`: the new system becomes the boot default.

A failed check switches back to the running system with `test`. The boot default never changed.
"""

import json
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pynix_engine import EXIT_STOPPED, Command, Fail, Layer, Plan, Session, Words

from basix.units import Cycle, ordering_cycles, summary

# 009-D29: Basix passes these two features to every Nix call, so it works on a host that has not turned them on.
NIX_FEATURES = ("--extra-experimental-features", "nix-command flakes")

# The setuid copy of sudo. The `sudo` first on `PATH` may be a plain copy that is not setuid (host fact).
SUDO = "/run/wrappers/bin/sudo"

# The health query: every system unit on one line each, `UNIT LOAD ACTIVE SUB`.
HEALTH_COMMAND: Command = ("systemctl", "list-units", "--all", "--no-legend", "--plain", "--no-pager")
UNIT_SUFFIXES = (".service", ".mount", ".socket", ".timer", ".path")
RESET_FAILED: Command = (SUDO, "systemctl", "reset-failed")

# Commit identity when git knows no author. Without it, a new account would stop at the first apply.
FALLBACK_IDENTITY = ("basix", "basix@localhost")

DEFAULT_FOLDER = "~/basix"

# The machine name comes from `system.json`, which the compile step writes after the layer exists. The layer holds a
# placeholder, and `build_command` puts the real attribute in its place.
BUILD_PLACEHOLDER = ".#nixosConfigurations"
COMPILER_ROOT = "compiler"  # `.git/basix/compiler`: the garbage-collector root of the compiler (BSX-364)


@dataclass(frozen=True)
class Host:
    """The files of the running system that Basix reads. A test points them at a scratch folder."""

    current: Path = Path("/run/current-system")
    profile: Path = Path("/nix/var/nix/profiles/system")
    nix_conf: Path = Path("/run/current-system/etc/nix/nix.conf")


DEFAULT_HOST = Host()


@dataclass(frozen=True)
class BasixWords(Words):
    """The plain lines of the engine (009-D15) and the lines that only Basix prints."""

    lock_failed: str = "The inputs could not be locked. Nothing was built, switched, or saved.\n{detail}"
    no_entry: str = "Found no {entry} in {folder}. Run the Basix install first."
    minix_workspace: str = (
        "{path} is a minix workspace. {name} does not change it.\n"
        "To change your own home, run: minix apply\n"
        "To change the machine, go to {folder} and run: {name} apply"
    )
    not_a_stub: str = (
        "{path} is not the file that Basix writes (its first line must be {header}). "
        "Move it away, then run {name} apply."
    )
    scratch_ignored: str = "added {names} to .gitignore"
    inputs_failed: str = "warning: the flake inputs could not be rooted: {detail}"
    inputs_build_failed: str = "The input packages could not be built. Nothing was built, switched, or saved.\n{detail}"
    sudo_notice: str = "Basix needs your password to change the machine"
    sudo_no_terminal: str = (
        "Basix needs your password to change the machine, and there is no terminal. "
        "Run {name} in a terminal window. Nothing was switched or saved."
    )
    sudo_refused: str = "sudo did not accept the password. Nothing was switched or saved. The system did not change."
    caches_building: str = "building the system with the new caches"
    caches_failed: str = (
        "The build with the new caches failed. Nothing was switched or saved. The system did not change.\n"
        "{detail}\n(full log: {log})"
    )
    conflict: str = "{places} set different values ({option}). Remove one."
    conflict_one: str = "{places} gives two different values ({option}). Remove one."
    greeter_next_start: str = "The login screen changes at the next start. Basix does not restart it now."
    cycle_warning: str = (
        "WARNING: the new system has a systemd ordering cycle ({count} found).\n"
        "At boot, systemd deletes jobs at random to break a cycle. Services such as SSH can then fail on some boots.\n"
        "{cycles}\n"
        "Basix continues, because a unit of an upstream package can cause a false report. "
        "Fix the order of these units before you restart the machine."
    )


WORDS = BasixWords()


def stopped(message: str) -> Fail:
    """A run that stops before any change (exit 2)."""
    return Fail(message, EXIT_STOPPED)


def machine_attr(ws: Path) -> str:
    """The flake attribute of the toplevel of the machine that `system.json` names (the compile step writes it)."""
    name = str(json.loads((ws / "system.json").read_text())["name"])
    quoted = name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)
    return f".#nixosConfigurations.{quoted}.config.system.build.toplevel"


# --- sudo -------------------------------------------------------------------------------------------
def ensure_sudo(session: Session, has_terminal: bool) -> None:
    """Make sudo ready for the next commands, with at most one prompt (009-D15, BSX-379).

    A valid sudo ticket (or a passwordless sudo) asks for nothing. Otherwise the command says why it asks, then
    `sudo -v` asks once. The next sudo commands in the plan use the ticket.
    """
    if session.run([SUDO, "-n", "true"], quiet=True).code == 0:
        return
    if not has_terminal:
        raise stopped(session.word("sudo_no_terminal"))
    session.say(session.word("sudo_notice"))
    if session.run([SUDO, "-v"]).code != 0:
        raise stopped(session.word("sudo_refused"))


# --- the plan ---------------------------------------------------------------------------------------
def real(path: Path) -> str | None:
    """The store path that a link reaches, or None when there is no link."""
    return os.path.realpath(path) if os.path.lexists(path) else None


def generation_of(profile: Path, target: str | None) -> int | None:
    """The newest generation of the system profile that points at `target`."""
    if target is None:
        return None
    found = [
        int(match.group(1))
        for link in profile.parent.glob(f"{profile.name}-*-link")
        if (match := re.fullmatch(rf"{re.escape(profile.name)}-(\d+)-link", link.name))
        and os.path.realpath(link) == target
    ]
    return max(found) if found else None


def switch(system: str, action: str) -> Command:
    return [SUDO, f"{system}/bin/switch-to-configuration", action]


def roll_back_commands(host: Host, running: str | None, earlier: str | None, built: str) -> list[Command]:
    """Switch back to the running system with `test`, and undo a profile change that the failed finish made (L123).

    No command means that there is no running system to return to.
    """
    if running is None or running == built or not (Path(running) / "bin" / "switch-to-configuration").exists():
        return []
    commands: list[Command] = []
    profile = str(host.profile)
    if real(host.profile) == built and earlier != built:  # the finish step set the profile, then failed
        previous = generation_of(host.profile, earlier)
        if previous is not None:
            commands.append([SUDO, "nix-env", "-p", profile, "--switch-generation", str(previous)])
        elif earlier is not None:
            commands.append([SUDO, "nix-env", "-p", profile, "--set", earlier])
        failed = generation_of(host.profile, built)
        if failed is not None:
            commands.append([SUDO, "nix-env", "-p", profile, "--delete-generations", str(failed)])
    return [*commands, switch(running, "test"), list(RESET_FAILED)]


GREETER_UNIT = Path("etc") / "systemd" / "system" / "greetd.service"


def greeter_changed(running: str | None, built: str) -> bool:
    """True when the new system has a login screen (greetd) that the running system lacks or has in another form (L29).

    The switch does not restart greetd (its unit has `restartIfChanged = false`): a restart would end the session of the
    learner. The new screen shows at the next start. The unit holds the path of the settings file, so a new setting gives
    a new unit file.
    """
    new = real(Path(built) / GREETER_UNIT)
    return new is not None and new != (real(Path(running) / GREETER_UNIT) if running else None)


def warn_about_cycles(session: Session, built: str) -> list[Cycle]:
    """Print a loud warning when the built system has a systemd ordering cycle (BSX-411, 010-D56).

    The warning does not stop the apply: Basix has no override, and the report can come from an upstream unit.
    """
    cycles = ordering_cycles(session.runner, Path(built), session.ws)
    if not cycles:
        return []
    for cycle in cycles:
        session.log(cycle.line)
    session.say(session.word("cycle_warning", count=len(cycles), cycles=summary(cycles)))
    return cycles


def plan(session: Session, built: str, host: Host, has_terminal: bool) -> Plan:
    """How Basix switches to `built`. The boot default changes only after the health check passes (L123).

    The system is unchanged only when it runs and is the boot default. After a failed apply that kept the new
    system (`BASIX_ON_FAIL=keep`), the same files must still set the boot default.
    """
    running, earlier = real(host.current), real(host.profile)
    if built == running and built == earlier:
        return Plan(unchanged=True)
    warn_about_cycles(session, built)
    ensure_sudo(session, has_terminal)
    return Plan(
        unchanged=False,
        notice=session.word("greeter_next_start") if greeter_changed(running, built) else None,
        reset=[list(RESET_FAILED)],
        activate=[switch(built, "test")],
        finish=[[SUDO, "nix-env", "-p", str(host.profile), "--set", built], switch(built, "boot")],
        restore=lambda: roll_back_commands(host, running, earlier, built),
    )


def basix_layer(
    ws: Path,
    *,
    host: Host = DEFAULT_HOST,
    has_terminal: bool = True,
    folder: str = DEFAULT_FOLDER,
    compile: Callable[[Session], None] | None = None,
    preflight: Callable[[Session], None] | None = None,
) -> Layer:
    """The layer of the workspace `ws`. `compile` replaces the default compile step of the engine."""

    def build_command(argv: Sequence[str]) -> Sequence[str]:
        return [*argv[:-1], machine_attr(ws)] if argv[-1] == BUILD_PLACEHOLDER else argv

    return Layer(
        name="basix",
        entry="system.py",
        json_file="system.json",
        folder=folder,
        noun="system",
        compiler_bin="basix-compile",
        build_attr=BUILD_PLACEHOLDER,
        health_command=HEALTH_COMMAND,
        unit_suffixes=UNIT_SUFFIXES,
        plan=lambda session, built: plan(session, built, host, has_terminal),
        on_fail_env="BASIX_ON_FAIL",
        compile=compile,
        build_command=build_command,
        preflight=preflight,
        fallback_identity=FALLBACK_IDENTITY,
        words=WORDS,
    )
