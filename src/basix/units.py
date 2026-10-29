"""The ordering check for the systemd units of a built system (BSX-411, 010-D56).

A set of units can order each other in a loop: A starts after B, and B starts after A. Nix builds such a system
without an error. At boot, systemd deletes jobs at random to break the loop. Services then fail in a different way on
each boot.

`systemd-analyze verify` prints "Found ordering cycle" for each loop. It exits 0 even then, so this file counts the
lines. It prints other lines for units of upstream packages. This file ignores them.

The check reads a built system (`result/`, the toplevel). It runs the `systemd-analyze` of that system, and falls
back to the one on `PATH`. It checks `default.target` and every unit that the target pulls in: the boot transaction.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Result(Protocol):
    """What the check reads from a finished command. `pynix_engine.Result` has both fields."""

    @property
    def code(self) -> int: ...

    @property
    def out(self) -> str: ...


class Runner(Protocol):
    """Anything that runs a command. `pynix_engine.Runner` fits. A script of the checks uses a small adapter.

    The file imports no other library, so the scripts of the opt-in checks can load it with a bare Python.
    """

    def run(self, argv: Sequence[str], *, cwd: Path | None = None) -> Result: ...


CYCLES_SHOWN = 3
MARKER = "Found ordering cycle"
UNIT_DIR = Path("etc") / "systemd" / "system"
TARGET = "default.target"
ANALYZE = Path("sw") / "bin" / "systemd-analyze"


@dataclass(frozen=True)
class Cycle:
    """One loop that systemd found: the text of its line, and the units in the loop."""

    line: str
    units: tuple[str, ...]

    def chain(self) -> str:
        return ", ".join(self.units)


def summary(cycles: Sequence[Cycle], shown: int = CYCLES_SHOWN) -> str:
    """One line for each of the first `shown` cycles, with the names of their units, and a line for the rest."""
    lines = [f"  cycle {number}: {cycle.chain()}" for number, cycle in enumerate(cycles[:shown], 1)]
    if len(cycles) > shown:
        lines.append(f"  and {len(cycles) - shown} more (see the log)")
    return "\n".join(lines)


def parse_cycle(line: str) -> Cycle | None:
    """Read one output line. Return None when the line is not a cycle line.

    The line has this form: `UNIT: Found ordering cycle: A/start after B/start after C/start - after A`.
    """
    _, found, rest = line.partition(MARKER + ":")
    if not found:
        return None
    path = rest.split(" - after ", 1)[0]
    names = [step.strip().removesuffix("/start").removesuffix("/stop") for step in path.split(" after ")]
    return Cycle(line.strip(), tuple(name for name in names if name))


def parse_cycles(output: str) -> list[Cycle]:
    """The cycles in the output of `systemd-analyze verify`, the shortest first.

    A loop that has the same units appears once. The shortest loop shows the cause. A longer one adds units that wait
    on the loop.
    """
    seen: set[frozenset[str]] = set()
    cycles: list[Cycle] = []
    for text in output.splitlines():
        cycle = parse_cycle(text)
        if cycle is None or frozenset(cycle.units) in seen:
            continue
        seen.add(frozenset(cycle.units))
        cycles.append(cycle)
    return sorted(cycles, key=lambda cycle: len(cycle.units))


def analyze_command(top: Path) -> Sequence[str] | None:
    """The command that verifies the boot transaction of the system `top`. None when the system has no units."""
    target = top / UNIT_DIR / TARGET
    if not target.exists():
        return None
    own = top / ANALYZE
    # systemd-analyze otherwise discovers units through the caller's live system. Keep
    # this override on the analyzer child only; never alter the apply process environment.
    unit_path = f"{top / UNIT_DIR}:{top / 'sw' / 'lib' / 'systemd' / 'system'}"
    return [
        "env",
        f"SYSTEMD_UNIT_PATH={unit_path}",
        str(own) if own.exists() else "systemd-analyze",
        "verify",
        str(target),
    ]


def ordering_cycles(runner: Runner, top: Path, cwd: Path | None = None) -> list[Cycle]:
    """The ordering cycles of the built system `top`. The list is empty when there is none.

    A system with no `default.target`, and a host with no `systemd-analyze`, give an empty list.
    """
    command = analyze_command(top)
    if command is None:
        return []
    result = runner.run(command, cwd=cwd)
    if result.code == 127:  # the program is missing
        return []
    return parse_cycles(result.out)
