"""`basix apply`: the order of the steps, the plain lines, the sudo prompt, the failure rule, and the refusals.

CONCEPT 4.1, L123, L134, 009-D15, 009-D20.
"""

import os
import subprocess
from pathlib import Path

import pytest
from test_cmd_support import (
    BAD_UNITS,
    CACHE,
    COMPILE_ERROR,
    DRV,
    ENTRY,
    LOOPING_UNITS,
    NIX,
    Setup,
    git_env,
    initialized,
)

from basix import commands
from basix.layer import SUDO

NOTICE = "Basix needs your password to change the machine"
TOPLEVEL = ".#nixosConfigurations.pybox.config.system.build.toplevel"
FIRST_APPLY = [
    "compiler",
    "compile",
    "build",
    "ticket",
    "password",
    "reset",
    "test:sys-1",
    "health",
    "profile:--set:sys-1",
    "boot:sys-1",
]
CONFLICT = (
    "error: Cannot build '/nix/store/0123456789abcdfghijklmnpqrsvwxyz-nixos-system.drv'.\n"
    "       error: The option `services.openssh.ports' has conflicting definition values:\n"
    "       - In `system.py:8 (RemoteLogin)': 2222\n"
    "       - In `system.py:3 (Machine)': 22\n"
    "       Use `lib.mkForce value` or `lib.mkDefault value` to change the priority on any of these definitions.\n"
)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return initialized(tmp_path)


def apply(setup: Setup, **env: str) -> int:
    setup.ctx.env = {**setup.ctx.env, **env}
    return commands.apply(setup.ctx)


def edit(setup: Setup, text: str = "# an edit\n") -> None:
    with (setup.ws / "system.py").open("a") as handle:
        handle.write(text)


# --- the steps and the lines ------------------------------------------------------------------------
def test_first_apply_runs_the_steps_in_order_and_saves_the_files(setup: Setup) -> None:
    assert apply(setup) == 0, setup.lines
    assert setup.trace() == FIRST_APPLY
    assert setup.lines == [
        "checked: system.py",
        "building the system",
        "built the system",
        NOTICE,
        "checking the units for 8 s",
        "applied: the system runs, and its units are healthy",
        "saved: system.json",
    ]
    assert setup.commits() == ["apply: system.json", "install"]
    assert setup.git("status", "--porcelain") == ""


def test_apply_makes_the_new_system_the_boot_default_only_at_the_end(setup: Setup) -> None:
    assert apply(setup) == 0
    assert setup.world.running() == str(setup.world.tmp / "store" / "sys-1")
    assert setup.world.boot_default() == str(setup.world.tmp / "store" / "sys-1")
    assert setup.world.switches == [("sys-1", "test"), ("sys-1", "boot")]


def test_apply_prints_no_store_path_and_no_git_text(setup: Setup) -> None:
    assert apply(setup) == 0
    text = "\n".join(setup.lines)
    assert "/nix/store" not in text
    assert str(setup.world.tmp) not in text
    assert "git " not in text


def test_apply_keeps_the_log_of_the_run(setup: Setup) -> None:
    assert apply(setup) == 0
    log = setup.log()
    assert "$ " in log
    assert "switch-to-configuration test" in log


def test_every_nix_call_carries_the_features(setup: Setup) -> None:
    setup.world.caches = [CACHE]
    assert apply(setup) == 0
    assert [call for call in setup.fake.calls if call[0] == "nix"]
    assert all(call[:3] == NIX for call in setup.fake.calls if call[0] == "nix")
    root = [call for call in setup.sudo_calls() if call[1] == "nix"][0]
    assert root[1:4] == ("nix", *NIX[1:])


def test_apply_builds_the_compiler_from_the_workspace_lock_with_a_root(setup: Setup) -> None:
    assert apply(setup) == 0
    call = setup.nix_calls("build", "--out-link")[0]
    assert call[len(NIX) + 2] == str(setup.ws / ".git" / "basix" / "compiler")
    assert call[-1] == ".#compiler"


def test_apply_builds_the_toplevel_once_and_never_evaluates_first(setup: Setup) -> None:
    assert apply(setup) == 0
    builds = [c for c in setup.nix_calls("build") if c[-1] == TOPLEVEL]
    assert len(builds) == 1
    assert str(setup.ws / ".git" / "basix" / "result") in builds[0]
    assert "eval" not in setup.trace()  # L21


