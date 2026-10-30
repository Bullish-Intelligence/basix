from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from pynix_engine import Result
from pynix_native import discover
from test_units import CYCLE

from basix_config.stub import make_stub

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "try_vm.py"
SPEC = importlib.util.spec_from_file_location("try_vm", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
flake_text = MODULE.flake_text
vm_module = MODULE.vm_module
workspace_for = MODULE.workspace_for
machine_user = MODULE.machine_user


def test_workspace_for_finds_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    child = workspace / "nested"
    child.mkdir(parents=True)
    (workspace / "system.py").write_text("machine = None\n")

    assert workspace_for(child) == workspace


def test_workspace_for_requires_system_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="no system.py"):
        workspace_for(tmp_path)


def test_machine_user_reads_basix_fragment() -> None:
    assert machine_user({"fragments": [{"path": ["basix"], "value": {"user": "learner"}}]}) == "learner"


def test_machine_user_requires_basix_account() -> None:
    with pytest.raises(ValueError, match="no basix.user account"):
        machine_user({"fragments": []})


def test_flake_uses_lib_machine_and_vm_module(tmp_path: Path) -> None:
    text = flake_text(tmp_path / "basix")

    assert 'inputs.basix.url = "path:' in text
    assert "basix.lib.machine" in text
    assert "nixosConfigurations =" in text
    assert "extraModules = [ ./vm.nix" in text
    assert "compiled = builtins.fromJSON" in text


def test_flake_keeps_minix_as_a_separate_workspace_input(tmp_path: Path) -> None:
    text = flake_text(tmp_path / "basix", tmp_path / "minix", "ada")

    assert 'inputs.home.url = "path:' in text
    assert 'homeActivation = home.homeConfigurations."ada".activationPackage;' in text
    assert 'ExecStart = "${homeActivation}/activate";' in text
    assert 'Environment = "PATH=/run/current-system/sw/bin";' in text
    assert "inputs.minix" not in text


INPUT_ENTRY = """\
from basix_config import Input

example = Input("example-host", "github:example/example-host", modules=["default"])
"""


def input_stub(tmp_path: Path):
    """The stub of an entry with one `Input`. The fake reads `nixpkgs` as the only input of the input."""
    entry = tmp_path / "entry.py"
    entry.write_text(INPUT_ENTRY)
    return make_stub(discover(entry), "path:/basix", lambda url: ["nixpkgs"])


def test_flake_reads_an_input_like_the_stub_of_apply(tmp_path: Path) -> None:
    stub = input_stub(tmp_path)
    text = flake_text(tmp_path / "basix", stub=stub)

    assert 'inputs.example-host.url = "github:example/example-host";' in text
    assert 'inputs.example-host.inputs.nixpkgs.follows = "nixpkgs";' in text
    assert 'inputs.nixpkgs.follows = "basix/nixpkgs";' in text
    assert "outputs = inputs@{ self, basix, ... }:" in text
    assert 'extraInputs = removeAttrs inputs [ "self" "basix" "nixpkgs" ];' in text
    assert "packages.x86_64-linux.input-packages = basix.lib.inputPackages {" in text
    # The extra module of the VM stays in the machine of the flake.
    assert "extraModules = [ ./vm.nix" in text
    # Every line of the stub inputs is in the flake of the stub, so the lock of one fits the other.
    assert all(f"  {line}\n" in stub.text for line in stub.inputs)


def test_flake_with_an_input_and_a_home_keeps_the_home_out_of_the_extra_inputs(tmp_path: Path) -> None:
    text = flake_text(tmp_path / "basix", tmp_path / "minix", "ada", input_stub(tmp_path))

    assert "outputs = inputs@{ self, basix, home, ... }:" in text
    assert 'removeAttrs inputs [ "self" "basix" "nixpkgs" "home" ]' in text
    assert 'inputs.example-host.url = "github:example/example-host";' in text


def test_flake_without_an_input_has_no_extra_inputs(tmp_path: Path) -> None:
    text = flake_text(tmp_path / "basix", stub=make_stub(None, "path:/basix"))

    assert "extraInputs" not in text
    assert "input-packages" not in text
    assert "outputs = { self, basix }:" in text


def test_vm_login_uses_the_machine_account() -> None:
    text = vm_module("ada")

    assert 'users.users."ada"' in text
    assert 'autologinUser = "ada";' in text


def test_vm_screen_is_an_explicit_choice() -> None:
    text = vm_module("ada", screen=True)

    assert "graphics = true;" in text


# --- main(): paths, run folders, --home, and the stop of QEMU. A fake runner stands in for Nix and QEMU. ---


