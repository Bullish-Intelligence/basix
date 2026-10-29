"""The parts of the command that the apply tests reach only through the fake machine: the conflict reader, the cache
comparison, the rollback commands, the explain call, and the folder search."""

from pathlib import Path

import pytest
from pynix_engine import Fail, Result
from test_cmd_support import CACHE, Setup, World, initialized

from basix import commands
from basix.caches import machine_caches, new_caches, read_conf, same_url
from basix.errors import conflict_line
from basix.layer import SUDO, WORDS, roll_back_commands
from basix.nix import NixRunner, with_features
from basix.workspace import find_workspace

# Observed on the pinned nixpkgs `e7439b6b`, with `lib.evalModules` and two modules that have a `_file`
# (`~/.local/state/basix/010/P3/p3-cmd/{p,b,u}.out`).
EQUAL = """\
       error: The option `services.x.port' has conflicting definition values:
       - In `system.py:8 (RemoteLogin)': 2222
       - In `system.py:3 (Machine)': 22
       Use `lib.mkForce value` or `lib.mkDefault value` to change the priority on any of these definitions.
"""
LONG_VALUE = """\
       error: The option `a.b' has conflicting definition values:
       - In `system.py:8 (RemoteLogin)':
           ''
             y
             more
           ...
       - In `system.py:3 (Machine)': "x"
       Use `lib.mkForce value` or `lib.mkDefault value` to change the priority on any of these definitions.
"""
UNIQUE = """\
       error: The option `u' is defined multiple times while it's expected to be unique.

       Definition values:
       - In `system.py:8 (RemoteLogin)': "r"
       - In `system.py:3 (Machine)': "q"
       Use `lib.mkForce value` or `lib.mkDefault value` to change the priority on any of these definitions.
"""


# --- the conflict line ------------------------------------------------------------------------------
@pytest.mark.parametrize(("text", "option"), [(EQUAL, "services.x.port"), (LONG_VALUE, "a.b"), (UNIQUE, "u")])
def test_a_conflict_between_learner_lines_names_both_lines_in_order(text: str, option: str) -> None:
    assert conflict_line(text, "system.py", WORDS) == (
        f"system.py:3 (Machine) and system.py:8 (RemoteLogin) set different values (NixOS option {option}). Remove one."
    )


def test_three_learner_lines_make_one_list() -> None:
    text = EQUAL.replace("- In `system.py:3", "- In `system.py:12 (Program)': 5\n       - In `system.py:3")
    assert "system.py:3 (Machine), system.py:8 (RemoteLogin), and system.py:12 (Program) set" in (
        conflict_line(text, "system.py", WORDS) or ""
    )


def test_one_line_that_makes_both_values_is_named_once() -> None:
    text = EQUAL.replace("system.py:3 (Machine)", "system.py:8 (RemoteLogin)")
    assert conflict_line(text, "system.py", WORDS) == (
        "system.py:8 (RemoteLogin) gives two different values (NixOS option services.x.port). Remove one."
    )


@pytest.mark.parametrize(
    "text",
    [
        EQUAL.replace("system.py:3 (Machine)", "/nix/store/0123456789abcdfghijklmnpqrsvwxyz-source/modules/core.nix"),
        "error: attribute 'x' missing\n",
        "",
    ],
)
def test_other_errors_get_no_conflict_line(text: str) -> None:
    assert conflict_line(text, "system.py", WORDS) is None


def test_the_nix_runner_shortens_a_failed_nix_command_and_keeps_the_lines_for_the_log() -> None:
    from pynix_engine.testing import FakeRunner

    fake = FakeRunner()
    fake.on(("nix",), Result(1, EQUAL))
    seen: list[str] = []
    result = NixRunner(fake).run(["nix", "build", "x"], on_line=seen.append)
    assert result.out.startswith("system.py:3 (Machine) and system.py:8 (RemoteLogin)")
    assert any("mkForce" in line for line in seen)  # the log gets the Nix text
    assert fake.calls == [("nix", "--extra-experimental-features", "nix-command flakes", "build", "x")]


def test_the_nix_runner_leaves_other_commands_alone() -> None:
    from pynix_engine.testing import FakeRunner

    fake = FakeRunner()
    fake.on(("git",), Result(1, EQUAL))
    assert NixRunner(fake).run(["git", "status"]).out == EQUAL
    assert with_features(["systemctl", "x"]) == ["systemctl", "x"]


