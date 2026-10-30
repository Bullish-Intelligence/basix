"""`basix explain` (009-D14): the lines for a field, a capability, and an option path, with a fake runner.

The real evaluation of a fixture is the case `explain` of `scripts/check_eval.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pynix_engine import Result
from pynix_engine.testing import FakeRunner

from basix.explain import explain, expression

PROVENANCE = {
    "Console": [],
    "RemoteLogin": ["services.openssh.enable", "services.openssh.settings.AllowUsers"],
    "caches": [],
    "disk": ["basix.disk.device"],
    "name": ["networking.hostName"],
    "programs": [],
    "trusted_interfaces": ["networking.firewall.trustedInterfaces"],
    "user": ["basix.user"],
}
STORE = "/nix/store/0123456789abcdfghijklmnpqrsvwxyz-source/"
FIREWALL = STORE + "nixos/modules/services/networking/firewall.nix"
CORE = "/nix/store/zyxwvsrqpnmlkjhgfdcba9876543210z-source/modules/core.nix"


def workspace(tmp_path: Path, provenance: dict | None = PROVENANCE) -> Path:
    data: dict = {"name": "box", "profile": "console", "fragments": [], "modules": []}
    if provenance is not None:
        data["provenance"] = provenance
    (tmp_path / "system.json").write_text(json.dumps(data))
    return tmp_path


def known(name: str, *files: str, value: object = None, shown: bool = True, option: str | None = None) -> dict:
    return {"name": name, "known": True, "option": option or name, "shown": shown, "value": value, "files": list(files)}


def answer(runner: FakeRunner, *items: dict) -> None:
    runner.on(("nix",), Result(0, "warning: Git tree is dirty\n" + json.dumps(list(items)) + "\n"))


def run(
    folder: Path, name: str, runner: FakeRunner, capsys: pytest.CaptureFixture[str]
) -> tuple[int, list[str], list[str]]:
    code = explain(folder, name, runner=runner)
    seen = capsys.readouterr()
    return code, seen.out.splitlines(), seen.err.splitlines()


def test_a_field_shows_the_value_and_the_learner_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    answer(
        runner,
        known("networking.firewall.trustedInterfaces", "system.py:9 (Machine)", FIREWALL, value=["enp1s0", "lo"]),
    )
    code, out, err = run(workspace(tmp_path), "trusted_interfaces", runner, capsys)
    assert (code, err) == (0, [])
    assert out == [
        'networking.firewall.trustedInterfaces = ["enp1s0", "lo"]',
        "  set by trusted_interfaces at system.py:9",
        "  set by NixOS (services/networking/firewall.nix)",
    ]


def test_a_line_of_a_helper_module_shows_its_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    answer(
        runner,
        known(
            "networking.firewall.trustedInterfaces",
            "helpers/net.py:4 (Machine)",
            "comps/desk.py:12 (RemoteLogin)",
            "/nix/store/0123456789abcdfghijklmnpqrsvwxyz-source/odd/x.py:3 (Machine)",
            FIREWALL,
            value=["lo"],
        ),
    )
    code, out, err = run(workspace(tmp_path), "trusted_interfaces", runner, capsys)
    assert (code, err) == (0, [])
    assert out == [
        'networking.firewall.trustedInterfaces = ["lo"]',
        "  set by trusted_interfaces at helpers/net.py:4",
        "  set by RemoteLogin at comps/desk.py:12",
        "  set by NixOS (services/networking/firewall.nix)",
        "  set by the module odd/x.py:3 (Machine)",
    ]


def test_one_nix_call_asks_for_all_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    answer(
        runner,
        known("services.openssh.enable", "system.py:7 (RemoteLogin)", value=True),
        known(
            "services.openssh.settings.AllowUsers",
            "system.py:7 (RemoteLogin)",
            value=["ada"],
            option="services.openssh.settings",
        ),
    )
    code, out, _err = run(workspace(tmp_path), "RemoteLogin", runner, capsys)
    assert code == 0
    assert out == [
        "services.openssh.enable = true",
        "  set by RemoteLogin at system.py:7",
        'services.openssh.settings.AllowUsers = ["ada"]',
        "  set by RemoteLogin at system.py:7",
    ]
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:5] == ("nix", "--extra-experimental-features", "nix-command flakes", "eval", "--json")
    assert argv[5] == ".#nixosConfigurations.box"
    assert argv[6] == "--apply"
    assert '["services.openssh.enable","services.openssh.settings.AllowUsers"]' in argv[7].replace('\\"', '"').replace(
        ", ", ","
    )
    assert runner.cwds == [tmp_path]


def test_the_core_prints_basix_and_never_a_priority_word(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    answer(runner, known("basix.user", CORE, value="ada"))
    code, out, err = run(workspace(tmp_path), "basix.user", runner, capsys)
    assert (code, err) == (0, [])
    assert out == ['basix.user = "ada"', "  set by Basix"]
    for word in ("priority", "mkDefault", "mkForce", "override"):
        assert word not in "\n".join(out).lower()


def test_an_option_path_with_no_definition_is_the_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    answer(runner, known("services.openssh.ports", value=[22]))
    code, out, _err = run(workspace(tmp_path), "services.openssh.ports", runner, capsys)
    assert code == 0
    assert out == ["services.openssh.ports = [22]", "  no line sets it: the value is the default"]


def test_many_nixos_modules_are_counted(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    files = [f"{STORE}nixos/modules/m{i}.nix" for i in range(5)]
    answer(runner, known("warnings", *files, "workspace/fans.nix", value=[]))
    code, out, _err = run(workspace(tmp_path), "warnings", runner, capsys)
    assert code == 0
    assert out == ["warnings = []", "  set by NixOS (5 modules)", "  set by the module workspace/fans.nix"]


def test_a_long_list_and_a_value_that_nix_cannot_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    answer(
        runner,
        known("environment.systemPackages", value=[f"p{i}" for i in range(12)]),
        known("fileSystems.x", shown=False),
    )
    code, out, _err = run(workspace(tmp_path), "environment.systemPackages", runner, capsys)
    assert code == 0
    assert out[0].endswith(', "p7", ... 4 more]')
    assert out[2] == "fileSystems.x = (Nix cannot show it)"


def test_a_field_that_sets_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    code, out, _err = run(workspace(tmp_path), "programs", runner, capsys)
    assert (code, out, runner.calls) == (0, ["`programs` sets no option on this machine."], [])


def test_an_unknown_name_gets_one_line_with_did_you_mean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    code, out, err = run(workspace(tmp_path), "trusted_interface", runner, capsys)
    assert (code, out, runner.calls) == (1, [], [])
    assert err == [
        "`trusted_interface` is not a field, a capability, or an option path of this machine. Did you mean `trusted_interfaces`?"
    ]
    code, out, err = run(workspace(tmp_path), "has space", runner, capsys)
    assert (
        code == 1 and len(err) == 1 and err[0].startswith("`has space` is not a field") and "The names are:" in err[0]
    )


def test_a_capability_that_the_machine_lacks(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    only = {k: v for k, v in PROVENANCE.items() if k != "RemoteLogin"}
    runner = FakeRunner()
    code, _out, err = run(workspace(tmp_path, only), "RemoteLogin", runner, capsys)
    assert (code, runner.calls) == (1, [])
    assert err == ["`RemoteLogin` is not in the `capabilities` of this machine."]


def test_a_path_that_is_no_option_gets_did_you_mean(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    near = {"prefix": ["services", "openssh"], "names": ["enable", "ports", "settings"]}
    answer(runner, {"name": "services.openssh.enabel", "known": False, "near": near})
    code, out, err = run(workspace(tmp_path), "services.openssh.enabel", runner, capsys)
    assert (code, out) == (1, [])
    assert err == [
        "`services.openssh.enabel` is not an option of this machine. Did you mean `services.openssh.enable`?"
    ]


def test_a_prefix_of_options_lists_the_names_below_it(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    near = {"prefix": ["services"], "names": ["openssh", "restic", "resolved"]}
    answer(runner, {"name": "services.restic", "known": False, "near": near, "children": ["server", "backups"]})
    code, out, err = run(workspace(tmp_path), "services.restic", runner, capsys)
    assert (code, out) == (1, [])
    assert err == [
        "`services.restic` is not an option of this machine."
        " It holds these options: `services.restic.backups`, `services.restic.server`."
    ]
    assert "Did you mean" not in err[0]


def test_a_long_list_of_children_is_cut(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    names = [f"n{i}" for i in range(9)]
    answer(runner, {"name": "x", "known": False, "near": {"prefix": [], "names": names}, "children": names})
    _code, _out, err = run(workspace(tmp_path), "x", runner, capsys)
    assert err[0].endswith("`x.n5`, and 3 more.")


def test_a_missing_path_never_suggests_itself(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    near = {"prefix": ["services"], "names": ["restic"]}
    answer(runner, {"name": "services.restic", "known": False, "near": near})
    code, _out, err = run(workspace(tmp_path), "services.restic", runner, capsys)
    assert (code, err) == (1, ["`services.restic` is not an option of this machine."])


def test_a_nix_error_prints_the_cause(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    runner.on(("nix",), Result(1, "warning: x\nerror: system.py changed: run `basix apply`\n"))
    code, out, err = run(workspace(tmp_path), "user", runner, capsys)
    assert (code, out) == (1, [])
    assert err == ["error: system.py changed: run `basix apply`"]


def test_no_json_and_old_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    runner = FakeRunner()
    code, _out, err = run(tmp_path, "user", runner, capsys)
    assert (code, err) == (1, ["There is no system.json yet. Run `basix apply`, then `basix explain`."])
    code, _out, err = run(workspace(tmp_path, None), "user", runner, capsys)
    assert (code, err) == (1, ["system.json holds no list of settings. Run `basix apply` again."])
    assert runner.calls == []


def test_the_expression_escapes_the_paths() -> None:
    """The Nix string holds the JSON list. Reading the Nix escapes back gives the same list."""
    paths = ['a"b', "c${d}", "e\\f"]
    text = expression(paths)
    body = text.split('builtins.fromJSON "', 1)[1].split('";\n', 1)[0]
    assert "${" not in body.replace("\\${", "")  # every `${` has a backslash before it
    unescaped = body.replace("\\${", "${")
    assert json.loads(json.loads(f'"{unescaped}"')) == paths