# --- sudo -------------------------------------------------------------------------------------------
def test_the_password_line_comes_before_the_first_sudo_prompt(setup: Setup) -> None:
    assert apply(setup) == 0
    assert setup.lines.count(NOTICE) == 1
    assert setup.trace().index("password") == setup.trace().index("ticket") + 1
    # the line comes after the build and before the units are watched
    assert setup.lines.index(NOTICE) == setup.lines.index("built the system") + 1


def test_one_prompt_serves_every_sudo_command_of_the_plan(setup: Setup) -> None:
    assert apply(setup) == 0
    assert setup.trace().count("password") == 1
    assert setup.world.unauthorized == []
    assert all(call[0] == SUDO for call in setup.sudo_calls())


def test_a_valid_sudo_ticket_asks_for_nothing(tmp_path: Path) -> None:
    setup = initialized(tmp_path, ticket=True)
    assert apply(setup) == 0
    assert NOTICE not in setup.lines
    assert "password" not in setup.trace()


def test_apply_with_no_terminal_and_no_ticket_stops_before_the_switch(setup: Setup) -> None:
    setup.ctx.has_terminal = False
    assert apply(setup) == 2
    assert setup.lines[-1] == (
        "Basix needs your password to change the machine, and there is no terminal. "
        "Run basix in a terminal window. Nothing was switched or saved."
    )
    assert "test:sys-1" not in setup.trace()
    assert setup.commits() == ["install"]


def test_a_refused_password_stops_before_the_switch(tmp_path: Path) -> None:
    setup = initialized(tmp_path, password_ok=False)
    assert apply(setup) == 2
    assert (
        setup.lines[-1] == "sudo did not accept the password. Nothing was switched or saved. The system did not change."
    )
    assert setup.world.switches == []
    assert setup.commits() == ["install"]


# --- nothing to change ------------------------------------------------------------------------------
def test_apply_with_the_running_system_says_nothing_to_change(setup: Setup) -> None:
    assert apply(setup) == 0
    setup.lines.clear()
    setup.fake.calls.clear()
    assert apply(setup) == 0
    assert setup.lines[-1] == "nothing to change"
    assert setup.sudo_calls() == []  # nothing to change asks for no password
    assert setup.commits() == ["apply: system.json", "install"]


def test_apply_with_only_a_file_change_saves_it_and_skips_the_switch(setup: Setup) -> None:
    assert apply(setup) == 0
    setup.fake.calls.clear()
    edit(setup)
    assert apply(setup) == 0
    assert "the system is the same; only files changed" in setup.lines
    assert setup.sudo_calls() == []
    assert setup.commits()[0] == "apply: system.py"


def test_a_system_that_runs_but_is_not_the_boot_default_is_not_unchanged(setup: Setup) -> None:
    setup.world.units = BAD_UNITS
    assert apply(setup, BASIX_ON_FAIL="keep") == 3  # the new system runs, the boot default did not move
    setup.world.units = "web.service loaded active running Web\n"
    setup.lines.clear()
    setup.fake.calls.clear()
    assert apply(setup) == 0, setup.lines
    assert setup.world.boot_default() == str(setup.world.tmp / "store" / "sys-1")
    assert "boot:sys-1" in setup.trace()
    assert setup.commits()[0] == "apply: system.json"


# --- errors before any change -----------------------------------------------------------------------
def test_a_compile_error_prints_the_line_and_stops_before_the_build(setup: Setup) -> None:
    setup.world.compile_error = COMPILE_ERROR
    assert apply(setup) == 2
    assert setup.lines == [COMPILE_ERROR]
    assert setup.trace() == ["compiler", "compile"]
    assert not (setup.ws / "system.json").exists()
    assert not (setup.ws / "system.json.new").exists()
    assert setup.commits() == ["install"]


def test_a_missing_system_py_stops_with_one_line(setup: Setup) -> None:
    (setup.ws / "system.py").unlink()
    assert apply(setup) == 2
    assert setup.lines == ["Found no system.py in ~/basix. Run the Basix install first."]