# --- the caches -------------------------------------------------------------------------------------
def test_same_url_ignores_the_final_slash_and_the_case_of_the_host() -> None:
    assert same_url("HTTPS://Cache.Example.org/") == same_url("https://cache.example.org")
    assert same_url("https://a.org/Path") != same_url("https://a.org/path")


def test_read_conf_reads_settings_and_skips_comments_and_a_missing_file(tmp_path: Path) -> None:
    conf = tmp_path / "nix.conf"
    conf.write_text("# nix\nsubstituters = https://a.org/ https://b.org # more\nextra-substituters = https://c.org\n")
    assert read_conf(conf) == {
        "substituters": ["https://a.org/", "https://b.org"],
        "extra-substituters": ["https://c.org"],
    }
    assert read_conf(tmp_path / "missing") == {}


def machine(*pairs: tuple[str, str]) -> dict:
    settings = {"extra-substituters": [u for u, _ in pairs], "extra-trusted-public-keys": [k for _, k in pairs]}
    return {"fragments": [{"path": ["nix"], "value": {"settings": settings}}]}


def test_machine_caches_read_the_nix_fragment_and_the_settings_fragment() -> None:
    assert machine_caches(machine(CACHE)) == ([CACHE[0]], [CACHE[1]])
    nested = {"fragments": [{"path": ["nix", "settings"], "value": {"extra-substituters": ["https://x.org"]}}]}
    assert machine_caches(nested) == (["https://x.org"], [])
    assert machine_caches({"fragments": [{"path": ["networking"], "value": {}}]}) == ([], [])


def test_new_caches_compare_with_the_running_nix_conf(tmp_path: Path) -> None:
    conf = tmp_path / "nix.conf"
    assert new_caches(machine(CACHE), conf) == ([CACHE[0]], [CACHE[1]])  # no file: nothing is known
    conf.write_text(f"extra-substituters = {CACHE[0]}\n")
    assert new_caches(machine(CACHE), conf) is not None  # the address is there, the key is not
    conf.write_text(f"substituters = {CACHE[0]}/\ntrusted-public-keys = {CACHE[1]}\n")
    assert new_caches(machine(CACHE), conf) is None
    assert new_caches(machine(), conf) is None


# --- the rollback commands --------------------------------------------------------------------------
def test_the_rollback_without_a_profile_change_is_the_switch_back_and_the_reset(tmp_path: Path) -> None:
    world = World(tmp_path.resolve(), tmp_path / "ws")
    running = str(world.install("sys-0"))
    built = str(world.store("sys-1"))
    assert roll_back_commands(world.host, running, running, built) == [
        [SUDO, f"{running}/bin/switch-to-configuration", "test"],
        [SUDO, "systemctl", "reset-failed"],
    ]


def test_the_rollback_returns_the_profile_when_the_finish_step_set_it(tmp_path: Path) -> None:
    world = World(tmp_path.resolve(), tmp_path / "ws")
    running = str(world.install("sys-0"))
    built = world.store("sys-1")
    world.set_profile(built)  # the finish step ran `nix-env --set`, then the boot step failed
    commands_ = roll_back_commands(world.host, running, running, str(built))
    profile = str(world.host.profile)
    assert commands_[:2] == [
        [SUDO, "nix-env", "-p", profile, "--switch-generation", "1"],
        [SUDO, "nix-env", "-p", profile, "--delete-generations", "2"],
    ]
    assert commands_[2][-1] == "test"


def test_the_rollback_has_no_command_without_a_running_system(tmp_path: Path) -> None:
    world = World(tmp_path.resolve(), tmp_path / "ws")
    assert roll_back_commands(world.host, None, None, str(world.store("sys-1"))) == []
    running = str(world.store("sys-1"))
    assert roll_back_commands(world.host, running, running, running) == []  # the same system: nothing to go back to


# --- explain ----------------------------------------------------------------------------------------
@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return initialized(tmp_path)


def test_explain_calls_the_function_of_lane_explain_with_the_folder_the_name_and_a_nix_runner(setup: Setup) -> None:
    seen: list[tuple[Path, str, object]] = []

    def fake(folder: Path, name: str, *, runner: object) -> int:
        seen.append((folder, name, runner))
        return 3

    setup.ctx.explain = fake
    assert commands.explain(setup.ctx, "RemoteLogin") == 3
    [(folder, name, runner)] = seen
    assert (folder, name) == (setup.ws, "RemoteLogin")
    assert isinstance(runner, NixRunner)  # every `nix` call of explain carries the features


