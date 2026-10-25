"""The command line: the four commands, their options, and the exit codes (the work itself is in `test_cmd_*`)."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from basix import __version__, cli, commands
from basix.cli import app

runner = CliRunner()


def test_help_lists_the_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for word in ("--version", "install", "apply", "undo", "update", "explain"):
        assert word in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.strip() == f"basix {__version__}"


def test_no_arguments_show_the_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.output


class Calls:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, code: int = 0) -> None:
        self.seen: list[tuple[str, tuple[object, ...]]] = []
        self.code = code
        monkeypatch.setattr(commands, "real_context", lambda: "context")
        for name in ("apply", "undo", "update", "explain"):
            monkeypatch.setattr(commands, name, self.recorder(name))

    def recorder(self, name: str):
        def call(*args: object) -> int:
            self.seen.append((name, args))
            return self.code

        return call


def test_apply_passes_the_folder_and_the_verbose_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls(monkeypatch)
    assert runner.invoke(app, ["apply", "--dir", "/tmp/w", "--verbose"]).exit_code == 0
    assert calls.seen == [("apply", ("context", Path("/tmp/w"), True))]


def test_apply_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls(monkeypatch)
    assert runner.invoke(app, ["apply"]).exit_code == 0
    assert calls.seen == [("apply", ("context", None, False))]


def test_undo_passes_the_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls(monkeypatch)
    assert runner.invoke(app, ["undo", "--dir", "/tmp/w"]).exit_code == 0
    assert calls.seen == [("undo", ("context", Path("/tmp/w"), False))]


def test_update_takes_the_name_of_an_input(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls(monkeypatch)
    assert runner.invoke(app, ["update", "nirinit"]).exit_code == 0
    assert runner.invoke(app, ["update"]).exit_code == 0
    assert [args[1] for _, args in calls.seen] == ["nirinit", "basix"]


def test_explain_takes_a_name_and_a_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = Calls(monkeypatch)
    assert runner.invoke(app, ["explain", "RemoteLogin"]).exit_code == 0
    assert runner.invoke(app, ["explain", "trusted_interfaces", "--dir", "/tmp/w"]).exit_code == 0
    assert calls.seen == [
        ("explain", ("context", "RemoteLogin", None)),
        ("explain", ("context", "trusted_interfaces", Path("/tmp/w"))),
    ]


def test_explain_needs_a_name() -> None:
    assert runner.invoke(app, ["explain"]).exit_code != 0


@pytest.mark.parametrize("command", ["apply", "undo", "update"])
@pytest.mark.parametrize("code", [1, 2, 3, 4])
def test_the_exit_code_of_a_command_is_the_exit_code_of_basix(
    monkeypatch: pytest.MonkeyPatch, command: str, code: int
) -> None:
    Calls(monkeypatch, code)
    assert runner.invoke(app, [command]).exit_code == code


def test_the_program_entry_is_the_app() -> None:
    assert cli.app is app
