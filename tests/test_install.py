"""Fixtures for the install questions, files, filter, and safe command order."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from basix import install as installer

FILTER = Path(__file__).resolve().parents[1] / "install" / "drop.jq"


def test_machine_and_account_questions_reject_unsafe_values() -> None:
    assert not installer._safe_name("iso")
    assert not installer._safe_name("seed")
    assert not installer._safe_name("Bad_Name")
    assert not installer._safe_name("bad-")
    assert installer._safe_name("workstation-2")
    assert not installer._safe_user("root!")
    assert installer._safe_user("learner7")


@pytest.mark.parametrize(
    "name",
    [
        "root",
        "wheel",
        "disk",
        "audio",
        "video",
        "users",
        "nixbld",
        "nixbld7",
        "nobody",
        "systemd-network",
        "messagebus",
    ],
)
def test_reserved_account_names_are_refused(name: str) -> None:
    assert not installer._safe_user(name)


def test_network_refusal_stops_before_a_disk_write() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "")

    with pytest.raises(installer.InstallError, match="cannot reach the network"):
        installer._check_network(run, lambda _: "n")
    assert calls == [["nm-online", "--quiet", "--timeout=30"], ["nm-online", "--quiet", "--timeout=30"]]


def test_locked_input_source_is_required_for_every_lock_node(tmp_path: Path) -> None:
    (tmp_path / "nixpkgs").mkdir()
    (tmp_path / "pynix").mkdir()
    lock = {"nodes": {"root": {}, "nixpkgs": {}, "pynix": {}}}
    assert installer._locked_inputs(lock, tmp_path) == [tmp_path / "nixpkgs", tmp_path / "pynix"]
    absent = tmp_path / "absent"
    (absent / "nixpkgs").mkdir(parents=True)
    with pytest.raises(installer.InstallError, match="no source for locked input pynix"):
        installer._locked_inputs(lock, absent)


def test_disk_question_uses_a_numbered_list(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(installer, "_disks", lambda run: [("/dev/disk/by-id/disk0", "Disk", 100_000_000_000, "nvme")])
    answers = iter(["name", "0", "1"])
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    assert installer._choose_disk(input_fn=ask) == "/dev/disk/by-id/disk0"
    assert prompts == ["Disk number: ", "Disk number: ", "Disk number: "]


def test_drop_filter_removes_identifiers_and_keeps_hardware(tmp_path: Path) -> None:
    source = tmp_path / "hardware.json"
    source.write_text(
        json.dumps(
            {
                "hardware": {
                    "cpu": [{"name": "CPU model", "tlb_size": 64, "cores": 8}],
                    "disks": [
                        {"model": "Disk model", "serial": "SERIAL-123", "wwn": "WWN-123"},
                        {"device": "/dev/disk/by-id/wwn-device"},
                    ],
                    "/dev/disk/by-id/nvme-SERIAL-456": {"model": "Hidden key"},
                    "/dev/disk/by-uuid/UUID-789": {"model": "Hidden uuid key"},
                },
                "names": ["/dev/disk/by-label/LABEL-1", "/dev/disk/by-partuuid/PART-2", "/dev/sda"],
                "usb": [{"serialNumber": "USB-123", "vendor": "Keyboard maker"}],
            }
        )
    )
    done = subprocess.run(["jq", "-f", str(FILTER), str(source)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    filtered = json.loads(done.stdout)
    text = done.stdout
    assert "SERIAL-123" not in text and "WWN-123" not in text and "USB-123" not in text
    assert "/dev/disk/by-id/" not in text and "tlb_size" not in text
    assert "by-uuid" not in text and "by-label" not in text and "by-partuuid" not in text
    assert filtered["names"] == ["/dev/sda"]
    assert filtered["hardware"]["cpu"][0] == {"name": "CPU model", "cores": 8}
    assert filtered["hardware"]["disks"][0]["model"] == "Disk model"
    assert filtered["usb"][0]["vendor"] == "Keyboard maker"


def test_workspace_files_have_answers_and_no_password(tmp_path: Path) -> None:
    lock = tmp_path / "iso.lock"
    lock.write_text("{}\n")
    source = Path("/nix/store/test-basix-source")
    installer._write_system(tmp_path, "workstation", "learner", "/dev/disk/by-id/disk0", "26.11", lock, source, "ttyS0")
    entry = (tmp_path / "system.py").read_text()
    assert 'name="workstation"' in entry
    assert 'user="learner"' in entry
    assert 'disk="/dev/disk/by-id/disk0"' in entry
    assert 'state_version="26.11"' in entry
    assert 'serial_console="ttyS0"' in entry
    assert "password" not in entry
    assert (tmp_path / ".gitignore").read_text() == ".basix/\n"
    assert "basix apply" in (tmp_path / "README.md").read_text()
    stub = (tmp_path / "flake.nix").read_text()
    assert f'inputs.basix.url = "path:{source}";' in stub
    assert "nixosConfigurations =" in stub


def test_copied_lock_is_writable_when_the_source_is_read_only(tmp_path: Path) -> None:
    """The ISO lock is a read-only store file. Nix writes the lock at `basix update`, so the copy must stay writable."""
    lock = tmp_path / "iso.lock"
    lock.write_text("{}\n")
    lock.chmod(0o444)
    work = tmp_path / "work"
    work.mkdir()
    installer._write_system(
        work, "workstation", "learner", "/dev/disk/by-id/disk0", "26.11", lock, Path("/nix/store/x")
    )
    mode = (work / "flake.lock").stat().st_mode
    assert mode & 0o200, oct(mode)
    assert (work / "flake.lock").read_text() == "{}\n"


def _cmdline(tmp_path: Path, text: str) -> Path:
    cmdline = tmp_path / "cmdline"
    cmdline.write_text(text + "\n")
    return cmdline


def test_live_serial_console_comes_from_the_basix_serial_parameter(tmp_path: Path) -> None:
    cmdline = _cmdline(tmp_path, "BOOT_IMAGE=/nix/store/kernel console=tty0 basix.serial=ttyS0 loglevel=4")
    assert installer._live_serial_console(cmdline) == "ttyS0"


def test_live_serial_console_is_absent_without_the_parameter(tmp_path: Path) -> None:
    assert installer._live_serial_console(_cmdline(tmp_path, "BOOT_IMAGE=/nix/store/kernel quiet")) is None


def test_live_serial_console_ignores_the_console_parameter(tmp_path: Path) -> None:
    cmdline = _cmdline(tmp_path, "BOOT_IMAGE=/nix/store/kernel console=tty0 console=ttyS0,115200n8")
    assert installer._live_serial_console(cmdline) is None


def test_live_serial_console_refuses_a_bad_device(tmp_path: Path) -> None:
    assert installer._live_serial_console(_cmdline(tmp_path, "basix.serial=tty0")) is None
    assert installer._live_serial_console(_cmdline(tmp_path, "basix.serial=../x")) is None


def test_final_message_matches_bsx_387() -> None:
    assert installer.FINAL_MESSAGE == (
        "Your machine file is ~/basix/system.py. Edit it, then run basix apply. "
        "For your own programs and settings, run minix init."
    )