def test_explain_outside_a_workspace_says_where_to_go(setup: Setup) -> None:
    setup.ctx.cwd = setup.home / "nowhere"
    (setup.ws / ".git").rename(setup.ws / ".gone")
    assert commands.explain(setup.ctx, "x") == 1
    assert setup.lines[0].endswith(
        "is not a basix workspace. Go to ~/basix first, or set BASIX_WORKSPACE to its folder."
    )


# --- the folder -------------------------------------------------------------------------------------
def test_the_dir_option_wins_over_the_current_folder(setup: Setup, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert find_workspace(other, setup.ws, setup.home) == other.resolve()


def test_the_repository_above_the_current_folder_counts_when_it_holds_system_py(setup: Setup) -> None:
    sub = setup.ws / "a" / "b"
    sub.mkdir(parents=True)
    assert find_workspace(None, sub, setup.home) == setup.ws


# --- the folder from the environment (016) ----------------------------------------------------------
def make_workspace(folder: Path, entry: str = "system.py") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".git").mkdir(exist_ok=True)
    (folder / entry).write_text("")
    return folder


def test_the_environment_names_the_workspace_outside_any_workspace(tmp_path: Path) -> None:
    kept = make_workspace(tmp_path / "home" / "kept" / "basix")
    nowhere = tmp_path / "nowhere"
    nowhere.mkdir()
    env = {"BASIX_WORKSPACE": str(kept)}
    assert find_workspace(None, nowhere, tmp_path / "home", env) == kept


def test_the_environment_expands_the_tilde_against_the_home_of_the_run(tmp_path: Path) -> None:
    home = tmp_path / "home"
    kept = make_workspace(home / "kept" / "basix")
    assert find_workspace(None, tmp_path, home, {"BASIX_WORKSPACE": "~/kept/basix"}) == kept
    make_workspace(home, "system.py")
    assert find_workspace(None, tmp_path, home, {"BASIX_WORKSPACE": "~"}) == home.resolve()


def test_the_order_is_dir_then_the_repository_above_then_the_environment_then_the_default(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default = make_workspace(home / "basix")
    named = make_workspace(home / "named")
    above = make_workspace(tmp_path / "above")
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    env = {"BASIX_WORKSPACE": str(named)}
    inside = above / "sub"
    inside.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert find_workspace(explicit, inside, home, env) == explicit.resolve()  # --dir first
    assert find_workspace(None, inside, home, env) == above  # then the repository above the current folder
    assert find_workspace(None, elsewhere, home, env) == named  # then the environment
    assert find_workspace(None, elsewhere, home, {}) == default  # then ~/basix
    assert find_workspace(None, elsewhere, home, {"BASIX_WORKSPACE": ""}) == default  # an empty variable is unset


def test_a_variable_that_names_a_folder_with_no_system_py_stops_with_one_line(tmp_path: Path) -> None:
    home = tmp_path / "home"
    make_workspace(home / "basix")  # the default exists, and still the run must not fall back to it
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(Fail) as stop:
        find_workspace(None, tmp_path, home, {"BASIX_WORKSPACE": str(empty)})
    assert str(stop.value) == (
        f"BASIX_WORKSPACE is {empty}, but that folder holds no system.py. "
        "Set BASIX_WORKSPACE to your workspace folder, or unset it."
    )
    with pytest.raises(Fail, match="holds no system.py"):  # a folder that does not exist
        find_workspace(None, tmp_path, home, {"BASIX_WORKSPACE": "~/missing"})
    with pytest.raises(Fail, match="holds no system.py"):  # a minix workspace is not a Basix workspace
        make_workspace(tmp_path / "min", "home.py")
        find_workspace(None, tmp_path, home, {"BASIX_WORKSPACE": str(tmp_path / "min")})


def test_a_command_reads_the_variable_from_its_context(setup: Setup) -> None:
    seen: list[Path] = []

    def fake(folder: Path, name: str, *, runner: object) -> int:
        seen.append(folder)
        return 0

    setup.ctx.explain = fake
    setup.ctx.cwd = setup.home / "nowhere"
    setup.ctx.env = {**setup.ctx.env, "BASIX_WORKSPACE": str(setup.ws)}
    assert commands.explain(setup.ctx, "x") == 0
    assert seen == [setup.ws]
