"""`basix install`: write a Basix workspace and install its system from the live ISO.

The workspace is `~/basix` of the new account. `BASIX_WORKSPACE` does not apply to the installer (see `_install`).
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from basix_config.stub import make_stub

RUN = Callable[..., subprocess.CompletedProcess[str]]
ETC = Path("/etc/basix")
ZONEINFO = Path("/etc/zoneinfo")
# The Bluetooth pairing of the live system (BSX-326). A keyboard that the learner paired in the installer then works at
# the first login.
BLUETOOTH = Path("/var/lib/bluetooth")
BLUETOOTH_WARNING = (
    "could not copy the Bluetooth pairing to the new system. "
    "The system is installed. Pair the device again after the first login."
)
MIN_DISK_BYTES = 64_000_000_000
# The partition labels that the disk layout writes (`modules/disk.nix`: the disk `main`, the partitions `ESP` and `root`).
BASIX_PARTLABELS = frozenset({"disk-main-ESP", "disk-main-root"})
# The name of a system account or group on a Basix system. The list is the users and groups that the NixOS
# modules of the live ISO define (observed on the ISO machine), with the standard groups. The prefixes below add more.
RESERVED_NAMES = frozenset(
    {
        "adm",
        "audio",
        "cdrom",
        "clock",
        "dialout",
        "disk",
        "floppy",
        "input",
        "keys",
        "kmem",
        "kvm",
        "lp",
        "messagebus",
        "networkmanager",
        "nobody",
        "nogroup",
        "nscd",
        "polkituser",
        "render",
        "resolvconf",
        "root",
        "rpc",
        "sgx",
        "shadow",
        "sshd",
        "tape",
        "tty",
        "users",
        "utmp",
        "uucp",
        "video",
        "wheel",
        "wpa_supplicant",
    }
)
RESERVED_PREFIXES = ("systemd-", "nixbld", "nm-")
AFTER_WIPE = "The disk holds an unfinished install. Run basix install again; it offers to erase the disk."
FINAL_MESSAGE = (
    "Your machine file is ~/basix/system.py. Edit it, then run basix apply. "
    "For your own programs and settings, run minix init."
)


class InstallError(Exception):
    """One safe reason why the install stopped."""


class GuardRefused(InstallError):
    """The disk guard refused the disk before any write."""


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _stream(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run one tool and copy its output to the console as it runs. Keep the last lines."""
    tail: deque[str] = deque(maxlen=40)
    with subprocess.Popen(
        argv, text=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs
    ) as process:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="" if line.endswith("\n") else "\n", flush=True)
            tail.append(line)
        code = process.wait()
    return subprocess.CompletedProcess(argv, code, "".join(tail), "")


