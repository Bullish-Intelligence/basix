"""The profile `Desktop()` (L66, L67, L42, L29, 009-D25, 009-D30): the component, its error lines, and the login-screen line.

The evaluation of the machine (the programs, the files in `/etc/xdg`, the equality with hand-written Nix) is in
`scripts/check_eval.py`. The login screen on a real screen is `scripts/desktop_vm.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import Compiled, machine_text
from pydantic import ValidationError
from test_cmd_support import Setup, initialized

import basix_config
from basix import commands
from basix.layer import WORDS, greeter_changed
from basix_config import Console, Desktop, Machine, Program, pkgs

DISK = "/dev/disk/by-id/nvme-EXAMPLE"
LOGIN_LINE = "The login screen changes at the next start. Basix does not restart it now."


def compiled(**more: object) -> dict:
    return Machine(name="box", user="ada", disk=DISK, profile=Desktop(**more)).compile()  # ty: ignore[invalid-argument-type]


def fragment(data: dict, *path: str) -> dict:
    return next(f["value"] for f in data["fragments"] if tuple(f["path"]) == path)


def one(result: Compiled) -> str:
    assert result.code == 1, result.err
    assert len(result.lines) == 1, result.err
    return result.lines[0]


# --- the component ------------------------------------------------------------------------------------------------
def test_the_learner_gets_the_name() -> None:
    assert "Desktop" in basix_config.__all__
    assert isinstance(Desktop(), basix_config.Profile)
    assert isinstance(Machine(name="box", user="ada", disk=DISK).profile, Console)


def test_the_defaults() -> None:
    desktop = Desktop()
    assert (desktop.browser, desktop.greeting, desktop.keyboard_layout, desktop.locale, desktop.extra_programs) == (
        True,
        None,
        None,
        "C.UTF-8",
        [],
    )


def test_the_profile_name_is_the_folder_of_the_native_part() -> None:
    data = compiled()
    assert data["profile"] == "desktop"
    assert (Path(__file__).resolve().parent.parent / "profiles" / data["profile"] / "module.nix").is_file()
    # `lib.machine` imports `profiles/desktop/module.nix` itself, so the component names no native module.
    assert data["modules"] == []


def test_the_session_and_the_programs() -> None:
    data = compiled()
    assert fragment(data, "programs", "hyprland") == {"enable": True, "withUWSM": True}
    assert fragment(data, "programs", "noctalia") == {
        "enable": True,
        "systemd": {"enable": True},
        "recommendedServices": {"enable": True},
    }
    packages = fragment(data, "environment")["systemPackages"]
    assert [p["__nixdantic_ref__"]["attr"] for p in packages] == ["foot", "firefox"]


def test_the_login_has_no_autologin_and_runs_the_wrapper() -> None:
    greetd = fragment(compiled(), "services", "greetd")
    assert greetd["enable"] is True and greetd["useTextGreeter"] is True
    assert "initial_session" not in greetd["settings"]
    command = greetd["settings"]["default_session"]["command"]["__nixdantic_ref__"]
    assert (command["kind"], command["level"]) == ("priority", 1000)
    parts = command["value"]["__nixdantic_ref__"]["parts"]
    assert parts[-1] == "--cmd basix-session"
    assert not any(isinstance(p, str) and "/nix/store" in p for p in parts)
    assert "/nix/store" not in json.dumps(compiled())


def test_the_greeting_goes_before_the_session() -> None:
    parts = fragment(compiled(greeting="Welcome, Ada!"), "services", "greetd")["settings"]["default_session"][
        "command"
    ]["__nixdantic_ref__"]["value"]["__nixdantic_ref__"]["parts"]
    assert parts[-2:] == ["--greeting 'Welcome, Ada!' ", "--cmd basix-session"]


def test_a_value_that_a_native_module_may_replace_is_default() -> None:
    """L42: the locale, the keyboard layout, and the login command have the priority of `mkDefault` (1000)."""
    data = compiled(keyboard_layout="de", locale="en_US.UTF-8")
    for path, value in ((("i18n",), "en_US.UTF-8"), (("console",), "de")):
        field = next(iter(fragment(data, *path).values()))["__nixdantic_ref__"]
        assert (field["kind"], field["level"], field["value"]) == ("priority", 1000, value)
    assert "console" not in [f["path"][0] for f in compiled()["fragments"]]


def test_a_locale_or_layout_of_the_learner_turns_the_imperative_locale_off() -> None:
    """010-D24: with either field set, a later change of the field applies. With neither, the core keeps the setting."""
    assert "imperativeLocale" not in fragment(compiled(), "i18n")
    for more in ({"locale": "C.UTF-8"}, {"locale": "en_US.UTF-8"}, {"keyboard_layout": "de"}):
        field = fragment(compiled(**more), "i18n")["imperativeLocale"]["__nixdantic_ref__"]
        assert (field["kind"], field["level"], field["value"]) == ("priority", 1000, False)
    assert "imperativeLocale" not in fragment(compiled(greeting="Hi", browser=False), "i18n")


def test_the_browser_can_be_left_out_and_programs_added() -> None:
    packages = fragment(compiled(browser=False, extra_programs=[Program(pkgs.htop)]), "environment")["systemPackages"]
    assert [p["__nixdantic_ref__"]["attr"] for p in packages] == ["foot", "htop"]


def test_the_user_rule_links_the_noctalia_default() -> None:
    rules = fragment(compiled(), "systemd", "user")["tmpfiles"]["rules"]
    assert rules[-1] == "L %h/.config/noctalia/00-basix.toml - - - - /etc/xdg/noctalia/00-basix.toml"


def test_the_provenance_names_the_options_of_the_profile() -> None:
    listed = compiled()["provenance"]["Desktop"]
    assert "programs.hyprland.enable" in listed and "services.greetd.settings.default_session.command" in listed


def test_the_machine_has_the_settings_as_fields() -> None:
    with pytest.raises(ValidationError):
        Desktop(greeting='say "hi"')
    with pytest.raises(ValidationError):
        Desktop(colour="blue")  # ty: ignore[unknown-argument]


# --- the default files --------------------------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent


def test_the_native_part_holds_the_default_look_and_the_wrapper() -> None:
    text = (ROOT / "profiles" / "desktop" / "module.nix").read_text()
    assert 'name = "basix-session"' in text
    assert 'environment.etc."xdg/hypr/hyprland.lua".source = lib.mkDefault ./hyprland.lua;' in text
    assert 'environment.etc."xdg/noctalia/00-basix.toml".source = lib.mkDefault ./00-basix.toml;' in text
    code = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    assert not any("minix" in line.lower() for line in code)  # the machine names no minix part (009-D30)


def test_the_hyprland_default_is_a_lua_file_with_the_basix_binds() -> None:
    text = (ROOT / "profiles" / "desktop" / "hyprland.lua").read_text()
    assert 'hl.bind(mod .. " + Q", hl.dsp.exec_cmd(terminal))' in text
    assert 'hl.bind(mod .. " + B", hl.dsp.exec_cmd(browser))' in text


# --- the error lines ----------------------------------------------------------------------------------------------
def desktop_text(arguments: str) -> str:
    return machine_text(f"profile=Desktop({arguments}),")


class TestErrorLines:
    """One line each, `FILE:LINE:COLUMN: ...`, with no traceback (the S18 cases s01 to s04)."""

    def test_a_quote_in_the_greeting(self, workspace) -> None:
        assert one(workspace(desktop_text('greeting="it\'s"'))).startswith(
            "system.py:7:"
        ) and "Desktop.greeting must be a greeting (1 to 60 letters, digits, spaces, or , . ! ? -)" in one(
            workspace(desktop_text('greeting="it\'s"'))
        )

    def test_a_layout_in_capital_letters(self, workspace) -> None:
        line = one(workspace(desktop_text('keyboard_layout="DE"')))
        assert line.startswith("system.py:7:")
        assert "Desktop.keyboard_layout must be a keyboard layout name such as us, de, or de-latin1" in line

    def test_the_browser_is_a_yes_or_no(self, workspace) -> None:
        line = one(workspace(desktop_text('browser="yes"')))
        assert line.startswith("system.py:7:") and "Desktop.browser must be" in line

    def test_a_locale_that_is_not_utf_8(self, workspace) -> None:
        line = one(workspace(desktop_text('locale="en_US.ISO-8859-1"')))
        assert line.startswith("system.py:7:")
        assert "Desktop.locale must be a UTF-8 locale such as C.UTF-8 or en_US.UTF-8" in line

    def test_a_typo_in_a_setting_gets_a_suggestion(self, workspace) -> None:
        line = one(workspace(desktop_text('greting="Hi"')))
        assert line.startswith("system.py:7:") and "Desktop.greting is not a setting." in line
        assert "Did you mean `greeting`?" in line

    def test_an_extra_program_is_a_program(self, workspace) -> None:
        line = one(workspace(desktop_text('extra_programs=["htop"]')))
        assert line.startswith("system.py:7:")
        assert "Desktop.extra_programs[0] must be a program: Program(pkgs.NAME)" in line

    def test_a_good_desktop_compiles(self, workspace) -> None:
        result = workspace(desktop_text('greeting="Welcome to Basix", keyboard_layout="de"'))
        assert result.code == 0, result.err
        assert result.data["profile"] == "desktop"


# --- L29: the line of `basix apply` -------------------------------------------------------------------------------
def greeter(system: Path, unit: str) -> None:
    """A system whose greetd unit is a link to a store file with this text (it holds the path of the settings)."""
    store = system.parent / f"unit-greetd-{abs(hash(unit))}.service"
    store.write_text(unit)
    target = system / "etc" / "systemd" / "system" / "greetd.service"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    target.symlink_to(store)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return initialized(tmp_path)


def applied_lines(setup: Setup) -> list[str]:
    assert commands.apply(setup.ctx) == 0, setup.lines
    return setup.lines


def test_the_word_is_one_plain_line() -> None:
    assert WORDS.greeter_next_start == LOGIN_LINE
    assert "\n" not in LOGIN_LINE and "/nix/store" not in LOGIN_LINE


def test_a_machine_without_a_login_screen_gets_no_line(setup: Setup) -> None:
    assert LOGIN_LINE not in applied_lines(setup)


def test_a_new_login_screen_is_announced_once(setup: Setup) -> None:
    greeter(setup.world.store("sys-1"), "[Service]\nExecStart=greetd --config /store/a.toml\n")
    lines = applied_lines(setup)
    assert lines.count(LOGIN_LINE) == 1
    assert lines.index(LOGIN_LINE) < lines.index("applied: the system runs, and its units are healthy")


def test_a_changed_login_screen_is_announced(setup: Setup) -> None:
    greeter(Path(setup.world.running() or ""), "[Service]\nExecStart=greetd --config /store/a.toml\n")
    greeter(setup.world.store("sys-1"), "[Service]\nExecStart=greetd --config /store/b.toml\n")
    assert applied_lines(setup).count(LOGIN_LINE) == 1


def test_the_same_login_screen_is_not_announced(setup: Setup) -> None:
    text = "[Service]\nExecStart=greetd --config /store/a.toml\n"
    greeter(Path(setup.world.running() or ""), text)
    greeter(setup.world.store("sys-1"), text)
    assert LOGIN_LINE not in applied_lines(setup)


def test_the_change_check_reads_the_greetd_unit_only(tmp_path: Path) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    new.mkdir()
    assert not greeter_changed(str(old), str(new))
    assert not greeter_changed(None, str(new))
    greeter(new, "a")
    assert greeter_changed(str(old), str(new))
    assert greeter_changed(None, str(new))
    greeter(old, "a")
    assert not greeter_changed(str(old), str(new))
    greeter(old, "b")
    assert greeter_changed(str(old), str(new))
    # a system that drops the login screen has nothing to announce
    assert not greeter_changed(str(new), str(tmp_path / "gone"))
