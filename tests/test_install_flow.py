"""Fixtures for the install steps: the disk list, the erase, the order of the commands, and the failure text."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from basix import install as installer

DISK = "/dev/disk/by-id/disk0"
GIB = 1_000_000_000


@pytest.fixture(autouse=True)
def _no_host_bluetooth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the pairing state of the machine that runs the tests."""
    monkeypatch.setattr(installer, "BLUETOOTH", tmp_path / "no-live-bluetooth")


class FakeRun:
    """A runner that records every command. `answers` maps a command prefix to (return code, standard output)."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, argv: list[str], *, check: bool = True, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        for prefix, (code, out) in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                if check and code:
                    raise installer.InstallError(f"command failed: {argv[0]} (exit {code})")
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]

    def names(self) -> list[str]:
        return [argv[0] for argv in self.argvs()]


def _used_disk_answers() -> dict[tuple[str, ...], tuple[int, str]]:
    return {
        ("lsblk", "-nro", "NAME"): (0, "disk0\ndisk0p1\ndisk0p2\n"),
        ("wipefs", "--noheadings"): (0, "gpt\n"),
        ("lsblk", "-nrpo", "PATH"): (0, "/dev/disk0\n/dev/disk0p1\n/dev/disk0p2\n"),
        ("lsblk", "-o"): (0, "NAME SIZE\ndisk0 100G\n"),
    }


def test_erase_wipes_the_partitions_then_the_disk_then_the_table() -> None:
    run = FakeRun(_used_disk_answers())
    installer.erase(DISK, run)
    wipes = [argv[-1] for argv in run.argvs() if argv[0] == "wipefs"]
    assert wipes == ["/dev/disk0p2", "/dev/disk0p1", DISK]
    names = run.names()
    assert names.index("sgdisk") > max(i for i, name in enumerate(names) if name == "wipefs")
    assert run.argvs()[names.index("sgdisk")] == ["sgdisk", "--zap-all", DISK]
    assert names[-2:] == ["partprobe", "udevadm"]


def test_used_disk_lists_the_disk_and_requires_erase_before_any_write(capsys: pytest.CaptureFixture[str]) -> None:
    run = FakeRun(_used_disk_answers())
    with pytest.raises(installer.InstallError, match=r"erase refused\. No disk was changed\."):
        installer._prepare_disk(DISK, run, lambda _: "no")
    out = capsys.readouterr().out
    assert out.startswith("This disk already contains data:\nNAME SIZE\ndisk0 100G\n")
    assert "earlier Basix install" not in out
    assert ["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL,PARTLABEL,MOUNTPOINTS", DISK] in run.argvs()
    assert not {"sgdisk", "partprobe"} & set(run.names())
    assert not any(argv[0] == "wipefs" and "--all" in argv for argv in run.argvs())


def test_used_disk_names_an_earlier_basix_install_by_the_partition_labels(capsys: pytest.CaptureFixture[str]) -> None:
    answers = _used_disk_answers() | {("lsblk", "-nro", "PARTLABEL"): (0, "\ndisk-main-ESP\ndisk-main-root\n")}
    run = FakeRun(answers)
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "erase"

    assert installer._prepare_disk(DISK, run, ask) is True
    out = capsys.readouterr().out
    assert "This may be an earlier Basix install, including one that did not finish.\n" in out
    assert prompts == ["Type erase to destroy everything on this disk: "]
    assert "sgdisk" in run.names()


def test_a_blank_disk_is_not_offered_for_erase() -> None:
    run = FakeRun({("lsblk", "-nro", "NAME"): (0, "disk0\n"), ("wipefs", "--noheadings"): (0, "")})

    def ask(prompt: str) -> str:
        raise AssertionError(prompt)

    assert installer._prepare_disk(DISK, run, ask) is False


def _by_id(tmp_path: Path, names: list[str]) -> Path:
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    for name in names:
        device = tmp_path / f"dev-{name}"
        device.write_text("")
        (by_id / name).symlink_to(device)
    return by_id


def test_disks_lists_only_large_writable_unmounted_whole_disks(tmp_path: Path) -> None:
    by_id = _by_id(tmp_path, ["big", "small", "locked", "boot", "other", "zalias"])
    (by_id / "big-part1").symlink_to(tmp_path / "dev-big")
    (by_id / "zalias").unlink()
    (by_id / "zalias").symlink_to(tmp_path / "dev-big")
    table = {
        "dev-big": f"disk 0 {100 * GIB} Fast_Disk nvme",
        "dev-small": f"disk 0 {32 * GIB} Small sata",
        "dev-locked": f"disk 1 {100 * GIB} Locked sata",
        "dev-boot": f"disk 0 {100 * GIB} Boot usb",
        "dev-other": f"loop 0 {100 * GIB} Loop",
    }

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        device = Path(argv[-1]).name
        if argv[1] == "-bdnro":
            return subprocess.CompletedProcess(argv, 0, table[device] + "\n", "")
        return subprocess.CompletedProcess(argv, 0, "/run/media\n" if device == "dev-boot" else "", "")

    found = installer._disks(run, by_id)
    assert [(Path(device).name, size, transport) for device, _, size, transport in found] == [
        ("big", 100 * GIB, "nvme")
    ]


def test_bios_start_is_refused_before_any_command(monkeypatch: pytest.MonkeyPatch) -> None:
    run = FakeRun()
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    original = Path.is_dir
    monkeypatch.setattr(Path, "is_dir", lambda self: False if str(self) == "/sys/firmware/efi" else original(self))
    assert installer.install(run=run, input_fn=lambda _: "") == 1
    assert run.calls == []


def test_a_start_without_root_is_refused_before_any_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = FakeRun()
    monkeypatch.setattr(installer.os, "geteuid", lambda: 1000)
    assert installer.install(run=run, input_fn=lambda _: "") == 1
    assert "run basix install as root" in capsys.readouterr().out
    assert run.calls == []


def test_compile_and_evaluation_precede_disk_action(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/iso/bin/basix-compile")
    run = FakeRun(
        {
            ("nix", "eval"): (0, "/nix/store/system.drv\n"),
            ("nix", "build"): (0, "/nix/store/disko-script\n"),
        }
    )
    assert installer._compile_evaluate(tmp_path, "box", run) == "/nix/store/disko-script"
    argvs = run.argvs()
    assert argvs[0] == ["/iso/bin/basix-compile", "system.py", "system.json"]
    assert argvs[1][:4] == ["nix", "flake", "lock", "--offline"]
    assert argvs[2][:4] == ["nix", "eval", "--offline", "--raw"]
    assert argvs[2][-1].endswith("nixosConfigurations.box.config.system.build.toplevel.drvPath")
    assert argvs[3][:2] == ["nix", "build"]
    assert argvs[3][-1].endswith("system.build.diskoScript")


def test_a_system_that_does_not_evaluate_stops_before_the_disk_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/iso/bin/basix-compile")
    run = FakeRun({("nix", "eval"): (1, "")})
    with pytest.raises(installer.InstallError, match="command failed: nix"):
        installer._compile_evaluate(tmp_path, "box", run)
    assert not any(argv[:2] == ["nix", "build"] for argv in run.argvs())


def _mount(tmp_path: Path) -> tuple[Path, Path]:
    work = tmp_path / "workspace"
    work.mkdir()
    (work / "system.py").write_text("machine = None\n")
    mnt = tmp_path / "mnt"
    (mnt / "etc").mkdir(parents=True)
    (mnt / "etc/passwd").write_text("learner:x:1000:100:User:/non-host-home/learner:/bin/bash\n")
    return work, mnt


def test_install_steps_run_in_order_and_the_password_stays_on_stdin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work, mnt = _mount(tmp_path)
    run = FakeRun()
    installer._install(
        work, "box", "learner", "secret-value", "Europe/Paris", DISK, [], "/nix/store/script", run, mnt=mnt
    )
    names = run.names()
    wanted = ["/nix/store/script", "mountpoint", "nix", "nixos-install", "nixos-enter", "git", "chown"]
    assert [name for name in names if name in wanted][:5] == wanted[:5]
    assert [argv[:2] for argv in run.argvs() if argv[0] == "nix"] == [["nix", "copy"]]
    streamed = [argv[0] for argv, kwargs in run.calls if kwargs.get("stream")]
    assert streamed == ["/nix/store/script", "nix", "nixos-install"]
    password_call = next((argv, kwargs) for argv, kwargs in run.calls if argv[0] == "nixos-enter")
    assert password_call[1]["input"] == "learner:secret-value\n"
    assert not any("secret-value" in " ".join(argv) for argv in run.argvs())
    captured = capsys.readouterr()
    assert "secret-value" not in captured.out + captured.err
    assert (mnt / "home/learner/basix").is_dir()
    assert (mnt / "etc/localtime").readlink() == Path("/etc/zoneinfo/Europe/Paris")


def test_the_workspace_is_in_the_home_of_the_new_account_whatever_the_environment_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer runs as root, and the variable of the new account cannot be known (016)."""
    monkeypatch.setenv("BASIX_WORKSPACE", str(tmp_path / "elsewhere"))
    work, mnt = _mount(tmp_path)
    installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/nix/store/script", FakeRun(), mnt=mnt)
    assert (mnt / "home/learner/basix/system.py").is_file()
    assert not (tmp_path / "elsewhere").exists()


