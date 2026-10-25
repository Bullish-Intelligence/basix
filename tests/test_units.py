"""The ordering check for the units of a built system (BSX-411, 010-D56).

The first tests read output that `systemd-analyze verify` printed for a module with an ordering cycle (an invented
example: a mount and a format service that wait for each other). The next tests run the real `systemd-analyze` on a
small system made of unit files. They skip when the program is absent. The apply tests use the fake machine.
"""

import shutil
import stat
from pathlib import Path

import pytest
from pynix_engine import Result, SubprocessRunner
from test_cmd_support import Setup, initialized

from basix import commands
from basix.units import MARKER, analyze_command, ordering_cycles, parse_cycle, parse_cycles, summary

CYCLE = """\
sysinit.target: Found ordering cycle: systemd-boot-random-seed.service/start after local-fs.target/start after data-store.mount/start after data-store-format.service/start after sysinit.target/start - after systemd-boot-random-seed.service
sysinit.target: Job systemd-boot-random-seed.service/start deleted to break ordering cycle starting with sysinit.target/start
sysinit.target: Found ordering cycle: systemd-binfmt.service/start after local-fs.target/start after data-store.mount/start after data-store-format.service/start after sysinit.target/start - after systemd-binfmt.service
sysinit.target: Job systemd-binfmt.service/start deleted to break ordering cycle starting with sysinit.target/start
data-store-format.service: Found ordering cycle: local-fs.target/start after data-store.mount/start after data-store-format.service/start - after local-fs.target
data-store-format.service: Job local-fs.target/start deleted to break ordering cycle starting with data-store-format.service/start
data-store-format.service: Found ordering cycle: local-fs.target/start after data-store.mount/start after data-store-format.service/start - after local-fs.target
"""
NOISE = """\
/nix/store/aaaa-unit-foo/foo.service:12: Unknown key 'Bar' in section [Service], ignoring.
systemd-udevd.service: Command 'udevadm' is not executable: No such file or directory
nscd.service: Found ordering cycle is not in this line
"""


# --- the reader -------------------------------------------------------------------------------------
def test_a_cycle_line_gives_its_units_without_the_job_suffix() -> None:
    cycle = parse_cycle(CYCLE.splitlines()[4])
    assert cycle is not None
    assert cycle.units == ("local-fs.target", "data-store.mount", "data-store-format.service")


def test_the_reader_counts_only_cycle_lines_and_ignores_the_noise_of_upstream_units() -> None:
    assert parse_cycles(NOISE) == []
    assert len(parse_cycles(NOISE + CYCLE)) == 3


def test_the_marker_inside_other_text_is_not_a_cycle() -> None:
    assert MARKER in NOISE
    assert parse_cycle("nscd.service: Found ordering cycle is not in this line") is None


def test_a_loop_that_systemd_prints_twice_appears_once_and_the_shortest_loop_is_first() -> None:
    cycles = parse_cycles(CYCLE)
    assert [c.units[0] for c in cycles] == [
        "local-fs.target",
        "systemd-boot-random-seed.service",
        "systemd-binfmt.service",
    ]


def test_the_summary_names_the_units_and_counts_the_rest() -> None:
    text = summary(parse_cycles(CYCLE), shown=2)
    assert "cycle 1: local-fs.target, data-store.mount, data-store-format.service" in text
    assert text.splitlines()[-1] == "  and 1 more (see the log)"


# --- the real program on a small system -------------------------------------------------------------
needs_analyze = pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze on PATH")


def make_system(root: Path, second_after: str | None) -> Path:
    """A system with two services that boot starts. `a` starts after `b`. `b` starts after `second_after`."""
    units = root / "etc" / "systemd" / "system"
    units.mkdir(parents=True)
    (units / "default.target").write_text("[Unit]\nDescription=test\nWants=a.service b.service\n")
    (units / "a.service").write_text("[Unit]\nAfter=b.service\n[Service]\nExecStart=/bin/true\n")
    after = f"After={second_after}\n" if second_after else ""
    (units / "b.service").write_text(f"[Unit]\n{after}[Service]\nExecStart=/bin/true\n")
    return root


@needs_analyze
def test_the_real_program_finds_a_loop_and_names_both_units(tmp_path: Path) -> None:
    cycles = ordering_cycles(SubprocessRunner(), make_system(tmp_path, "a.service"))
    assert cycles
    assert {"a.service", "b.service"} <= {unit for cycle in cycles for unit in cycle.units}


@needs_analyze
def test_the_real_program_finds_no_loop_in_a_system_with_one_order(tmp_path: Path) -> None:
    assert ordering_cycles(SubprocessRunner(), make_system(tmp_path, None)) == []


def test_a_system_with_no_units_gives_no_cycle(tmp_path: Path) -> None:
    assert analyze_command(tmp_path) is None
    assert ordering_cycles(SubprocessRunner(), tmp_path) == []


def test_the_check_uses_the_program_of_the_built_system_when_it_has_one(tmp_path: Path) -> None:
    top = make_system(tmp_path, None)
    assert analyze_command(top) == ["systemd-analyze", "verify", str(top / "etc/systemd/system/default.target")]
    program = top / "sw" / "bin" / "systemd-analyze"
    program.parent.mkdir(parents=True)
    program.write_text(
        "#!/bin/sh\necho 'x.service: Found ordering cycle: x.service/start after y.service/start - after x.service'\n"
    )
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    found = ordering_cycles(SubprocessRunner(), top)
    assert [c.units for c in found] == [("x.service", "y.service")]


def test_a_host_with_no_program_gives_no_cycle(tmp_path: Path) -> None:
    class Missing:
        def run(self, argv, *, cwd=None):
            return Result(127, "systemd-analyze: command not found\n")

    assert ordering_cycles(Missing(), make_system(tmp_path, "a.service")) == []


# --- basix apply ------------------------------------------------------------------------------------
@pytest.fixture
def cycle_setup(tmp_path: Path) -> Setup:
    return initialized(tmp_path, analyze=CYCLE)


def test_apply_warns_loudly_before_the_switch_and_still_applies(cycle_setup: Setup) -> None:
    assert commands.apply(cycle_setup.ctx) == 0, cycle_setup.lines
    lines = cycle_setup.lines
    warning = next(line for line in lines if line.startswith("WARNING"))
    assert "systemd ordering cycle (3 found)" in warning
    assert "data-store.mount" in warning and "data-store-format.service" in warning
    assert "/nix/store" not in warning
    assert lines.index(warning) < lines.index("checking the units for 8 s")
    trace = cycle_setup.trace()
    assert trace.index("build") < trace.index("test:sys-1")  # the switch still runs
    assert "boot:sys-1" in trace


def test_apply_keeps_every_cycle_line_in_the_log(cycle_setup: Setup) -> None:
    assert commands.apply(cycle_setup.ctx) == 0
    log = cycle_setup.log()
    assert log.count(MARKER) == 3


def test_apply_prints_no_warning_for_a_system_without_a_cycle(tmp_path: Path) -> None:
    setup = initialized(tmp_path, analyze=NOISE)
    assert commands.apply(setup.ctx) == 0
    assert not [line for line in setup.lines if "WARNING" in line]
    assert any(call[0] == "systemd-analyze" for call in setup.fake.calls)


def test_apply_skips_the_check_when_the_system_does_not_change(tmp_path: Path) -> None:
    setup = initialized(tmp_path, analyze=CYCLE)
    assert commands.apply(setup.ctx) == 0
    setup.lines.clear()
    setup.fake.calls.clear()
    assert commands.apply(setup.ctx) == 0
    assert not [call for call in setup.fake.calls if call[0] == "systemd-analyze"]