def test_a_failed_compiler_build_stops_with_the_cause(setup: Setup) -> None:
    setup.world.build_error = "compiler: no network"
    assert apply(setup) == 2
    assert setup.lines[0].startswith("The compiler could not be built. Nothing was built, switched, or saved.")
    assert setup.commits() == ["install"]


def test_a_failed_build_stops_before_the_switch_with_the_log_path(setup: Setup) -> None:
    setup.world.build_error = "error: builder for 'nixos-system.drv' failed\n"
    assert apply(setup) == 2
    out = "\n".join(setup.lines)
    assert "The build failed. Nothing was switched or saved. The system did not change." in out
    assert setup.lines[-1].endswith("(full log: .git/basix/last.log)")
    assert setup.sudo_calls() == []


# --- the conflict line (009-D20) --------------------------------------------------------------------
def test_a_conflict_between_two_learner_lines_prints_one_line_and_logs_the_nix_text(setup: Setup) -> None:
    setup.world.build_error = CONFLICT
    assert apply(setup) == 2
    out = "\n".join(setup.lines)
    assert (
        "system.py:3 (Machine) and system.py:8 (RemoteLogin) set different values "
        "(NixOS option services.openssh.ports). Remove one." in out
    )
    assert "mkForce" not in out
    assert "definition" not in out
    assert "/nix/store" not in out
    assert "mkForce" in setup.log()  # the Nix text goes to the log


def test_a_conflict_with_a_line_that_is_not_the_learner_gets_the_general_text(setup: Setup) -> None:
    other = CONFLICT.replace(
        "system.py:3 (Machine)", "/nix/store/0123456789abcdfghijklmnpqrsvwxyz-source/modules/core.nix"
    )
    setup.world.build_error = other
    assert apply(setup) == 2
    out = "\n".join(setup.lines)
    assert "Remove one" not in out
    assert "The build failed. Nothing was switched or saved." in out