def test_the_step_lines_come_in_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work, mnt = _mount(tmp_path)
    installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/nix/store/script", FakeRun(), mnt=mnt)
    assert capsys.readouterr().out.splitlines() == [
        "Partitioning and formatting the disk...",
        "Copying the workspace inputs to the new disk...",
        "Installing the system...",
    ]


@pytest.mark.parametrize("failing", ["/nix/store/script", "mountpoint", "nix", "nixos-install", "nixos-enter", "git"])
def test_every_failure_after_the_wipe_names_the_unfinished_install(tmp_path: Path, failing: str) -> None:
    work, mnt = _mount(tmp_path)
    run = FakeRun({(failing,): (1, "")})
    with pytest.raises(installer.InstallError) as caught:
        installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/nix/store/script", run, mnt=mnt)
    message = str(caught.value)
    assert message.endswith(
        ". The disk holds an unfinished install. Run basix install again; it offers to erase the disk."
    )
    assert "\n" not in message
    assert "No disk was changed" not in message


def test_a_missing_account_after_the_wipe_names_the_unfinished_install(tmp_path: Path) -> None:
    work, mnt = _mount(tmp_path)
    (mnt / "etc/passwd").write_text("other:x:1:1::/:/bin/sh\n")
    with pytest.raises(installer.InstallError, match=r"^the new system has no account learner\. The disk holds"):
        installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/nix/store/script", FakeRun(), mnt=mnt)