class FakeNix:
    """Replace `run` and `boot`. Record each call. Write the files that the real tools would write."""

    def __init__(self, compiled: dict) -> None:
        self.compiled = compiled
        self.calls: list[list[str]] = []
        self.logs: list[Path] = []
        self.boots: list[tuple[Path, Path, Path]] = []

    def run(self, argv: list[str], *, cwd: Path, log: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        self.logs.append(log)
        out = ""
        if argv[0].endswith("basix-compile"):
            (cwd / "system.json").write_text(json.dumps(self.compiled))
        elif argv[0].endswith("minix-compile"):
            (cwd / "home.json").write_text(json.dumps({"user": "ada"}))
        elif argv[:2] == ["nix", "build"] and "compiler" in argv[2]:
            out = f"{MODULE.STORE}aaa-compiler\n"
        elif argv[:2] == ["nix", "eval"]:
            out = "/nix/store/bbb.drv"
        elif argv[:2] == ["nix", "build"]:
            out = str(self.vm_output) + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")

    def boot(self, runner: Path, run_dir: Path, vm_log_path: Path) -> int:
        self.boots.append((runner, run_dir, vm_log_path))
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "vm.qcow2").write_text("disk")
        return 0

    vm_output = Path()


def machine_json(user: str | None = "ada") -> dict:
    fragments = [{"path": ["basix"], "value": {"user": user}}] if user else []
    return {"name": "box", "fragments": fragments}


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeNix:
    nix = FakeNix(machine_json())
    nix.vm_output = tmp_path / "vm-out"
    (nix.vm_output / "bin").mkdir(parents=True)
    (nix.vm_output / "bin" / "run-box-vm").write_text("")
    # No test reads the inputs of an input through Nix.
    monkeypatch.setattr(MODULE, "make_stub", lambda found, url: make_stub(found, url, lambda _: ["nixpkgs"]))
    monkeypatch.setattr(MODULE, "STORE", str(tmp_path) + "/")
    monkeypatch.setattr(MODULE, "run", nix.run)
    monkeypatch.setattr(MODULE, "boot", nix.boot)
    monkeypatch.setenv("BASIX_TRY_WORK", str(tmp_path / "work"))
    monkeypatch.setenv("BASIX_TRY_LOGS", str(tmp_path / "logs"))
    return nix


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "system.py").write_text("machine = None\n")
    return workspace


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "home-ws"
    home.mkdir()
    for name in ("home.py", "flake.nix", "flake.lock"):
        (home / name).write_text("")
    return home


def test_work_and_logs_follow_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BASIX_TRY_WORK", str(tmp_path / "w"))
    monkeypatch.setenv("BASIX_TRY_LOGS", str(tmp_path / "l"))

    assert MODULE.work_root() == tmp_path / "w"
    assert MODULE.logs_root() == tmp_path / "l"