# --- the stage guards (G11) -------------------------------------------------------------------------
def test_a_secret_stops_the_apply_before_nix_reads_a_file(setup: Setup) -> None:
    (setup.ws / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    assert apply(setup) == 2
    assert setup.lines == ["apply stopped: key.pem looks like a secret; move it out of ~/basix"]
    assert not [call for call in setup.fake.calls if call[0] == "nix"]
    assert setup.commits() == ["install"]


def test_a_refused_secret_leaves_the_index_as_it_was(setup: Setup) -> None:
    (setup.ws / "key.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (setup.ws / "notes.txt").write_text("a plain new file\n")
    before = setup.git("ls-files", "-s")
    assert apply(setup) == 2
    assert setup.git("ls-files", "-s") == before
    assert setup.git("diff", "--cached", "--name-only") == ""
    assert setup.commits() == ["install"]


def test_a_nested_repository_stops_the_apply(setup: Setup) -> None:
    nested = setup.ws / "other"
    nested.mkdir()
    for args in (
        ["init", "-q"],
        ["-c", "user.name=T", "-c", "user.email=t@example.org", "commit", "-q", "--allow-empty", "-m", "x"],
    ):
        subprocess.run(["git", *args], cwd=nested, env=git_env(), check=True, capture_output=True)
    before = setup.git("ls-files", "-s")
    assert apply(setup) == 2
    assert setup.lines == [
        "apply stopped: other holds its own repository, so its files stay hidden; move it out of ~/basix"
    ]
    assert setup.git("ls-files", "-s") == before


def test_a_symlink_that_leaves_the_workspace_stops_the_apply(setup: Setup) -> None:
    os.symlink("/etc/hostname", setup.ws / "outside")
    before = setup.git("ls-files", "-s")
    assert apply(setup) == 2
    assert setup.lines == ["apply stopped: outside is a link that leaves ~/basix; put the file inside ~/basix"]
    assert setup.git("ls-files", "-s") == before


# --- a failed unit (L123, G7) -----------------------------------------------------------------------
def test_a_failed_unit_switches_back_with_test_and_never_sets_the_boot_default(setup: Setup) -> None:
    earlier = setup.world.running()
    setup.world.units = BAD_UNITS
    assert apply(setup) == 3
    out = "\n".join(setup.lines)
    assert "problem: failed units: api.service" in out
    assert "failed: the earlier system is back. Nothing was saved." in out
    assert "Your edited files are still in the folder." in out
    assert setup.world.running() == earlier  # the running generation is back
    assert setup.world.boot_default() == earlier  # the boot default never changed
    assert setup.world.switches == [("sys-1", "test"), ("sys-0", "test")]
    assert not [t for t in setup.trace() if t.startswith("boot") or t.startswith("profile")]
    assert setup.commits() == ["install"]
    assert (setup.ws / "system.json").exists()  # the edit stays in the folder


def test_the_switch_back_runs_the_earlier_system_then_resets_the_units(setup: Setup) -> None:
    setup.world.units = BAD_UNITS
    assert apply(setup) == 3
    trace = setup.trace()
    assert trace[trace.index("test:sys-1") :] == ["test:sys-1", "health", "test:sys-0", "reset"]


def test_a_looping_unit_counts_as_failed(setup: Setup) -> None:
    setup.world.units = LOOPING_UNITS
    assert apply(setup) == 3
    assert "problem: failed units: api.service" in setup.lines


def test_a_failed_test_switch_goes_back(setup: Setup) -> None:
    setup.world.test_code = 7
    assert apply(setup) == 3
    assert "problem: the system did not switch (exit 7)" in setup.lines
    assert setup.world.running() == str(setup.world.tmp / "store" / "sys-0")
    assert setup.commits() == ["install"]


def test_a_failed_health_query_fails_the_apply(setup: Setup) -> None:
    setup.world.health_code = 1
    setup.world.units = "Failed to connect to bus: No medium found\n"
    assert apply(setup) == 3
    assert "problem: the health check could not run" in setup.lines
    assert setup.commits() == ["install"]


def test_on_fail_keep_keeps_the_new_system_and_sets_no_boot_default(setup: Setup) -> None:
    setup.world.units = BAD_UNITS
    assert apply(setup, BASIX_ON_FAIL="keep") == 3
    out = "\n".join(setup.lines)
    assert "failed: the new system is running, but it did not start cleanly. Nothing was saved." in out
    assert setup.world.running() == str(setup.world.tmp / "store" / "sys-1")
    assert setup.world.boot_default() == str(setup.world.tmp / "store" / "sys-0")
    assert ("sys-0", "test") not in setup.world.switches
    assert not [t for t in setup.trace() if t.startswith("boot") or t.startswith("profile")]
    assert setup.commits() == ["install"]


def test_a_bad_on_fail_value_stops(setup: Setup) -> None:
    assert apply(setup, BASIX_ON_FAIL="maybe") == 1
    assert setup.lines == ["BASIX_ON_FAIL must be rollback or keep, found maybe."]


def test_a_failed_boot_step_restores_the_profile_and_the_running_system(setup: Setup) -> None:
    setup.world.boot_code = 5
    assert apply(setup) == 3
    assert "problem: the system did not switch (exit 5)" in setup.lines
    assert setup.world.running() == str(setup.world.tmp / "store" / "sys-0")
    assert setup.world.boot_default() == str(setup.world.tmp / "store" / "sys-0")
    assert setup.world.deleted == ["2"]  # the generation of the failed apply leaves the profile
    assert setup.commits() == ["install"]


def test_a_failed_first_apply_with_no_running_system_has_nothing_to_return_to(tmp_path: Path) -> None:
    setup = initialized(tmp_path)
    setup.world.host.current.unlink()
    setup.world.units = BAD_UNITS
    assert apply(setup) == 3
    assert "found no earlier system to return to, so the new system stays" in "\n".join(setup.lines)


# --- the caches (L134, BSX-400) ---------------------------------------------------------------------
def test_a_new_cache_makes_root_build_with_the_cache_options(tmp_path: Path) -> None:
    setup = initialized(tmp_path)
    setup.world.caches = [CACHE]
    assert apply(setup) == 0, setup.lines
    trace = setup.trace()
    assert trace[: trace.index("health") + 1] == [
        "compiler",
        "compile",
        "ticket",
        "password",
        "eval",
        "root-build",
        "build",
        "ticket",
        "reset",
        "test:sys-1",
        "health",
    ]
    [root] = [call for call in setup.sudo_calls() if call[1] == "nix"]
    assert root == (
        SUDO,
        "nix",
        *NIX[1:],
        "build",
        "--no-link",
        "--print-out-paths",
        "--option",
        "extra-substituters",
        CACHE[0],
        "--option",
        "extra-trusted-public-keys",
        CACHE[1],
        f"{DRV}^*",
    )
    [evaluation] = setup.nix_calls("eval")
    assert evaluation[-1] == f"{TOPLEVEL}.drvPath"  # the account evaluates, not root
    assert setup.lines.count(NOTICE) == 1  # one prompt for the root build and the switch
    assert setup.lines.index("building the system with the new caches") < setup.lines.index("checked: system.py")


def test_the_caches_of_the_running_system_need_no_root_build(tmp_path: Path) -> None:
    setup = initialized(tmp_path)
    setup.world.caches = [CACHE]
    conf = setup.world.host.nix_conf
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(f"# nix.conf\nextra-substituters = {CACHE[0]}/\nextra-trusted-public-keys = {CACHE[1]}\n")
    assert apply(setup) == 0
    assert "root-build" not in setup.trace()
    assert "eval" not in setup.trace()


def test_a_dropped_cache_needs_no_root_build(tmp_path: Path) -> None:
    setup = initialized(tmp_path)
    conf = setup.world.host.nix_conf
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text(f"extra-substituters = {CACHE[0]}\nextra-trusted-public-keys = {CACHE[1]}\n")
    assert apply(setup) == 0
    assert "root-build" not in setup.trace()


def test_a_failed_root_build_stops_before_the_switch(tmp_path: Path) -> None:
    setup = initialized(tmp_path, root_build_code=1)
    setup.world.caches = [CACHE]
    assert apply(setup) == 2
    assert "The build with the new caches failed. Nothing was switched or saved." in "\n".join(setup.lines)
    assert "test:sys-1" not in setup.trace()
    assert setup.commits() == ["install"]


def test_a_conflict_found_by_the_cache_evaluation_prints_the_one_line(tmp_path: Path) -> None:
    setup = initialized(tmp_path, eval_error=CONFLICT)
    setup.world.caches = [CACHE]
    assert apply(setup) == 2
    out = "\n".join(setup.lines)
    assert "system.py:3 (Machine) and system.py:8 (RemoteLogin) set different values" in out
    assert "root-build" not in setup.trace()


# --- the other layer and the folder -----------------------------------------------------------------
def test_a_minix_workspace_names_minix_apply(setup: Setup) -> None:
    minix = setup.home / "minix"
    (minix / ".git").mkdir(parents=True)
    (minix / "home.py").write_text("home = 1\n")
    setup.ctx.cwd = minix
    assert apply(setup) == 1
    assert setup.lines == [
        "~/minix is a minix workspace. basix does not change it.\n"
        "To change your own home, run: minix apply\n"
        "To change the machine, go to ~/basix and run: basix apply"
    ]
    assert setup.fake.calls == []


def test_a_dir_option_with_a_minix_workspace_names_minix_apply(setup: Setup) -> None:
    minix = setup.home / "minix"
    minix.mkdir()
    (minix / "home.py").write_text("home = 1\n")
    assert commands.apply(setup.ctx, minix) == 1
    assert setup.lines[0].startswith("~/minix is a minix workspace.")


def test_apply_outside_a_workspace_goes_to_the_default_folder(setup: Setup) -> None:
    setup.ctx.cwd = setup.home
    assert apply(setup) == 0
    assert setup.commits()[0] == "apply: system.json"


def test_apply_without_a_workspace_says_where_to_go(tmp_path: Path) -> None:
    from test_cmd_support import make_setup

    other = make_setup(tmp_path)
    assert commands.apply(other.ctx) == 1
    assert other.lines == ["~ is not a basix workspace. Go to ~/basix first."]


def test_apply_finds_the_workspace_from_a_subfolder(setup: Setup) -> None:
    sub = setup.ws / "notes"
    sub.mkdir()
    setup.ctx.cwd = sub
    assert apply(setup) == 0
    assert setup.commits()[0] == "apply: system.json"


def test_apply_commits_with_the_fallback_identity_when_git_knows_no_author(setup: Setup) -> None:
    assert apply(setup) == 0
    assert setup.git("log", "-1", "--format=%an <%ae>").strip() == "basix <basix@localhost>"


# --- the stub, the lock, and the input packages -----------------------------------------------------
INPUT = 'from basix_config import *\n\ninputs = [Input("nirinit", "github:amaanq/nirinit/REV", modules=["nirinit"])]\n'


def test_an_input_in_system_py_rewrites_the_stub_locks_and_makes_the_packages(setup: Setup) -> None:
    (setup.ws / "system.py").write_text(ENTRY + INPUT.split("\n\n", 1)[1])
    assert apply(setup) == 0, setup.lines
    stub = (setup.ws / "flake.nix").read_text()
    assert 'inputs.nirinit.url = "github:amaanq/nirinit/REV";' in stub
    assert 'inputs.nirinit.inputs.nixpkgs.follows = "nixpkgs";' in stub
    assert setup.trace()[:4] == ["lock", "packages", "compiler", "compile"]
    packages = setup.nix_calls("build", "--out-link")[0]
    assert packages[len(NIX) + 2] == str(setup.ws / ".basix" / "pynix")
    assert packages[-1] == ".#input-packages"
    assert setup.git("status", "--porcelain") == ""
    assert ".basix" not in setup.git("ls-files")


def test_an_unchanged_stub_needs_no_lock_but_the_input_packages_are_built_on_every_run(setup: Setup) -> None:
    (setup.ws / "system.py").write_text(ENTRY + INPUT.split("\n\n", 1)[1])
    assert apply(setup) == 0, setup.lines
    setup.fake.calls.clear()
    edit(setup)
    assert apply(setup) == 0, setup.lines
    assert setup.nix_calls("flake", "lock") == []
    assert len([c for c in setup.nix_calls("build") if c[-1] == ".#input-packages"]) == 1  # 010-D13


def test_an_entry_without_inputs_builds_no_input_packages(setup: Setup) -> None:
    assert apply(setup) == 0, setup.lines
    assert [c for c in setup.nix_calls("build") if c[-1] == ".#input-packages"] == []
    assert setup.nix_calls("flake", "lock") == []


def test_the_stub_keeps_the_url_that_it_holds(setup: Setup) -> None:
    stub = setup.ws / "flake.nix"
    stub.write_text(stub.read_text().replace("Bullish-Intelligence/basix", "example.org/mine"))
    assert apply(setup) == 0
    assert 'inputs.basix.url = "git+https://github.com/example.org/mine?ref=release";' in stub.read_text()


def test_a_flake_nix_that_basix_did_not_write_stops_the_apply(setup: Setup) -> None:
    (setup.ws / "flake.nix").write_text("{ outputs = _: { }; }\n")
    assert apply(setup) == 2
    assert setup.lines[0].startswith("flake.nix is not the file that Basix writes")


def test_a_failed_package_build_stops_before_the_compiler(setup: Setup) -> None:
    (setup.ws / "system.py").write_text(ENTRY + INPUT.split("\n\n", 1)[1])
    setup.world.input_packages_code = 1
    assert apply(setup) == 2
    assert setup.lines[0].startswith("The input packages could not be built. Nothing was built, switched, or saved.")
    assert "compiler" not in setup.trace()


def test_a_failed_lock_stops_with_the_cause(setup: Setup) -> None:
    (setup.ws / "system.py").write_text(ENTRY + INPUT.split("\n\n", 1)[1])
    setup.world.lock_code = 1
    assert apply(setup) == 2
    assert setup.lines[0].startswith("The inputs could not be locked. Nothing was built, switched, or saved.")
    assert "cannot lock" in setup.lines[0]


def test_apply_ignores_the_scratch_folder_when_the_ignore_file_lost_it(setup: Setup) -> None:
    (setup.ws / ".gitignore").write_text("result\n")
    setup.git("-c", "user.name=t", "-c", "user.email=t@example.org", "commit", "-q", "-am", "drop the ignore line")
    assert apply(setup) == 0
    assert (setup.ws / ".gitignore").read_text() == "result\n.basix/\n"
    assert "added .basix/ to .gitignore" in setup.lines