def test_a_failure_while_erasing_names_the_unfinished_install(tmp_path: Path) -> None:
    work, mnt = _mount(tmp_path)
    answers = _used_disk_answers() | {("sgdisk",): (2, "")}
    with pytest.raises(installer.InstallError, match=r"\. The disk holds an unfinished install\."):
        installer._install(
            work, "box", "learner", "pw", "UTC", DISK, [], "/s", FakeRun(answers), lambda _: "erase", mnt
        )


def test_a_guard_refusal_changes_nothing_and_says_so(tmp_path: Path) -> None:
    work, mnt = _mount(tmp_path)
    refused = "basix-disk-guard: REFUSED the disk has a Windows partition\n"
    run = FakeRun({("/nix/store/script",): (1, refused)})
    with pytest.raises(installer.GuardRefused) as caught:
        installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/nix/store/script", run, mnt=mnt)
    assert (
        str(caught.value) == "the disk guard refused this disk: the disk has a Windows partition. No disk was changed."
    )
    assert "nix" not in run.names()


def test_stream_shows_the_output_while_the_tool_runs(capsys: pytest.CaptureFixture[str]) -> None:
    done = installer._run(["sh", "-c", "echo one; echo two >&2; exit 3"], check=False, stream=True)
    assert capsys.readouterr().out == "one\ntwo\n"
    assert done.returncode == 3 and done.stdout == "one\ntwo\n"
    with pytest.raises(installer.InstallError, match=r"command failed: sh \(two\)"):
        installer._run(["sh", "-c", "echo one; echo two >&2; exit 3"], stream=True)