def test_work_and_logs_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("BASIX_TRY_WORK", raising=False)
    monkeypatch.delenv("BASIX_TRY_LOGS", raising=False)
    monkeypatch.delenv("BASIX_WORK_BASE", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(MODULE.Path, "home", lambda: tmp_path)

    assert MODULE.logs_root() == tmp_path / ".local" / "state" / "basix" / "try"
    assert MODULE.work_root() == tmp_path / ".cache" / "basix" / "try"


def test_main_refuses_a_folder_with_no_system_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert MODULE.main(["--dir", str(tmp_path)]) == 2
    assert "no system.py" in capsys.readouterr().err


def test_main_refuses_an_unknown_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        MODULE.main(["--bogus"])

    assert stop.value.code == 2
    assert "--bogus" in capsys.readouterr().err


def test_main_refuses_a_home_with_missing_files(
    tmp_path: Path, fake: FakeNix, capsys: pytest.CaptureFixture[str]
) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()

    assert MODULE.main(["--dir", str(make_workspace(tmp_path)), "--home", str(home)]) == 2
    assert "has no home.py" in capsys.readouterr().err
    assert fake.calls == []


def test_main_boots_with_the_paths_of_the_run(tmp_path: Path, fake: FakeNix) -> None:
    assert MODULE.main(["--dir", str(make_workspace(tmp_path))]) == 0

    ((runner, run_dir, vm_log),) = fake.boots
    assert runner == fake.vm_output / "bin" / "run-box-vm"
    work_run = run_dir.parent
    assert work_run.parent == tmp_path / "work"
    assert vm_log.parent.parent == tmp_path / "logs"
    assert vm_log.parent.name == work_run.name
    assert all(log.parent == vm_log.parent for log in fake.logs)
    assert 'users.users."ada"' in (work_run / "workspace" / "vm.nix").read_text()
    # The disk of the VM is throwaway. The workspace stays for a look.
    assert not run_dir.exists()


def test_main_locks_and_builds_the_inputs_before_it_compiles(tmp_path: Path, fake: FakeNix) -> None:
    workspace = make_workspace(tmp_path)
    (workspace / "system.py").write_text(INPUT_ENTRY)

    assert MODULE.main(["--dir", str(workspace)]) == 0

    ((_, run_dir, _),) = fake.boots
    scratch = run_dir.parent / "workspace"
    steps = [call[:3] if call[0] == "nix" else [Path(call[0]).name] for call in fake.calls]
    lock = steps.index(["nix", "flake", "lock"])
    packages = next(i for i, call in enumerate(fake.calls) if call[-1].endswith("#input-packages"))
    compile_ = next(i for i, call in enumerate(fake.calls) if call[0].endswith("basix-compile"))
    assert lock < packages < compile_
    # The compiler reads the input packages from `.basix/pynix` in the scratch workspace.
    assert fake.calls[packages][fake.calls[packages].index("--out-link") + 1] == str(scratch / ".basix" / "pynix")
    # The final flake keeps the inputs and passes them on.
    flake = (scratch / "flake.nix").read_text()
    assert 'inputs.example-host.url = "github:example/example-host";' in flake
    assert "extraInputs = removeAttrs inputs" in flake and "extraModules = [ ./vm.nix" in flake


def test_main_without_an_input_builds_no_input_packages(tmp_path: Path, fake: FakeNix) -> None:
    assert MODULE.main(["--dir", str(make_workspace(tmp_path))]) == 0

    assert not any(call[-1].endswith("#input-packages") for call in fake.calls)
    assert [call[:3] for call in fake.calls].count(["nix", "flake", "lock"]) == 1


def test_main_stops_when_the_inputs_fail_to_lock(
    tmp_path: Path, fake: FakeNix, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = make_workspace(tmp_path)
    (workspace / "system.py").write_text(INPUT_ENTRY)
    run = fake.run

    def failing(argv: list[str], *, cwd: Path, log: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        done = run(argv, cwd=cwd, log=log, timeout=timeout)
        return subprocess.CompletedProcess(argv, 1 if argv[:3] == ["nix", "flake", "lock"] else 0, done.stdout, "")

    monkeypatch.setattr(MODULE, "run", failing)

    assert MODULE.main(["--dir", str(workspace)]) == 1
    assert "input lock failed" in capsys.readouterr().err
    assert fake.boots == []


class AnalyzeStub:
    """Stands in for `SubprocessRunner`. It records the command and returns the text of `systemd-analyze verify`."""

    commands: list[list[str]]

    def __init__(self, out: str) -> None:
        self.out = out
        self.commands = []

    def __call__(self) -> AnalyzeStub:
        return self

    def run(self, argv: list[str], *, cwd: Path | None = None) -> Result:
        self.commands.append(list(argv))
        return Result(0, self.out)


def add_default_target(fake: FakeNix) -> Path:
    """Give the built VM system a `default.target`, so the cycle check runs. Return the toplevel."""
    top = fake.vm_output / "system"
    (top / "etc" / "systemd" / "system").mkdir(parents=True)
    (top / "etc" / "systemd" / "system" / "default.target").write_text("")
    return top


def test_main_refuses_to_boot_a_system_with_a_systemd_cycle(
    tmp_path: Path, fake: FakeNix, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    top = add_default_target(fake)
    analyze = AnalyzeStub(CYCLE)
    monkeypatch.setattr(MODULE, "SubprocessRunner", analyze)

    code = MODULE.main(["--dir", str(make_workspace(tmp_path))])

    captured = capsys.readouterr()
    assert code == 1
    assert captured.err == (
        "basix-try: the system has 3 systemd ordering cycle(s), so the VM would boot in a random order.\n"
        "  cycle 1: local-fs.target, data-store.mount, data-store-format.service\n"
        "  cycle 2: systemd-boot-random-seed.service, local-fs.target, data-store.mount, data-store-format.service, sysinit.target\n"
        "  cycle 3: systemd-binfmt.service, local-fs.target, data-store.mount, data-store-format.service, sysinit.target\n"
        "Fix the order of these units. The VM did not start.\n"
    )
    assert "booting" not in captured.out
    assert fake.boots == []
    # The check reads the VM system, and only `default.target` of it.
    (command,) = analyze.commands
    assert command[-2:] == ["verify", str(top / "etc" / "systemd" / "system" / "default.target")]


def test_main_boots_a_system_that_systemd_finds_without_a_cycle(
    tmp_path: Path, fake: FakeNix, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_default_target(fake)
    analyze = AnalyzeStub("nscd.service: Command 'x' is not executable: No such file or directory\n")
    monkeypatch.setattr(MODULE, "SubprocessRunner", analyze)

    assert MODULE.main(["--dir", str(make_workspace(tmp_path))]) == 0

    assert len(analyze.commands) == 1
    assert len(fake.boots) == 1


def test_two_runs_keep_their_own_folders(tmp_path: Path, fake: FakeNix, monkeypatch: pytest.MonkeyPatch) -> None:
    ids = iter(["run-a", "run-b"])
    monkeypatch.setattr(MODULE, "new_run_id", lambda: next(ids))
    workspace = make_workspace(tmp_path)

    assert MODULE.main(["--dir", str(workspace)]) == 0
    first = tmp_path / "work" / "run-a" / "workspace" / "vm.nix"
    assert first.is_file()
    assert MODULE.main(["--dir", str(workspace)]) == 0

    assert first.is_file()
    assert (tmp_path / "work" / "run-b" / "workspace" / "vm.nix").is_file()
    assert (tmp_path / "logs" / "run-a" / "compile.log").parent.is_dir()
    assert (tmp_path / "logs" / "run-b").is_dir()


def test_run_id_holds_the_process_number(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(MODULE.os, "getpid", lambda: 4711)

    assert MODULE.new_run_id().endswith("-4711")


def test_home_is_refused_when_the_machine_has_no_user(
    tmp_path: Path, fake: FakeNix, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.compiled = machine_json(user=None)

    code = MODULE.main(["--dir", str(make_workspace(tmp_path)), "--home", str(make_home(tmp_path))])

    err = capsys.readouterr().err.strip().splitlines()
    assert code == 2
    assert err == ["basix-try: --home needs a machine with a user; system.py sets no user"]
    # The refusal comes before the slow minix build.
    assert not any("minix-compile" in call[0] for call in fake.calls)
    assert fake.boots == []


def test_no_user_without_home_exits_2(tmp_path: Path, fake: FakeNix, capsys: pytest.CaptureFixture[str]) -> None:
    fake.compiled = machine_json(user=None)

    assert MODULE.main(["--dir", str(make_workspace(tmp_path))]) == 2
    assert "no basix.user account" in capsys.readouterr().err


def test_home_user_must_match_the_machine_user(
    tmp_path: Path, fake: FakeNix, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.compiled = machine_json(user="bob")

    assert MODULE.main(["--dir", str(make_workspace(tmp_path)), "--home", str(make_home(tmp_path))]) == 2
    assert "--home names 'ada', but system.py names 'bob'" in capsys.readouterr().err
    assert fake.boots == []


def test_home_adds_the_input_to_the_flake(tmp_path: Path, fake: FakeNix) -> None:
    assert MODULE.main(["--dir", str(make_workspace(tmp_path)), "--home", str(make_home(tmp_path))]) == 0

    ((_, run_dir, _),) = fake.boots
    flake = (run_dir.parent / "workspace" / "flake.nix").read_text()
    assert 'inputs.home.url = "path:' in flake
    assert 'homeConfigurations."ada"' in flake
    assert any("minix-compile" in call[0] for call in fake.calls)


class FakeVm:
    """A QEMU stand-in. `wait` raises a timeout for each name in `timeouts`, then it returns."""

    pid = 4242

    def __init__(self, timeouts: int) -> None:
        self.timeouts = timeouts
        self.waits: list[int | None] = []

    def poll(self) -> None:
        return None

    def wait(self, timeout: int | None = None) -> int:
        self.waits.append(timeout)
        if self.timeouts:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired("qemu", timeout or 0)
        return 0


def test_stop_vm_sends_sigkill_after_the_wait_expires(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(MODULE.os, "killpg", lambda pid, sig: signals.append((pid, sig)))
    vm = FakeVm(timeouts=1)

    MODULE.stop_vm(vm)  # type: ignore[arg-type]

    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert vm.waits == [MODULE.STOP_WAIT_S, None]
    err = capsys.readouterr().err.strip().splitlines()
    assert err == [f"basix-try: QEMU still ran after {MODULE.STOP_WAIT_S} s; sent SIGKILL"]


def test_stop_vm_sends_no_sigkill_when_qemu_stops(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(MODULE.os, "killpg", lambda pid, sig: signals.append((pid, sig)))

    MODULE.stop_vm(FakeVm(timeouts=0))  # type: ignore[arg-type]

    assert signals == [(4242, signal.SIGTERM)]
    assert capsys.readouterr().err == ""


def test_boot_gives_the_vm_a_short_tmpdir_and_removes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    long_work = tmp_path / "work" / ("a-long-folder-name-to-pass-the-socket-limit-" * 3)
    run_dir = long_work / "run-id" / "run"
    logs = tmp_path / "logs"
    logs.mkdir()
    runner = tmp_path / "run-fake-vm"
    # The fake runner saves its `TMPDIR` and `NIX_DISK_IMAGE`, and checks that the folder exists.
    runner.write_text('#!/bin/sh\ntest -d "$TMPDIR" && printf "%s\\n%s\\n" "$TMPDIR" "$NIX_DISK_IMAGE" > seen.txt\n')
    runner.chmod(0o755)
    monkeypatch.setattr(MODULE.sys, "stdin", open(os.devnull))  # noqa: SIM115

    assert MODULE.boot(runner, run_dir, logs / "qemu.log") == 0

    vm_tmp, image = (run_dir / "seen.txt").read_text().splitlines()
    assert len(run_dir.parent.parent.as_posix()) > 108
    assert len(vm_tmp) < 40
    # The disk image stays in the run folder. Only `TMPDIR` moves.
    assert image == str(run_dir / "vm.qcow2")
    assert not Path(vm_tmp).exists()


def test_boot_removes_the_tmpdir_when_the_run_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Path] = []

    def fail(runner: Path, run_dir: Path, vm_log_path: Path, vm_tmp: Path) -> int:
        seen.append(vm_tmp)
        assert vm_tmp.is_dir()
        raise RuntimeError("boom")

    monkeypatch.setattr(MODULE, "_boot", fail)

    with pytest.raises(RuntimeError):
        MODULE.boot(tmp_path / "runner", tmp_path / "run", tmp_path / "qemu.log")

    assert not seen[0].exists()


# --- the input packages of the home workspace (011 bug 3) ---
HOME_WITH_INPUTS = "{ packages.x86_64-linux.input-packages = minix.lib.inputPackages { }; }\n"


def test_the_home_needs_its_packages_only_when_its_stub_defines_them() -> None:
    assert MODULE.home_needs_packages(HOME_WITH_INPUTS) is True
    assert MODULE.home_needs_packages("{ outputs = { self, minix }: { }; }\n") is False
    assert MODULE.home_needs_packages("") is False


def test_home_with_inputs_builds_its_packages_before_the_home_compiler(tmp_path: Path, fake: FakeNix) -> None:
    home = make_home(tmp_path)
    (home / "flake.nix").write_text(HOME_WITH_INPUTS)
    (home / ".basix").mkdir()
    (home / ".basix" / "pynix").symlink_to(tmp_path / "stale-store-path")  # the link of an earlier `minix apply`

    assert MODULE.main(["--dir", str(make_workspace(tmp_path)), "--home", str(home)]) == 0

    ((_, run_dir, _),) = fake.boots
    scratch = run_dir.parent / "home"
    packages = [i for i, call in enumerate(fake.calls) if call[-1] == f"path:{scratch}#input-packages"]
    compile_ = next(i for i, call in enumerate(fake.calls) if call[0].endswith("minix-compile"))
    assert len(packages) == 1 and packages[0] < compile_
    call = fake.calls[packages[0]]
    assert call[call.index("--out-link") + 1] == str(scratch / ".basix" / "pynix")


def test_home_without_inputs_builds_no_packages(tmp_path: Path, fake: FakeNix) -> None:
    assert MODULE.main(["--dir", str(make_workspace(tmp_path)), "--home", str(make_home(tmp_path))]) == 0

    assert not any(call[-1].endswith("#input-packages") for call in fake.calls)


def test_home_stops_when_its_packages_fail_to_build(
    tmp_path: Path, fake: FakeNix, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    home = make_home(tmp_path)
    (home / "flake.nix").write_text(HOME_WITH_INPUTS)
    run = fake.run

    def failing(argv: list[str], *, cwd: Path, log: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        if argv[-1].endswith("#input-packages"):
            return subprocess.CompletedProcess(argv, 3, "", "error")
        return run(argv, cwd=cwd, log=log, timeout=timeout)

    monkeypatch.setattr(MODULE, "run", failing)
    assert MODULE.main(["--dir", str(make_workspace(tmp_path)), "--home", str(home)]) == 3
    assert "home input packages build failed" in capsys.readouterr().err
    assert fake.boots == []