def _run(
    argv: Sequence[str], *, check: bool = True, stream: bool = False, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run one installer tool and return its output.

    A tool with `stream=True` shows its output on the console while it runs. Its result keeps the last lines only.
    """
    if stream:
        done = _stream(argv, **kwargs)
    else:
        done = subprocess.run(argv, text=True, capture_output=True, check=False, **kwargs)
    if check and done.returncode:
        detail = _last_line(done.stderr) or _last_line(done.stdout)
        raise InstallError(f"command failed: {argv[0]} ({detail or f'exit {done.returncode}'})")
    return done


def _say(text: str) -> None:
    print(text, flush=True)


def _ask(prompt: str, input_fn: Callable[[str], str] = input) -> str:
    try:
        return input_fn(prompt).strip()
    except EOFError as error:
        raise InstallError("the input ended before the questions ended. No disk was changed.") from error


def _check_network(run: RUN = _run, input_fn: Callable[[str], str] = input) -> None:
    """Require a network before the disk changes, and offer the live Wi-Fi tool."""
    _say("Checking network...")
    if run(["nm-online", "--quiet", "--timeout=30"], check=False).returncode == 0:
        return
    _say("No working network connection. Connect Ethernet or use nmtui to join Wi-Fi.")
    if _ask("Run nmtui now? [y/N] ", input_fn) == "y":
        run(["nmtui"], check=False)
    if run(["nm-online", "--quiet", "--timeout=30"], check=False).returncode != 0:
        raise InstallError(
            "cannot reach the network. Connect a network cable or join Wi-Fi, then run basix install again. No disk was changed."
        )


def _safe_name(value: str) -> bool:
    return (
        bool(re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value))
        and not value.endswith("-")
        and value
        not in {
            "iso",
            "seed",
        }
    )


def _safe_user(value: str) -> bool:
    """Accept a lower-case account name that no system account or group uses."""
    return (
        bool(re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value))
        and value not in RESERVED_NAMES
        and not value.startswith(RESERVED_PREFIXES)
    )


def _safe_zone(value: str, zoneinfo: Path = ZONEINFO) -> bool:
    """Accept a name that is a time zone file of the live system."""
    return bool(re.fullmatch(r"[A-Za-z0-9_+/-]+", value)) and (zoneinfo / value).is_file()


def _locked_inputs(lock: Mapping[str, object], root: Path = ETC / "inputs") -> list[Path]:
    """Find one immutable source for every non-root node in the Basix lock."""
    nodes = lock.get("nodes")
    if not isinstance(nodes, dict):
        raise InstallError("the ISO lock has no input nodes. No disk was changed.")
    names = sorted(set(nodes) - {"root"})
    paths: list[Path] = []
    for name in names:
        source = root / str(name)
        if not source.is_dir():
            raise InstallError(f"the ISO has no source for locked input {name}. No disk was changed.")
        paths.append(source.resolve())
    return paths


def _disks(run: RUN = _run, by_id: Path = Path("/dev/disk/by-id")) -> list[tuple[str, str, int, str]]:
    """List usable whole disks by stable by-id name."""
    found: list[tuple[str, str, int, str]] = []
    seen: set[Path] = set()
    for link in sorted(by_id.glob("*")):
        if "-part" in link.name or not link.is_symlink():
            continue
        resolved = link.resolve()
        if resolved in seen:
            continue
        device = str(link)
        info = run(
            ["lsblk", "-bdnro", "TYPE,RO,SIZE,MODEL,TRAN", str(resolved)],
            check=False,
        )
        fields = info.stdout.strip().split(None, 4)
        if len(fields) < 3 or fields[0] != "disk" or fields[1] != "0":
            continue
        try:
            size = int(fields[2])
        except ValueError:
            continue
        if size < MIN_DISK_BYTES:
            continue
        mounted = run(["lsblk", "-nro", "MOUNTPOINTS", str(resolved)], check=False).stdout.strip()
        if mounted:
            continue
        model = fields[3] if len(fields) > 3 and fields[3] else "unknown model"
        transport = fields[4] if len(fields) > 4 and fields[4] else "unknown"
        found.append((device, model.strip(), size, transport))
        seen.add(resolved)
    return found


def _choose_disk(run: RUN = _run, input_fn: Callable[[str], str] = input) -> str:
    disks = _disks(run)
    if not disks:
        raise InstallError("no disk with a /dev/disk/by-id name is available and 64 GB or larger. No disk was changed.")
    _say("Available disks:")
    for index, (_, model, size, transport) in enumerate(disks, 1):
        _say(f"{index}. {model} | {size // 1_000_000_000} GB | {transport}")
    while True:
        answer = _ask("Disk number: ", input_fn)
        if answer.isdecimal() and 1 <= int(answer) <= len(disks):
            return disks[int(answer) - 1][0]
        _say(f"basix install: type a number from the list, 1 to {len(disks)}.")


def _disk_is_used(disk: str, run: RUN = _run) -> bool:
    parts = run(["lsblk", "-nro", "NAME", disk], check=False).stdout.splitlines()[1:]
    signatures = run(["wipefs", "--noheadings", "--output", "TYPE", disk], check=False).stdout.strip()
    return bool(parts or signatures)


def _write_system(
    work: Path,
    name: str,
    user: str,
    disk: str,
    state_version: str,
    lock_file: Path,
    basix_source: Path,
    serial_console: str | None = None,
) -> None:
    source_url = f"path:{basix_source.resolve()}"
    (work / "flake.nix").write_text(make_stub(basix_url=source_url).text)
    # copyfile, not copy2: the ISO lock is a read-only store file, and the learner workspace must keep it writable.
    shutil.copyfile(lock_file, work / "flake.lock")
    (work / ".gitignore").write_text(".basix/\n")
    (work / "README.md").write_text(
        "# Your Basix workspace\n\n"
        "Edit `system.py` to change this machine. Run `basix apply` to use a change. "
        "Run `minix init` to set up your own programs and settings.\n"
    )
    machine_fields = [
        f"    name={json.dumps(name)},",
        f"    user={json.dumps(user)},",
        f"    disk={json.dumps(disk)},",
        f"    state_version={json.dumps(state_version)},",
    ]
    if serial_console is not None:
        machine_fields.append(f"    serial_console={json.dumps(serial_console)},")
    (work / "system.py").write_text(
        "from basix_config import *\n\nmachine = Machine(\n" + "\n".join(machine_fields) + "\n)\n"
    )


def _live_serial_console(cmdline_file: Path = Path("/proc/cmdline")) -> str | None:
    """Return the device of the boot parameter `basix.serial=DEVICE`, which only a test VM sets (010-D35).

    A `console=` parameter does not count: a real computer may start with one and must not get a serial console.
    """
    for word in cmdline_file.read_text().split():
        if not word.startswith("basix.serial="):
            continue
        device = word.partition("=")[2]
        if re.fullmatch(r"(?:tty[A-Za-z]+[0-9]+|hvc[0-9]+)", device) and device != "tty0":
            return device
    return None


def _filter_hardware(work: Path, run: RUN = _run, filter_file: Path = ETC / "drop.jq") -> None:
    raw = work / "facter-raw.json"
    report = run(["nixos-facter"], check=True)
    raw.write_text(report.stdout)
    result = run(["jq", "-f", str(filter_file), str(raw)], check=True)
    (work / "facter.json").write_text(result.stdout)
    raw.unlink()


def _state_version() -> str:
    for line in Path("/etc/os-release").read_text().splitlines():
        if line.startswith("VERSION_ID="):
            return line.partition("=")[2].strip('"')
    raise InstallError("the ISO has no NixOS version. No disk was changed.")


def _compile_evaluate(work: Path, name: str, run: RUN = _run) -> str:
    """Compile, then evaluate the whole system and build the disk script, before the disk changes.

    The evaluation of the system runs with `--offline`. It fails when the ISO lacks the source of any locked input,
    so it proves that the ISO carries every input. The build of the disk script may use a cache that the live system
    configures: it fetches build outputs, never a source of an input.
    """
    compiler = shutil.which("basix-compile")
    if not compiler:
        raise InstallError("the ISO has no Basix compiler. No disk was changed.")
    run([compiler, "system.py", "system.json"], cwd=work, check=True)
    run(["nix", "flake", "lock", "--offline"], cwd=work, check=True)
    system = f"path:{work}#nixosConfigurations.{name}.config.system.build"
    evaluated = run(["nix", "eval", "--offline", "--raw", f"{system}.toplevel.drvPath"], cwd=work, check=True)
    if not evaluated.stdout.strip().endswith(".drv"):
        raise InstallError("Nix did not return the system path. No disk was changed.")
    built = run(["nix", "build", "--no-link", "--print-out-paths", f"{system}.diskoScript"], cwd=work, check=True)
    script = built.stdout.strip().splitlines()
    if not script:
        raise InstallError("Nix did not return the disk script path. No disk was changed.")
    return script[-1]


def _after_wipe(error: Exception) -> InstallError:
    """Name a failure after the disk changed: the disk holds an unfinished install."""
    reason = _last_line(str(error)).rstrip(". ") or "the install failed"
    return InstallError(f"{reason}. {AFTER_WIPE}")


def erase(disk: str, run: RUN = _run) -> None:
    """Wipe every partition signature first, then the disk. A signature that stays makes the disk script reuse it."""
    children = run(["lsblk", "-nrpo", "PATH", disk], check=False).stdout.splitlines()[1:]
    for child in reversed(children):
        run(["wipefs", "--all", "--force", child])
    run(["wipefs", "--all", "--force", disk])
    run(["sgdisk", "--zap-all", disk])
    run(["partprobe", disk], check=False)
    run(["udevadm", "settle"])


def _prepare_disk(disk: str, run: RUN = _run, input_fn: Callable[[str], str] = input) -> bool:
    """Offer to erase a used disk. Return true when the disk was erased."""
    if not _disk_is_used(disk, run):
        return False
    _say("This disk already contains data:")
    table = run(["lsblk", "-o", "NAME,SIZE,FSTYPE,LABEL,PARTLABEL,MOUNTPOINTS", disk], check=False).stdout
    _say(table.rstrip())
    labels = run(["lsblk", "-nro", "PARTLABEL", disk], check=False).stdout.split()
    if BASIX_PARTLABELS.intersection(labels):
        _say("This may be an earlier Basix install, including one that did not finish.")
    if _ask("Type erase to destroy everything on this disk: ", input_fn) != "erase":
        raise InstallError("erase refused. No disk was changed.")
    try:
        erase(disk, run)
    except (InstallError, OSError) as error:
        raise _after_wipe(error) from error
    return True


def _run_disk_script(script: str, run: RUN, erased: bool) -> None:
    """Run the disk script. The guard runs first, so a refusal leaves the disk as it was."""
    done = run([script], check=False, stream=True)
    if not done.returncode:
        return
    refusal = next((line for line in done.stdout.splitlines() if "basix-disk-guard: REFUSED" in line), "")
    if refusal and not erased:
        reason = refusal.partition("REFUSED")[2].strip()
        raise GuardRefused(f"the disk guard refused this disk: {reason}. No disk was changed.")
    raise InstallError(f"the disk script failed (exit {done.returncode})")


def _copy_bluetooth(mnt: Path, run: RUN, source: Path | None = None) -> None:
    """Copy the pairing state of the live system to the new system. Keep the modes and the owners.

    With no state, do nothing. A failed copy costs only a new pairing, so it prints one warning and the install goes on.
    """
    source = source or BLUETOOTH
    try:
        if not source.is_dir() or not any(source.iterdir()):
            return
        target = mnt / "var/lib/bluetooth"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        run(["cp", "-a", str(source), str(target)])
    except (InstallError, OSError):
        _say(f"basix install: {BLUETOOTH_WARNING}")


def _install(
    work: Path,
    name: str,
    user: str,
    password: str,
    zone: str,
    disk: str,
    inputs: list[Path],
    script: str,
    run: RUN,
    input_fn: Callable[[str], str] = input,
    mnt: Path = Path("/mnt"),
    bluetooth: Path | None = None,
) -> None:
    """Offer the erase, run the guard, install, and set the password through standard input.

    After the disk changes, every failure names an unfinished install.
    """
    erased = _prepare_disk(disk, run, input_fn)
    try:
        _say("Partitioning and formatting the disk...")
        _run_disk_script(script, run, erased)
        if run(["mountpoint", "-q", str(mnt)], check=False).returncode:
            raise InstallError("the disk script did not mount the new disk")
        _say("Copying the workspace inputs to the new disk...")
        run(["nix", "copy", "--no-check-sigs", "--to", str(mnt), *map(str, inputs)], stream=True)
        roots = mnt / "nix/var/nix/gcroots/basix"
        roots.mkdir(parents=True, exist_ok=True)
        for index, source in enumerate(inputs, 1):
            link = roots / f"input-{index}"
            link.unlink(missing_ok=True)
            link.symlink_to(source)
        _say("Installing the system...")
        run(
            [
                "nixos-install",
                "--root",
                str(mnt),
                "--flake",
                f"{work}#{name}",
                "--no-root-password",
                "--no-channel-copy",
            ],
            stream=True,
        )
        _copy_bluetooth(mnt, run, bluetooth)
        run(["nixos-enter", "--root", str(mnt), "-c", "chpasswd"], input=f"{user}:{password}\n")
        localtime = mnt / "etc/localtime"
        localtime.unlink(missing_ok=True)
        localtime.symlink_to(f"/etc/zoneinfo/{zone}")
        passwd = (mnt / "etc/passwd").read_text().splitlines()
        account = next((line.split(":") for line in passwd if line.startswith(f"{user}:")), None)
        if account is None:
            raise InstallError(f"the new system has no account {user}")
        uid, gid = account[2], account[3]
        # The installer runs as root on the live ISO, and the new account has no environment yet. So the variable
        # BASIX_WORKSPACE has no meaning here: the workspace is always ~/basix of the new account. The learner sets
        # the variable later and moves the folder.
        target = mnt / "home" / user / "basix"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(work, target)
        run(["git", "init", "-q"], cwd=target)
        run(["git", "config", "user.name", user], cwd=target)
        run(["git", "config", "user.email", f"{user}@{name}"], cwd=target)
        run(["git", "add", "-A"], cwd=target)
        run(["git", "commit", "-q", "-m", "Install Basix"], cwd=target)
        run(["chown", "-R", f"{uid}:{gid}", str(target)])
    except GuardRefused:
        raise
    except (InstallError, OSError) as error:
        raise _after_wipe(error) from error


def install(
    *,
    run: RUN = _run,
    input_fn: Callable[[str], str] = input,
    password_fn: Callable[[str], str] = getpass.getpass,
    etc: Path = ETC,
    zoneinfo: Path = ZONEINFO,
) -> int:
    """Ask for the machine settings, then install Basix."""
    try:
        if os.geteuid() != 0:
            raise InstallError("run basix install as root from the live ISO. No disk was changed.")
        if not Path("/sys/firmware/efi").is_dir():
            raise InstallError(
                "UEFI firmware is required. Enable UEFI boot and start the ISO again. No disk was changed."
            )
        _check_network(run, input_fn)
        lock = json.loads((etc / "flake.lock").read_text())
        inputs = [Path(etc / "source").resolve(), *_locked_inputs(lock, etc / "inputs")]
        name = _ask("Machine name [basix]: ", input_fn) or "basix"
        while not _safe_name(name):
            _say("basix install: use a lower-case machine name with letters, digits, and hyphens.")
            name = _ask("Machine name [basix]: ", input_fn) or "basix"
        user = _ask("Account name: ", input_fn)
        while not _safe_user(user) or any(
            run(["getent", database, user], check=False).returncode == 0 for database in ("passwd", "group")
        ):
            _say("basix install: choose an unused account name with lower-case letters, digits, and hyphens.")
            user = _ask("Account name: ", input_fn)
        if password_fn is getpass.getpass and not sys.stdin.isatty():
            raise InstallError("run basix install in a terminal so the password stays hidden. No disk was changed.")
        password = password_fn("Password: ")
        while not password or password != password_fn("Password again: "):
            _say("basix install: passwords must be non-empty and match.")
            password = password_fn("Password: ")
        zone = _ask("Time zone [UTC]: ", input_fn) or "UTC"
        while not _safe_zone(zone, zoneinfo):
            _say(f'basix install: unknown time zone "{zone}". Use the form Region/City, for example America/New_York.')
            zone = _ask("Time zone [UTC]: ", input_fn) or "UTC"
        disk = _choose_disk(run, input_fn)
        # Resolve and validate the chosen name again. Never accept a kernel device path or partition.
        if not disk.startswith("/dev/disk/by-id/") or "-part" in Path(disk).name or not Path(disk).exists():
            raise InstallError("choose a whole disk by its /dev/disk/by-id name. No disk was changed.")
        _say("Checking your settings...")
        with tempfile.TemporaryDirectory(prefix="basix-install-") as temp:
            work = Path(temp) / "basix"
            work.mkdir()
            _filter_hardware(work, run, etc / "drop.jq")
            _write_system(
                work,
                name,
                user,
                disk,
                _state_version(),
                etc / "flake.lock",
                Path(etc / "source").resolve(),
                _live_serial_console(),
            )
            script = _compile_evaluate(work, name, run)
            _install(work, name, user, password, zone, disk, inputs, script, run, input_fn)
            del password
            _say(FINAL_MESSAGE)
        return 0
    except (InstallError, OSError, ValueError, json.JSONDecodeError) as error:
        _say(f"basix install: {error}")
        return 1