def _answers(values: list[str]) -> tuple[list[str], Any]:
    prompts: list[str] = []
    answers = iter(values)

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    return prompts, ask


def test_install_asks_in_order_and_evaluates_before_the_disk_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    etc = tmp_path / "etc"
    (etc / "inputs/node").mkdir(parents=True)
    (etc / "source").mkdir()
    (etc / "flake.lock").write_text('{"nodes":{"root":{},"node":{}}}\n')
    zoneinfo = tmp_path / "zoneinfo"
    (zoneinfo / "Europe").mkdir(parents=True)
    (zoneinfo / "Europe/Paris").write_text("")
    prompts, ask = _answers(["box", "root", "learner", "Mars/Base", "Europe/Paris", "0", "1"])
    events: list[str] = []
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    original_is_dir, original_exists = Path.is_dir, Path.exists
    monkeypatch.setattr(
        Path, "is_dir", lambda self: True if str(self) == "/sys/firmware/efi" else original_is_dir(self)
    )
    monkeypatch.setattr(Path, "exists", lambda self: True if str(self) == DISK else original_exists(self))
    monkeypatch.setattr(installer, "_disks", lambda run: [(DISK, "Disk", 100 * GIB, "nvme")])
    monkeypatch.setattr(
        installer,
        "_filter_hardware",
        lambda work, run, filter_file: (events.append("hardware"), (work / "facter.json").write_text("{}\n")),
    )
    monkeypatch.setattr(installer, "_state_version", lambda: "26.11")
    monkeypatch.setattr(installer, "_live_serial_console", lambda: None)
    monkeypatch.setattr(installer, "_compile_evaluate", lambda work, name, run: (events.append("evaluate"), "/s")[1])
    monkeypatch.setattr(installer, "_install", lambda *args, **kwargs: events.append("disk-write"))

    def password(prompt: str) -> str:
        prompts.append(prompt)
        return "secret"

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1 if argv[0] == "getent" else 0, "", "")

    result = installer.install(run=run, input_fn=ask, password_fn=password, etc=etc, zoneinfo=zoneinfo)
    assert result == 0
    assert prompts == [
        "Machine name [basix]: ",
        "Account name: ",
        "Account name: ",
        "Password: ",
        "Password again: ",
        "Time zone [UTC]: ",
        "Time zone [UTC]: ",
        "Disk number: ",
        "Disk number: ",
    ]
    assert events == ["hardware", "evaluate", "disk-write"]
    out = capsys.readouterr().out
    assert (
        'basix install: unknown time zone "Mars/Base". Use the form Region/City, for example America/New_York.\n' in out
    )
    assert out.index("Checking your settings...") > out.index("Available disks:")
    assert out.rstrip().endswith(installer.FINAL_MESSAGE)


def test_the_after_wipe_line_reaches_the_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    etc = tmp_path / "etc"
    (etc / "inputs").mkdir(parents=True)
    (etc / "source").mkdir()
    (etc / "flake.lock").write_text('{"nodes":{"root":{}}}\n')
    zoneinfo = tmp_path / "zoneinfo"
    zoneinfo.mkdir()
    (zoneinfo / "UTC").write_text("")
    _, ask = _answers(["", "learner", "", "1"])
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    original_is_dir, original_exists = Path.is_dir, Path.exists
    monkeypatch.setattr(
        Path, "is_dir", lambda self: True if str(self) == "/sys/firmware/efi" else original_is_dir(self)
    )
    monkeypatch.setattr(Path, "exists", lambda self: True if str(self) == DISK else original_exists(self))
    monkeypatch.setattr(installer, "_disks", lambda run: [(DISK, "Disk", 100 * GIB, "nvme")])
    monkeypatch.setattr(installer, "_filter_hardware", lambda work, run, filter_file: None)
    monkeypatch.setattr(installer, "_state_version", lambda: "26.11")
    monkeypatch.setattr(installer, "_live_serial_console", lambda: None)
    monkeypatch.setattr(installer, "_compile_evaluate", lambda work, name, run: "/s")
    run = FakeRun({("getent",): (2, ""), ("/s",): (1, "")})
    result = installer.install(run=run, input_fn=ask, password_fn=lambda _: "pw", etc=etc, zoneinfo=zoneinfo)
    assert result == 1
    assert (
        capsys.readouterr()
        .out.rstrip()
        .endswith(
            "basix install: the disk script failed (exit 1). "
            "The disk holds an unfinished install. Run basix install again; it offers to erase the disk."
        )
    )


def _live_pairing(tmp_path: Path, files: dict[str, str]) -> Path:
    live = tmp_path / "live-bluetooth"
    live.mkdir()
    for name, text in files.items():
        (live / name).write_text(text)
    return live


def _bluetooth_cp(run: FakeRun) -> list[list[str]]:
    return [argv for argv in run.argvs() if argv[0] == "cp"]


def test_a_pairing_in_the_live_system_is_copied_with_its_modes_and_owners(tmp_path: Path) -> None:
    work, mnt = _mount(tmp_path)
    live = _live_pairing(tmp_path, {"state": "paired"})
    run = FakeRun()
    installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/s", run, mnt=mnt, bluetooth=live)
    assert _bluetooth_cp(run) == [["cp", "-a", str(live), str(mnt / "var/lib/bluetooth")]]
    names = run.names()
    assert names.index("nixos-install") < names.index("cp") < names.index("nixos-enter")


def test_a_stale_target_is_removed_before_the_copy(tmp_path: Path) -> None:
    work, mnt = _mount(tmp_path)
    (mnt / "var/lib/bluetooth").mkdir(parents=True)
    (mnt / "var/lib/bluetooth/old").write_text("x")
    live = _live_pairing(tmp_path, {"state": "paired"})
    installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/s", FakeRun(), mnt=mnt, bluetooth=live)
    assert not (mnt / "var/lib/bluetooth").exists()


def test_no_pairing_in_the_live_system_copies_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work, mnt = _mount(tmp_path)
    run = FakeRun()
    installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/s", run, mnt=mnt, bluetooth=tmp_path / "absent")
    assert _bluetooth_cp(run) == []
    assert "Bluetooth" not in capsys.readouterr().out


def test_an_empty_pairing_folder_copies_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    work, mnt = _mount(tmp_path)
    live = _live_pairing(tmp_path, {})
    run = FakeRun()
    installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/s", run, mnt=mnt, bluetooth=live)
    assert _bluetooth_cp(run) == []
    assert "Bluetooth" not in capsys.readouterr().out


def test_a_failed_pairing_copy_warns_once_and_the_install_goes_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work, mnt = _mount(tmp_path)
    live = _live_pairing(tmp_path, {"state": "paired"})
    run = FakeRun({("cp",): (1, "")})
    installer._install(work, "box", "learner", "pw", "UTC", DISK, [], "/s", run, mnt=mnt, bluetooth=live)
    warnings = [line for line in capsys.readouterr().out.splitlines() if "Bluetooth" in line]
    assert warnings == [
        "basix install: could not copy the Bluetooth pairing to the new system. "
        "The system is installed. Pair the device again after the first login."
    ]
    assert run.names()[-2:] == ["git", "chown"]
    assert (mnt / "home/learner/basix").is_dir()
