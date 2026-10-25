"""Run `basix install` in VMs and log in to the installed systems (B7 step 1; BSX-150, BSX-159).

Usage: python3 -I scripts/install_vm.py [ROOT] [SCENARIO ...]      (run by `testee verify opt:basix-install`)

`BASIX_PARALLEL=N` sets how many scenarios run at once (default 1). `BASIX_PARALLEL=1` runs them in turn.
Each scenario has its own images, VM folders, and log files. The ISO is built once, before they start.

The check builds the ISO, boots it under OVMF (or SeaBIOS for the negative shape), and types the
answers of a learner into `basix install` over the serial line. Every disk is a qcow2 image file.
No host block device reaches QEMU. The password is a test-only value.

Scenarios (all by default):
- nvme       `q35-nvme`: install on a blank 64 GB NVMe disk, boot the disk alone, log in (`LOGIN OK`). The
             learner first types an unknown time zone (the warning line), then `Europe/Berlin`; the installed system
             holds that zone. The first offline `basix apply` runs on the installed disk (BSX-378).
- bios       `pc-bios`: the install refuses before it touches the disk.
- no-network the install stops before it writes a disk and says how to connect.
- small-disk a 55 GB disk: the install refuses it.
- used-disk  `q35-sata`: a disk with a partition `precious`: the erase offer lists the disk and does not name an
             earlier Basix install; a wrong word at the erase prompt refuses and keeps the image; `erase` installs;
             the new system boots and the login works. This is also the AHCI install, and a comparison of its
             workspace with the nvme one follows after all scenarios end.
- cut-install kill the VM when the system copy of `nixos-install` starts (after `Installing the system...` and its
             first `copying path` line); the retry names an earlier Basix install, erases it, installs, and the
             login works.

Every install also checks the step lines of the text contract in order (`Checking your settings...` to
`Installing the system...`), that the password is not on the console, and that the installed `system.py` holds
`serial_console="ttyS0"` (the test ISO boots with `basix.serial=ttyS0`, 010-D35).

The check builds and boots the test ISO (`packages.iso-test`, fast squashfs compression; see `iso_boot.ISO_ATTR`).
It fails fast: a wait on the console stops after `vmlib.STALL_S` seconds with no new output, `nvme-cache` is skipped
when `nvme` failed, and a scenario stops its dependent steps after a failed precondition. `QUIET_STALL_S` is the bound
of the steps that still print nothing (see its comment).

A refusal passes only when the image file hash is unchanged. A successful install compares only
the size and the modification time of its image, because a hash of a big image is slow. Raw serial logs and the image hashes go
to `$BASIX_LOGS/install_vm/TIME`. VM files go under `$BASIX_WORK`. The check stops a step when the
unallocated space of `/` is under 15 GiB.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import inspect
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import iso_boot  # noqa: E402
import vmlib  # noqa: E402
import workdir  # noqa: E402

RESULTS: list[tuple[bool, str]] = []
APPLIED = "applied: the system runs, and its units are healthy"  # the last line of `basix apply`, as in apply_vm.py
_LOCK = threading.Lock()  # guards RESULTS and the output of `report`
_HERE = threading.local()  # `_HERE.results` is the list of the scenario that runs in this thread
DEFAULT_PARALLEL = 1
#: The stall bound (seconds with no console output) of a step that still prints nothing for a long time. The disk
#: script, the `nix copy`, and `nixos-install` stream their progress, so their waits use the default bound
#: (`vmlib.STALL_S`). Three steps stay silent: the check after `Checking your settings...` (hardware report, compile, and
#: the evaluation of the whole system), the evaluation inside `nixos-install` before its first copy line, and
#: the offline `basix apply` on the installed disk (a quiet `nix build`).
QUIET_STALL_S = 600.0
PASSWORD = "B7-test-Console-2026!"
ACCOUNT = "learner"
MACHINE = "basix"
ZONE = "Europe/Berlin"  # the zone that the nvme scenarios type; the other scenarios take the default
BAD_ZONE = "Mars/Base"  # the nvme scenarios type this unknown zone first
MIN_FREE_GIB = 15.0
# The used-disk raw file lives here (tmpfs keeps it sparse). It may not hold more than this after sfdisk.
RAW_DIR = "/dev/shm"
RAW_MAX_BYTES = 64 * 1024**2
INSTALL_GIB = 64
# The cache of installed disks (see `DiskCache`). The default is a constant, so a task needs no variable for it.
DISK_FORMAT = 2  # The P4 ISO installer and VM harness change the installed disk contract.
DISK_KEEP = 2  # The cache keeps the newest bases up to this number.
GIB = 1024**3
SCENARIOS = ["nvme", "bios", "no-network", "small-disk", "used-disk", "cut-install"]
SHAPES = {
    # name: (cpu, disk device arguments, serial of the disk)
    "q35-nvme": ("Skylake-Client-v4", ["-device", "nvme,drive=d0,serial=b7nvme"], "nvme"),
    "q35-sata": ("Haswell-v4", ["-device", "ich9-ahci,id=ahci", "-device", "ide-hd,drive=d0,bus=ahci.0,serial=b7sata"], "sata"),
    "pc-bios": ("Haswell-v4", ["-device", "ich9-ahci,id=ahci", "-device", "ide-hd,drive=d0,bus=ahci.0,serial=b7bios"], "sata"),
}


def report(ok: bool, text: str) -> bool:
    with _LOCK:
        RESULTS.append((ok, text))
        mine = getattr(_HERE, "results", None)
        if mine is not None:
            mine.append((ok, text))
        print(("PASS " if ok else "FAIL ") + text, flush=True)
    return ok


def stamp(path: Path) -> tuple[int, int]:
    """Size and modification time of a file. A write to the image changes the modification time."""
    info = path.stat()
    return info.st_size, info.st_mtime_ns


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail_count() -> int:
    """The failed results of this thread's scenario, or of the whole run when no scenario thread exists."""
    with _LOCK:
        rows = getattr(_HERE, "results", None)
        return sum(1 for ok, _ in (RESULTS if rows is None else rows) if not ok)


def drive_fingerprint() -> str:
    """A hash of the code that drives the installer and starts the VM. A change of it changes the key of the cache."""
    parts = [inspect.getsource(item) for item in (type_answers, answer_zone, start_install, basix_command, Vm.__init__)]
    parts.append(repr(SHAPES))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def disk_key(lab: Lab, shape: str) -> tuple[str, dict]:
    """The name and the key of the installed disk that `basix install` makes from `lab.iso` on this shape.

    The ISO store path covers the installer, the carried sources (/etc/basix/inputs), the template, and the system inputs:
    the ISO derivation holds each of them. The rest is the VM hardware (the hardware report goes into the disk), the answers
    of the learner, and the code that drives the install.
    """
    key = {
        "format": DISK_FORMAT,
        "iso": str(lab.iso),
        "shape": shape,
        "shape_arguments": SHAPES[shape],
        "disk_gib": INSTALL_GIB,
        "cpus": vmlib.VM_CPUS,
        "qemu": str(lab.qemu),
        "ovmf": str(lab.ovmf),
        "answers": {
            "machine": MACHINE,
            "account": ACCOUNT,
            "password_sha256": hashlib.sha256(PASSWORD.encode()).hexdigest(),
            "zone": ZONE,
            "profile": "default",
            "keyboard": "default",
            "disk_number": "1",
        },
        "drive": drive_fingerprint(),
    }
    key = json.loads(json.dumps(key))  # the form that the stamp holds: a tuple becomes a list
    digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:16]
    return f"{shape}-{digest}", key


class DiskCache:
    """Installed disks that later runs reuse, so `undo_vm.py` and `release_vm.py` skip the install.

    A base is the qcow2 image of a finished install after the clean power-off, before any boot of the installed disk.
    A run never writes a base. It boots a qcow2 overlay that names the base as its backing file.
    Each base has a stamp `NAME.json` with its key. The stamp is the last file that a writer renames, so a reader that
    finds a stamp finds a complete base. A reader takes a shared lock on the base for the life of the process, and
    the pruner skips a locked base.
    """

    def __init__(self, logs: vmlib.Logs):
        work = os.environ.get("BASIX_WORK") or str(workdir.default_work("b5"))
        self.dir = Path(os.environ.get("BASIX_DISK_CACHE", f"{work}/installed-disks"))
        self.logs = logs
        self.holds: list = []

    def paths(self, name: str) -> tuple[Path, Path]:
        return self.dir / f"{name}.qcow2", self.dir / f"{name}.json"

    def has(self, name: str, key: dict) -> bool:
        base, stamp = self.paths(name)
        try:
            data = json.loads(stamp.read_text())
            return data.get("key") == key and base.stat().st_size == data.get("bytes")
        except (OSError, ValueError):
            return False

    def lookup(self, name: str, key: dict) -> Path | None:
        """The base for this key, locked for sharing, or None."""
        if not self.has(name, key):
            return None
        base, stamp = self.paths(name)
        try:
            handle = base.open("rb")
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if os.fstat(handle.fileno()).st_nlink == 0:
                handle.close()
                return None
        except OSError:
            return None
        self.holds.append(handle)
        with contextlib.suppress(OSError):
            os.utime(stamp)  # The newest use keeps a base when the cache prunes.
        return base

    def space(self, label: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        total = sum(item.stat().st_size for item in self.dir.glob("*.qcow2"))
        free = shutil.disk_usage(self.dir).free
        self.logs.note(f"disk cache {label}: {total / GIB:.1f} GiB in bases, {free / GIB:.1f} GiB free on {self.dir}")

    def stage(self, disk: Path, name: str) -> Path | None:
        """Copy the installed disk to a temporary file in the cache. Return its path, or None when the space is short."""
        self.dir.mkdir(parents=True, exist_ok=True)
        need = disk.stat().st_size + 10 * GIB
        free = shutil.disk_usage(self.dir).free
        if free < need:
            self.logs.note(f"disk cache: no copy of {name}: {free / GIB:.1f} GiB free, {need / GIB:.1f} GiB needed")
            return None
        tmp = self.dir / f".tmp-{os.getpid()}-{name}.qcow2"
        started = time.monotonic()
        shutil.copyfile(disk, tmp)
        self.logs.note(f"disk cache: copied the installed disk {name} to {tmp} in {time.monotonic() - started:.0f} s")
        return tmp

    def commit(self, tmp: Path, name: str, key: dict, *, replace: bool) -> bool:
        """Rename the staged copy to the base of `name`, write the stamp, and prune. Return whether it stored."""
        base, stamp = self.paths(name)
        with (self.dir / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.has(name, key) and not replace:
                tmp.unlink(missing_ok=True)
                self.logs.note(f"disk cache: another run stored {name} first; this copy is dropped")
                return False
            if base.exists():
                with base.open("rb") as old:
                    try:
                        fcntl.flock(old, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except OSError:
                        tmp.unlink(missing_ok=True)
                        self.logs.note(f"disk cache: the base {name} is in use; the new copy is dropped")
                        return False
                    stamp.unlink(missing_ok=True)
                    os.replace(tmp, base)
            else:
                os.replace(tmp, base)
            base.chmod(0o444)
            note = {"key": key, "bytes": base.stat().st_size, "stored": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")}
            part = stamp.with_name(f".tmp-{os.getpid()}-{name}.json")
            part.write_text(json.dumps(note, indent=1, sort_keys=True) + "\n")
            os.replace(part, stamp)
            self.logs.note(f"disk cache: stored the base {name} ({note['bytes'] / GIB:.1f} GiB)")
            self.space("before pruning")
            self.prune(name)
            self.space("after pruning")
        return True

    def prune(self, keep_name: str) -> None:
        """Keep the newest DISK_KEEP bases. Skip a base that a run uses. Remove old temporary files."""
        stamps = sorted((item for item in self.dir.glob("*.json") if not item.name.startswith(".")), key=lambda item: item.stat().st_mtime, reverse=True)
        for stamp in stamps[DISK_KEEP:]:
            if stamp.stem == keep_name:
                continue
            base = stamp.with_suffix(".qcow2")
            try:
                with base.open("rb") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    stamp.unlink(missing_ok=True)
                    base.unlink()
            except FileNotFoundError:
                stamp.unlink(missing_ok=True)
            except OSError:
                self.logs.note(f"disk cache: the base {stamp.stem} is in use; the prune skips it")
                continue
            self.logs.note(f"disk cache: pruned the base {stamp.stem}")
        for item in self.dir.glob(".tmp-*"):
            if time.time() - item.stat().st_mtime > 24 * 3600:
                item.unlink(missing_ok=True)


class Lab:
    """The tools, the ISO, and the folders that every scenario shares."""

    def __init__(self, logs: vmlib.Logs, root: Path, env: dict[str, str]):
        self.logs, self.root, self.env = logs, root, env
        self.work = vmlib.work_dir("install")
        self.qemu: Path
        self.ovmf: Path
        self.iso: Path
        self.count = 0
        self.count_lock = threading.Lock()
        self.workspaces: dict[str, dict[str, str]] = {}
        # The store paths of /etc/basix/inputs on the ISO (BSX-153). `install_vm.main` fills it with `iso_inputs`.
        # Another check that sets `lab.iso` leaves it empty, and `iso_roots` fills it at the first login.
        self.inputs: list[str] = []
        self.inputs_lock = threading.Lock()
        self.disks = DiskCache(logs)

    def iso_roots(self) -> list[str]:
        """The store paths of the ISO that this lab booted. It evaluates them once, at the first call."""
        with self.inputs_lock:
            if not self.inputs:
                self.inputs = iso_inputs(self.logs, self.root, self.env)
            return self.inputs

    def space_check(self, label: str) -> None:
        free, meta = vmlib.root_usage(self.logs)
        self.logs.note(f"host check before {label}: unallocated {free:.2f} GiB, metadata {meta:.1f} %")
        if free < MIN_FREE_GIB:
            raise vmlib.Stop(f"waiting for host space: only {free:.2f} GiB unallocated on / before {label} (need {MIN_FREE_GIB:.0f})")

    def overlay(self, name: str, base: Path) -> Path:
        """A qcow2 overlay on a cached base. The VM writes the overlay only."""
        path = self.work / f"{name}.qcow2"
        argv = [str(self.qemu / "bin/qemu-img"), "create", "-q", "-f", "qcow2", "-b", str(base), "-F", "qcow2", str(path)]
        done = self.logs.run("qemu-img-overlay", argv, qemu=True)
        if done.returncode != 0:
            raise vmlib.Stop(f"qemu-img overlay failed: {done.stderr[-500:]}")
        return path

    def image(self, name: str, gib: int = INSTALL_GIB) -> Path:
        path = self.work / f"{name}.qcow2"
        done = self.logs.run("qemu-img", [str(self.qemu / "bin/qemu-img"), "create", "-q", "-f", "qcow2", str(path), f"{gib}G"], qemu=True)
        if done.returncode != 0:
            raise vmlib.Stop(f"qemu-img failed: {done.stderr[-500:]}")
        return path

    def used_image(self, name: str) -> Path:
        """A 64 GB image with one GPT partition named `precious`."""
        # The work directory is on NTFS, which keeps no sparse file: the backup GPT at the end of a raw file
        # would force a 64 GiB write. The raw file starts on tmpfs, which stays sparse. Only the qcow2 goes to NTFS.
        path = self.work / f"{name}.qcow2"
        tmp = Path(tempfile.mkdtemp(prefix="basix-used-", dir=RAW_DIR))
        try:
            raw = tmp / f"{name}.raw"
            with raw.open("wb") as handle:
                handle.truncate(64 * 1024**3)
            script = 'label: gpt\nstart=2048, size=204800, type=L, name="precious"\n'
            done = subprocess.run(["sfdisk", "--quiet", str(raw)], input=script, text=True, capture_output=True)
            if done.returncode != 0:
                raise vmlib.Stop(f"sfdisk failed: {done.stderr[-500:]}")
            used = raw.stat().st_blocks * 512
            if used > RAW_MAX_BYTES:
                raise vmlib.Stop(f"the raw file is not sparse: {used // 1024**2} MiB allocated in {tmp} (limit {RAW_MAX_BYTES // 1024**2} MiB)")
            done = self.logs.run("qemu-img-convert", [str(self.qemu / "bin/qemu-img"), "convert", "-f", "raw", "-O", "qcow2", str(raw), str(path)], qemu=True)
            if done.returncode != 0:
                raise vmlib.Stop(f"qemu-img convert failed: {done.stderr[-500:]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return path


class Vm:
    def __init__(self, lab: Lab, name: str, shape: str, disk: Path, *, iso: bool, network: bool):
        with lab.count_lock:
            lab.count += 1
            number = lab.count
        self.lab, self.name = lab, name
        folder = lab.work / f"vm{number}-{name}"
        folder.mkdir()
        self.sock = folder / "serial.sock"
        variables = folder / "vars.fd"
        shutil.copyfile(lab.ovmf / "FV/OVMF_VARS.fd", variables)
        variables.chmod(0o600)
        cpu, disk_args, _ = SHAPES[shape]
        argv = [
            str(lab.qemu / "bin/qemu-system-x86_64"),
            *["-enable-kvm", "-machine", "q35" if shape != "pc-bios" else "pc", "-cpu", cpu, "-m", "4096", "-smp", str(vmlib.VM_CPUS)],
            *["-display", "none", "-monitor", f"unix:{folder / 'monitor.sock'},server,nowait"],
            *["-chardev", f"socket,id=ser0,path={self.sock},server=on,wait=off", "-serial", "chardev:ser0"],
            *["-drive", f"file={disk},if=none,id=d0,format=qcow2", *disk_args],
            *(["-nic", "user,model=virtio-net-pci"] if network else ["-nic", "none"]),
        ]
        if shape != "pc-bios":
            argv += [
                *["-drive", f"if=pflash,format=raw,readonly=on,file={lab.ovmf}/FV/OVMF_CODE.fd"],
                *["-drive", f"if=pflash,format=raw,file={variables}"],
            ]
        if iso:
            argv += ["-drive", f"file={lab.iso},media=cdrom,readonly=on,if=ide", "-boot", "order=d"]
        (lab.logs.root / f"{name}-qemu-argv.json").write_text(json.dumps(argv, indent=1) + "\n")
        self.proc = subprocess.Popen(
            argv,
            env=lab.env,
            stdin=subprocess.DEVNULL,
            stdout=(lab.logs.root / f"{name}-qemu.out").open("wb"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=vmlib.limit_memlock,
        )
        self.monitor = folder / "monitor.sock"
        self.console = vmlib.Console(lab.logs, name, sock=self.sock)

    def send_keys(self, *keys: str) -> None:
        """Press keys through the QEMU monitor (the syslinux menu of the BIOS ISO has no serial input)."""
        import socket

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(self.monitor))
            for key in keys:
                sock.sendall(f"sendkey {key}\n".encode())
                time.sleep(0.1)

    KEYS = {" ": "spc", ".": "dot", "/": "slash", "-": "minus", ">": "shift-dot", "|": "shift-backslash", "&": "shift-7", "_": "shift-minus"}

    def type_text(self, text: str) -> None:
        """Type a line on the screen keyboard (QEMU monitor `sendkey`) and press Enter."""
        keys = [self.KEYS.get(ch, f"shift-{ch.lower()}" if ch.isupper() else ch) for ch in text]
        self.send_keys(*keys, "ret")

    def kill(self, hard: bool = False) -> None:
        self.console.close()
        if hard and self.proc.poll() is None:
            os.killpg(self.proc.pid, signal.SIGKILL)  # a power cut: the guest gets no time to flush
            self.proc.wait()
        vmlib.stop_process(self.proc, self.lab.logs, self.name)

    def log_text(self) -> str:
        return vmlib.ANSI.sub("", Path(self.console.log.name).read_text(errors="replace"))

    def power_off(self, from_live: bool = True) -> None:
        self.console.write(b"sudo poweroff\r" if from_live else f"printf '%s\\n' '{PASSWORD}' | sudo -S -p '' poweroff\r".encode())
        started = time.monotonic()
        if self.console.drain_until_exit(self.proc, 240):
            self.lab.logs.note(f"{self.name}: the guest stopped {time.monotonic() - started:.0f} s after poweroff")
        else:
            self.lab.logs.note(f"{self.name}: the guest did not stop in 240 s; the check ends QEMU")
        self.kill()


# --- Driving the live system --------------------------------------------------------------------


def boot_live(vm: Vm, shape: str) -> None:
    console = vm.console
    if shape == "pc-bios":
        # The BIOS ISO boots through syslinux, which reads the keyboard only. Press Home to stop the
        # countdown, and let the first entry boot. The live system has no serial console here, so the
        # check types its commands on the screen keyboard through the QEMU monitor and sends their
        # output to the serial device with `sudo tee /dev/ttyS0`.
        console.wait_for(r"Automatic boot in", 240)
        vm.send_keys("home")
        vm.send_keys("ret")
        for _attempt in range(60):
            vm.type_text("echo BIOS-READY | sudo tee /dev/ttyS0")
            try:
                console.wait_for(r"BIOS-READY", 5)
                break
            except vmlib.Stop:
                continue
        else:
            raise vmlib.Stop("the BIOS live system did not answer on the serial device in 5 minutes")
        vm.lab.logs.note(f"{vm.name}: the BIOS live system answers on /dev/ttyS0")
        return
    else:
        iso_boot.boot_with_serial_console(console)
    console.wait_for(r"\[nixos@[^\]]*\]\$ ", 600)
    vm.lab.logs.note(f"{vm.name}: live prompt reached")
    console.command("systemctl is-system-running --wait", 600)
    if shape != "pc-bios":
        # BSX-204: the live system runs no SSH server and keeps port 22 closed.
        _, state = console.command("systemctl is-active sshd.service")
        _, ports = console.command("ss -ltn | grep -c ':22 '")
        report(state in ("inactive", "unknown") and ports.strip() == "0", f"{vm.name}: no sshd on the live system (state {state!r}, port 22 sockets {ports.strip()!r})")


def basix_command() -> bytes:
    return b"sudo basix install\r"


def answer(console: vmlib.Console, prompt: str, reply: str, timeout: float = 300) -> None:
    console.wait_for(prompt, timeout)
    console.write(reply.encode() + b"\r")


ZONE_PROMPT = r"Time zone \[UTC\]: "
ERROR_LINE = r"basix install: [^\r\n]*\r?\n"
SHELL_PROMPT = r"\[nixos@[^\]]*\]\$ "


def scenario_zone(name: str) -> str | None:
    """The time zone that a scenario types. The nvme scenarios type `ZONE`. The others take the default (`UTC`)."""
    return ZONE if name.startswith("nvme") else None


def answer_zone(console: vmlib.Console, zone: str | None = None) -> None:
    """Answer the time zone question (010-D36). `None` takes the default.

    A zone gets an unknown name first. The installer must print the warning line and ask again.
    """
    if zone is None:
        answer(console, ZONE_PROMPT, "")
        return
    answer(console, ZONE_PROMPT, BAD_ZONE)
    warning = rf'basix install: unknown time zone "{BAD_ZONE}"\. Use the form Region/City, for example America/New_York\.'
    console.wait_for(warning, 60)
    report(True, f"the installer warns about the unknown time zone {BAD_ZONE} and asks again")
    answer(console, ZONE_PROMPT, zone)


def type_answers(console: vmlib.Console, disk_number: str = "1", machine: str = MACHINE, zone: str | None = None) -> None:
    """Answer the questions of `basix install` in the order of BSX-158."""
    answer(console, r"Machine name \[basix\]: ", "" if machine == MACHINE else machine)
    answer(console, r"Account name: ", ACCOUNT)
    answer(console, r"Password: ", PASSWORD)
    answer(console, r"Password again: ", PASSWORD)
    answer_zone(console, zone)
    answer(console, r"Disk number: ", disk_number)


def start_install(console: vmlib.Console) -> None:
    console.write(basix_command())
    console.wait_for(r"Checking network\.\.\.", 120)


def expect(console: vmlib.Console, line: str, timeout: float, stall: float | None = None) -> None:
    """Wait for a step line of the text contract. An error line of the installer before it stops the scenario."""
    found = console.wait_for(rf"{line}|{ERROR_LINE}", timeout, stall=stall)
    if found.group(0).startswith("basix install:"):
        with contextlib.suppress(vmlib.Stop):
            console.wait_for(SHELL_PROMPT, 60, consume=False)
        raise vmlib.Stop(f"the install stopped with: {found.group(0).strip()}\n{console.text()[-1500:]}")


def read_offer(console: vmlib.Console, name: str, *, earlier: bool) -> None:
    """Read the erase offer of a used disk. Stop at the erase prompt, and leave the prompt for the next answer.

    The offer lists what is on the disk. It names an earlier Basix install only when the disk holds the partitions
    that the Basix layout writes (010 review item 1).
    """
    found = console.wait_for(
        r"(?s:This disk already contains data:(.*?)Type erase to destroy everything on this disk: )|" + SHELL_PROMPT,
        600,
        consume=False,
        stall=QUIET_STALL_S,
    )
    if found.group(1) is None:
        raise vmlib.Stop(f"the install returned to the shell before the used-disk offer; console tail:\n{console.text()[-1500:]}")
    offer = found.group(1)
    logs_line = " ".join(offer.split())
    console.logs.note(f"{name}: the erase offer says: {logs_line[:600]}")
    earlier_line = "This may be an earlier Basix install, including one that did not finish."
    if earlier:
        report(earlier_line in offer, f"{name}: the erase offer names an earlier Basix install")
        report("disk-main" in offer, f"{name}: the erase offer lists the Basix partition labels")
    else:
        report("earlier Basix install" not in offer, f"{name}: the erase offer does not claim an earlier Basix install for a foreign disk")
        report("precious" in offer, f"{name}: the erase offer lists what is on the disk (partition `precious`)")


def password_off_console(vm: Vm, label: str) -> None:
    """The console must not show the password. The log of the harness holds its own `>>> SENT` lines, so skip those."""
    shown = [line for line in vm.log_text().splitlines() if not line.startswith(">>> SENT")]
    report(PASSWORD not in "\n".join(shown), f"{label}: the password is not on the console")


STEPS = [
    "Checking your settings...",
    "Partitioning and formatting the disk...",
    "Copying the workspace inputs to the new disk...",
    "Installing the system...",
]


def finish_install(vm: Vm, logs: vmlib.Logs, label: str) -> bool:
    """Wait for the step lines after `Checking your settings...`, then for the final message."""
    started = time.monotonic()
    try:
        # The check after the answers is silent until its end (QUIET_STALL_S). The later steps stream.
        expect(vm.console, r"Partitioning and formatting the disk\.\.\.", 3600, stall=QUIET_STALL_S)
        expect(vm.console, r"Copying the workspace inputs to the new disk\.\.\.", 900)
        expect(vm.console, r"Installing the system\.\.\.", 1800)
        done = vm.console.wait_for(r"Your machine file is ~/basix/system\.py|" + ERROR_LINE, 4500)
        if not done.group(0).startswith("Your machine file"):
            # Read on until the shell prompt, so the log shows the error line and the log tail.
            with contextlib.suppress(vmlib.Stop):
                vm.console.wait_for(SHELL_PROMPT, 60, consume=False)
            raise vmlib.Stop(f"the install stopped with: {done.group(0).strip()}\n{vm.console.text()[-1500:]}")
        vm.console.wait_for(SHELL_PROMPT, 120)
        text = vm.log_text()
        report("run minix init" in text, f"{label}: the final message names minix init")
        positions = [text.find(step) for step in STEPS]
        report(
            all(position >= 0 for position in positions) and positions == sorted(positions),
            f"{label}: the console shows the install steps in order (positions {positions})",
        )
        password_off_console(vm, label)
    except vmlib.Stop as error:
        report(False, f"{label}: the install did not complete:\n{str(error)[-1800:]}")
        return False
    seconds = time.monotonic() - started
    logs.note(f"{label}: install complete {seconds:.0f} s after the check of the settings")
    report(True, f"{label}: `basix install` printed the final workspace instructions ({seconds:.0f} s)")
    _, mem = vm.console.command("free -m | awk '/^Mem:/ {print \"total \" $2 \" MB, available \" $7 \" MB\"}'")
    logs.note(f"{label}: live memory after the install: {mem}")
    return True


def log_in(vm: Vm, label: str) -> None:
    """Log in at the console of the installed disk with the password typed at install."""
    console = vm.console
    console.wait_for(rf"{MACHINE} login: ", 900)
    console.write(ACCOUNT.encode() + b"\r")
    console.wait_for(r"Password: ", 60)
    console.write(PASSWORD.encode() + b"\r")
    console.wait_for(rf"\[{ACCOUNT}@{MACHINE}:[^\]]*\]\$ ", 180)
    vm.lab.logs.note(f"{label}: LOGIN OK")
    report(True, f"{label}: LOGIN OK (the learner logged in with the password typed at install)")


def iso_inputs(logs: vmlib.Logs, root: Path, env: dict[str, str]) -> list[str]:
    """Return the locked source node names. The guest checks the installed roots."""
    lock = json.loads((root / "flake.lock").read_text())
    return ["source", *sorted(set(lock["nodes"]) - {"root"})]


def offline_apply(vm: Vm, label: str) -> None:
    """The first offline `basix apply` on the installed disk (BSX-378).

    The guest has no Python, so `sed` adds one line to `system.py`. The program `git` is already in the installed
    closure, so the edit changes the system with no network fetch. The learner runs `basix apply`; `sudo -v` first gives
    the ticket, so `basix apply` asks for no password. The build is quiet, so the wait uses `QUIET_STALL_S`.
    """
    console = vm.console
    edit = r's/^    serial_console="ttyS0",$/&\n    programs=[Program(pkgs.git)],/'
    rc, _ = console.command(f"sed -i {shlex.quote(edit)} ~/basix/system.py")
    _, found = console.command(r"grep -c '^    programs=\[Program(pkgs.git)\],$' ~/basix/system.py")
    edited = rc == 0 and found.strip() == "1"
    report(edited, f"{label}: sed added the program line to system.py (rc {rc}; lines found {found.strip()!r})")
    if not edited:
        return
    command = f"printf '%s\\n' '{PASSWORD}' | sudo -S -p '' -v && cd ~/basix && basix apply"
    rc, output = console.command(command, 1800, stall=QUIET_STALL_S)
    vm.lab.logs.note(f"{label}: offline basix apply rc {rc}; output tail: {output[-1200:]}")
    report(rc == 0, f"{label}: the first offline basix apply exits 0 (rc {rc})")
    report(APPLIED in output, f"{label}: the apply prints `{APPLIED}`")
    report(re.search(r"saved: .*system\.py", output) is not None, f"{label}: the apply saves system.py")
    _, head = console.command("git -C ~/basix log -1 --format=%s")
    report(head.strip().startswith("apply:"), f"{label}: the last commit is the apply (got {head.strip()!r})")
    _, dirty = console.command("git -C ~/basix status --porcelain")
    report(dirty.strip() == "", f"{label}: the workspace is clean after the apply (got {dirty.strip()[:200]!r})")


def offline_apply_after_gc(vm: Vm, label: str) -> None:
    """An offline `basix apply` after `nix-collect-garbage -d` (BSX-413).

    Root collects the garbage of the installed disk, which has no network. The collection drops every old generation
    and every path that no root holds. The edit then removes the program line that the first apply added. The new
    system differs from the one that the collection removed, so the apply must build it again from the store and the
    locked input sources that the roots keep. The apply must succeed and the new generation must run.
    """
    console = vm.console
    logs = vm.lab.logs
    root_path = "~/basix/.git/basix/compiler"
    _, before = console.command(f"readlink -f /run/current-system; readlink -f {root_path}")
    before_system, before_compiler = (before.splitlines() + ["", ""])[:2]
    report(before_compiler.startswith("/nix/store/"), f"{label}: before the collection, .git/basix/compiler holds a store path (got {before_compiler!r})")
    _, count = console.command("find /nix/var/nix/gcroots/basix -type l | wc -l")
    sudo = f"printf '%s\\n' '{PASSWORD}' | sudo -S -p ''"
    gc_line = f"{sudo} nix-collect-garbage -d >/tmp/gc.log 2>&1; gc=$?; tail -n 3 /tmp/gc.log; (exit $gc)"
    rc, output = console.command(gc_line, 1800, stall=QUIET_STALL_S)
    logs.note(f"{label}: nix-collect-garbage -d rc {rc}; output tail: {output[-600:]}")
    deleted = re.search(r"(\d+) store paths deleted", output)
    report(rc == 0 and deleted is not None and int(deleted.group(1)) > 0, f"{label}: nix-collect-garbage -d deleted store paths (rc {rc}; got {output[-200:]!r})")
    _, generations = console.command("ls /nix/var/nix/profiles | grep -c '^system-[0-9]*-link$'")
    report(generations.strip() == "1", f"{label}: after the collection one system generation stays (got {generations.strip()!r})")
    _, after = console.command(f"readlink -f /run/current-system; readlink -f {root_path}; find /nix/var/nix/gcroots/basix -type l | wc -l")
    after_system, after_compiler, after_roots = (after.splitlines() + ["", "", ""])[:3]
    report(
        after_system == before_system and after_compiler == before_compiler and after_roots.strip() == count.strip(),
        f"{label}: the running system, the compiler root, and the input roots survive the collection",
    )
    _, present = console.command(f"test -e {root_path} && echo yes")
    report(present.strip() == "yes", f"{label}: the compiler root still resolves")
    edit = r"/^    programs=\[Program(pkgs\.git)\],$/d"
    rc, _ = console.command(f"sed -i {shlex.quote(edit)} ~/basix/system.py")
    _, found = console.command(r"grep -c '^    programs=' ~/basix/system.py")
    report(rc == 0 and found.strip() == "0", f"{label}: sed removed the program line from system.py (rc {rc}; lines left {found.strip()!r})")
    rc, output = console.command(f"{sudo} -v && cd ~/basix && basix apply", 1800, stall=QUIET_STALL_S)
    logs.note(f"{label}: apply after the collection rc {rc}; output tail: {output[-1500:]}")
    report(rc == 0, f"{label}: the offline basix apply after the collection exits 0 (rc {rc})")
    report(APPLIED in output, f"{label}: the apply after the collection prints `{APPLIED}`")
    _, now = console.command("readlink -f /run/current-system; readlink -f /nix/var/nix/profiles/system; ls /nix/var/nix/profiles | grep -c '^system-[0-9]*-link$'")
    run, profile, links = (now.splitlines() + ["", "", ""])[:3]
    report(run != "" and run != before_system and run == profile, f"{label}: the new generation runs and is the profile (running {run!r}, profile {profile!r})")
    report(links.strip() == "2", f"{label}: the profile holds two generations (got {links.strip()!r})")
    _, failed = console.command("systemctl is-system-running --wait; systemctl --failed --no-legend --plain", 300)
    report(failed.strip() == "running", f"{label}: the system is running with no failed unit after the apply (got {failed.strip()[:200]!r})")
    _, head = console.command("git -C ~/basix log -1 --format=%s; git -C ~/basix status --porcelain")
    report(head.strip().startswith("apply:"), f"{label}: the apply after the collection is saved and the workspace is clean (got {head.strip()[:200]!r})")


def login_and_check(
    vm: Vm,
    shape: str,
    label: str,
    disk_label: str,
    extra: Callable[[Vm], None] | None = None,
    *,
    reused: bool = False,
    on_proved: Callable[[], None] | None = None,
) -> dict[str, str]:
    """Boot the installed disk without a network and check the account and workspace."""
    console = vm.console
    log_in(vm, label)

    def check(name: str, line: str, want: str | None = None, *, contains: str | None = None, timeout: float = 120) -> str:
        rc, out = console.command(line, timeout)
        value = out.strip().splitlines()[-1].strip() if out.strip() else ""
        ok = rc == 0
        if want is not None:
            ok = ok and value == want
        if contains is not None:
            ok = ok and contains in out
        report(ok, f"{label}: {name} (rc {rc}; got {value[:200]!r})")
        return out.strip()

    check("the account is learner", "id -un", ACCOUNT)
    check("the host name is basix", "hostname", MACHINE)
    check("the system reaches state running", "systemctl is-system-running --wait", "running", timeout=300)
    check("no unit failed", "systemctl --failed --no-legend --plain", "")
    check("the learner owns the workspace", "stat -c %U ~/basix", ACCOUNT)
    check("the workspace has one install commit", "git -C ~/basix rev-list --count HEAD", "1")
    check("the workspace is clean after install", "git -C ~/basix status --porcelain", "")
    tracked = check("the workspace tracks its install files", "git -C ~/basix ls-files")
    for filename in ("flake.nix", "flake.lock", "system.py", "facter.json", "README.md"):
        report(filename in tracked.split(), f"{label}: the commit holds {filename}")
    check("the selected disk uses a stable id", "grep -F '/dev/disk/by-id/' ~/basix/system.py")
    check("the installed system.py holds the serial console of the test ISO (010-D35)", "grep -c '^    serial_console=\"ttyS0\",$' ~/basix/system.py", "1")
    check("sudo works with the install password", f"printf '%s\n' '{PASSWORD}' | sudo -S -k -p '' true")
    check("the VM has no default route", "! ip route | grep -q '^default '")
    roots = check("the installer roots locked input sources", "find /nix/var/nix/gcroots/basix -type l | wc -l")
    report(int(roots or "0") == len(vm.lab.iso_roots()), f"{label}: GC roots cover every input source")
    zone = scenario_zone(label)
    if zone is not None:
        # 010-D36: the installer makes /etc/localtime a link to the typed zone. The core sets no zone of its own.
        check("/etc/localtime points to the typed time zone", "readlink /etc/localtime", contains=f"zoneinfo/{zone}")
        check("timedatectl shows the typed time zone", "timedatectl show -p Timezone --value", zone)
    else:
        _, shown = console.command("timedatectl show -p Timezone --value; readlink /etc/localtime || true")
        vm.lab.logs.note(f"{label}: default time zone: {' | '.join(shown.split())}")

    # Hash the files as the install left them. The apply below adds `system.json` and changes `system.py`.
    _, files = console.command("cd ~/basix && git ls-files -z | xargs -0 sha256sum | sort -k2", 120)
    hashes = {line.split(None, 1)[1]: line.split()[0] for line in files.splitlines() if line.strip()}

    if label == "nvme":
        offline_apply(vm, label)
        offline_apply_after_gc(vm, label)
        # 010 review item 10: `minix init` needs the network and a minix source. The installed-disk VM has no network,
        # and the ISO carries no minix source. A network for this boot is not added.
        vm.lab.logs.note(
            f"{label}: BLOCKER minix-init-installed-disk: `minix init` is not run on the installed disk. "
            "No scenario has a network at that point, and the ISO carries no minix source (010-D28; G27 stays open)."
        )

    if on_proved is not None:
        on_proved()
    if extra is not None:
        extra(vm)
    vm.power_off(from_live=False)
    return hashes


# --- Scenarios ----------------------------------------------------------------------------------


def install_and_login(
    lab: Lab,
    name: str,
    shape: str,
    *,
    disk: Path | None = None,
    erase_first: str | None = None,
    earlier: bool = False,
    extra: Callable[[Vm], None] | None = None,
    cache: str | None = None,
) -> bool:
    """Install on a blank (or given) disk, then boot it alone and log in.

    `cache` is `"use"` or `"store"`, for a blank disk only (see `DiskCache`). Both store the disk of a fresh install, after
    the login checks pass, when no base for the key exists. `"use"` first looks for a base: it boots an overlay of the
    base and skips the install. `"store"` always installs, because it proves the installer. `BASIX_FRESH_INSTALL=1` makes
    `"use"` install and replace the base.
    """
    logs = lab.logs
    key_name, key = ("", {})
    if cache and disk is None and erase_first is None:
        key_name, key = disk_key(lab, shape)
    forced = bool(os.environ.get("BASIX_FRESH_INSTALL"))
    if key_name and cache == "use" and not forced:
        base = lab.disks.lookup(key_name, key)
        if base is not None:
            logs.note(f"{name}: installed disk REUSED: key {key_name}, base {base}")
            report(True, f"{name}: the install is reused from the disk cache (key {key_name}); the installer proof ran when the base was made")
            return boot_installed(lab, name, shape, lab.overlay(name, base), extra, reused=True)
    if key_name:
        why = "BASIX_FRESH_INSTALL=1" if forced else "store mode" if cache == "store" else "no base for the key"
        logs.note(f"{name}: installed disk FRESH ({why}): key {key_name}")
    disk = disk or lab.image(name)
    before = stamp(disk)  # no hash: the case below needs only a change, and a hash of a big image is slow
    baseline = fail_count()
    logs.note(f"{name}: image {disk} size and mtime before {before}")
    lab.space_check(f"{name} install")
    vm = Vm(lab, f"{name}-install", shape, disk, iso=True, network=True)
    try:
        boot_live(vm, shape)
        start_install(vm.console)
        type_answers(vm.console, zone=scenario_zone(name))
        expect(vm.console, r"Checking your settings\.\.\.", 180)
        if erase_first is not None:
            read_offer(vm.console, name, earlier=earlier)
            answer(vm.console, r"Type erase to destroy everything on this disk: ", erase_first)
        ok = finish_install(vm, logs, name)
        if ok:
            vm.power_off()
        else:
            vm.kill()
    except vmlib.Stop as error:
        report(False, f"{name}: {str(error)[-1500:]}")
        vm.kill()
        return False
    after = stamp(disk)
    logs.note(f"{name}: image size and mtime after the install {after}")
    if not ok:
        return False  # The failed install is reported above. A second failure line would repeat it.
    report(before != after, f"{name}: the install changed the image (size and mtime {before} -> {after})")
    if os.environ.get("BASIX_SAVE_DISK"):
        # Development aid: keep the installed disk for `BASIX_UNDO_DISK` runs of `undo_vm.py`.
        shutil.copyfile(disk, os.environ["BASIX_SAVE_DISK"])
        logs.note(f"{name}: BASIX_SAVE_DISK kept a copy of the installed disk")
    # Copy the disk now, while it is as the install left it. The first boot and the steps of `extra` change it.
    staged = None
    if key_name and (forced or not lab.disks.has(key_name, key)):
        lab.disks.space("before the copy")
        staged = lab.disks.stage(disk, key_name)
    proved = {"ok": False}

    def mark_proved() -> None:
        proved["ok"] = fail_count() == baseline

    try:
        booted = boot_installed(lab, name, shape, disk, extra, on_proved=mark_proved)
        if staged is not None:
            if proved["ok"]:
                lab.disks.commit(staged, key_name, key, replace=forced)
            else:
                logs.note(f"{name}: the disk cache does not keep this install: a check failed before the steps of the run")
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)
    return booted


def boot_installed(
    lab: Lab,
    name: str,
    shape: str,
    disk: Path,
    extra: Callable[[Vm], None] | None = None,
    *,
    reused: bool = False,
    on_proved: Callable[[], None] | None = None,
) -> bool:
    lab.space_check(f"{name} boot")
    vm = Vm(lab, f"{name}-login", shape, disk, iso=False, network=False)
    try:
        hashes = login_and_check(vm, shape, name, name, extra, reused=reused, on_proved=on_proved)
    except vmlib.Stop as error:
        report(False, f"{name}: login check: {str(error)[-1800:]}")
        if reused:
            lab.logs.note(f"{name}: the reused base may be bad. Run again with BASIX_FRESH_INSTALL=1 to install and replace it.")
        vm.kill()
        return False
    lab.workspaces[name] = hashes
    return True


def scenario_nvme(lab: Lab) -> None:
    if not install_and_login(lab, "nvme", "q35-nvme", cache="store"):
        # The cache run needs the stored disk of the first run. Without it, the second run repeats the same failure.
        lab.logs.note("nvme-cache: skipped, because nvme failed")
        return
    install_and_login(lab, "nvme-cache", "q35-nvme", cache="use")


def compare_workspaces(lab: Lab) -> None:
    """Compare the nvme and used-disk (sata) workspaces. It runs after both scenarios end, so the order does not matter."""
    nvme, sata = lab.workspaces.get("nvme"), lab.workspaces.get("used-disk")
    if nvme and sata:
        differ = sorted(path for path in set(nvme) | set(sata) if nvme.get(path) != sata.get(path))
        lab.logs.note(f"nvme and used-disk workspaces differ in: {differ}")
        report(
            set(differ) <= {"system.py", "system.json", "facter.json"},  # system.json is compiled from system.py (disk id)
            f"used-disk: the SATA workspace differs only in disk and hardware data (differ: {differ})",
        )


def refusal(lab: Lab, name: str, shape: str, disk: Path, *, network: bool, wants: list[str], answers=None) -> None:
    """Run `basix install` where it must refuse, and prove that the image does not change."""
    logs = lab.logs
    before = sha256(disk)
    logs.note(f"{name}: image sha256 before {before}")
    lab.space_check(name)
    vm = Vm(lab, name, shape, disk, iso=True, network=network)
    try:
        boot_live(vm, shape)
        console = vm.console
        console.write(basix_command())
        if answers:
            answers(console)
        console.wait_for(r"\[nixos@[^\]]*\]\$ ", 600)
        text = vm.log_text()
        for wanted in wants:
            report(wanted in text, f"{name}: the console shows: {wanted}")
        rc, _ = console.command("true")
        vm.kill()
    except vmlib.Stop as error:
        report(False, f"{name}: {str(error)[-1500:]}")
        vm.kill()
        return
    after = sha256(disk)
    report(before == after, f"{name}: the image is unchanged (sha256 {before[:16]}...)")


def scenario_bios(lab: Lab) -> None:
    """`pc-bios`: the install refuses before it touches the disk (BSX-133)."""
    disk = lab.image("bios")
    before = sha256(disk)
    lab.logs.note(f"bios: image sha256 before {before}")
    lab.space_check("bios")
    vm = Vm(lab, "bios", "pc-bios", disk, iso=True, network=True)
    try:
        boot_live(vm, "pc-bios")
        vm.type_text("sudo basix install 2>&1 | sudo tee /dev/ttyS0")
        # Wait for the last line of the message: the serial line delivers it in pieces.
        vm.console.wait_for(r"No disk was changed\.\r?\n", 300)
        text = vm.log_text()
        for wanted in ("basix install: UEFI firmware is required.", "Enable UEFI boot and start the ISO again", "No disk was changed."):
            report(wanted in text, f"bios: the console shows: {wanted}")
        time.sleep(5)
    except vmlib.Stop as error:
        report(False, f"bios: {str(error)[-1500:]}")
    vm.kill()
    after = sha256(disk)
    report(before == after, f"bios: the image is unchanged (sha256 {before[:16]}...)")


def scenario_no_network(lab: Lab) -> None:
    disk = lab.image("nonet")

    def drive(console: vmlib.Console) -> None:
        console.wait_for(r"No working network connection", 180)
        answer(console, r"Run nmtui now\? \[y/N\] ", "n", 60)

    refusal(lab, "no-network", "q35-sata", disk, network=False, wants=["basix install: cannot reach the network", "Connect a network cable", "No disk was changed"], answers=drive)


def scenario_small_disk(lab: Lab) -> None:
    disk = lab.image("small", gib=55)

    def drive(console: vmlib.Console) -> None:
        answer(console, r"Machine name \[basix\]: ", "")
        answer(console, r"Account name: ", ACCOUNT)
        answer(console, r"Password: ", PASSWORD)
        answer(console, r"Password again: ", PASSWORD)
        answer_zone(console)
        console.wait_for(r"no disk with a /dev/disk/by-id name is available and 64 GB or larger", 120)

    refusal(lab, "small-disk", "q35-nvme", disk, network=True, wants=["no disk with a /dev/disk/by-id name is available and 64 GB or larger", "No disk was changed"], answers=drive)


def scenario_used_disk(lab: Lab) -> None:
    logs = lab.logs
    disk = lab.used_image("used")
    before = sha256(disk)
    lab.space_check("used-disk refusal")
    vm = Vm(lab, "used-refuse", "q35-sata", disk, iso=True, network=True)
    try:
        boot_live(vm, "q35-sata")
        start_install(vm.console)
        type_answers(vm.console)
        expect(vm.console, r"Checking your settings\.\.\.", 180)
        read_offer(vm.console, "used-disk", earlier=False)
        answer(vm.console, r"Type erase to destroy everything on this disk: ", "yes please", 60)
        vm.console.wait_for(r"basix install: erase refused\. No disk was changed\.", 60)
        report(True, "used-disk: a wrong word at the erase prompt prints `basix install: erase refused. No disk was changed.`")
        vm.console.wait_for(SHELL_PROMPT, 60)
        vm.kill()
    except vmlib.Stop as error:
        report(False, f"used-disk: {str(error)[-1500:]}")
        vm.kill()
        return
    after = sha256(disk)
    report(before == after, f"used-disk: the image is unchanged after the refusal (sha256 {before[:16]}...)")
    logs.note(f"used-disk: refusal hashes {before} -> {after}")
    if before != after:
        # The erase install needs a used disk. The disk changed, so the proof of the refusal failed.
        logs.note("used-disk: the erase install is skipped, because the refusal changed the image")
        return
    install_and_login(lab, "used-disk", "q35-sata", disk=disk, erase_first="erase")


def scenario_cut_install(lab: Lab) -> None:
    logs = lab.logs
    disk = lab.image("cut")
    before = stamp(disk)
    lab.space_check("cut install")
    vm = Vm(lab, "cut-first", "q35-nvme", disk, iso=True, network=True)
    try:
        boot_live(vm, "q35-nvme")
        start_install(vm.console)
        type_answers(vm.console)
        expect(vm.console, r"Checking your settings\.\.\.", 180)
        # The cut comes when `nixos-install` copies the system: the disk script ran, /mnt is mounted, and the partitions,
        # their Basix labels, and the file systems exist. The `nix copy` of the inputs comes first and prints
        # `copying path` too, so wait for the step line `Installing the system...` before the first copy line.
        expect(vm.console, r"Partitioning and formatting the disk\.\.\.", 3600, stall=QUIET_STALL_S)
        expect(vm.console, r"Copying the workspace inputs to the new disk\.\.\.", 900)
        expect(vm.console, r"Installing the system\.\.\.", 1800)
        # The evaluation inside `nixos-install` is silent until its first copy line.
        progress = vm.console.wait_for(r"copying path|" + ERROR_LINE + "|" + SHELL_PROMPT, 2400, stall=QUIET_STALL_S)
        if not progress.group(0).startswith("copying path"):
            raise vmlib.Stop(f"the install ended before the system copy: {progress.group(0).strip()}\n{vm.console.text()[-1500:]}")
        logs.note("cut-install: the check kills QEMU when the system copy starts")
        vm.kill(hard=True)
    except vmlib.Stop as error:
        report(False, f"cut-install: {str(error)[-1500:]}")
        vm.kill()
        return
    if not report(stamp(disk) != before, "cut-install: the cut install had written to the image"):
        # The retry needs the disk that the cut install left behind. A blank disk gives no erase offer.
        logs.note("cut-install: the retry is skipped, because the first install wrote nothing")
        return
    install_and_login(lab, "cut-install", "q35-nvme", disk=disk, erase_first="erase", earlier=True)


SCENARIO_FUNCTIONS = {
    "nvme": scenario_nvme,
    "bios": scenario_bios,
    "no-network": scenario_no_network,
    "small-disk": scenario_small_disk,
    "used-disk": scenario_used_disk,
    "cut-install": scenario_cut_install,
}


def parallel_count() -> int:
    """The number of scenarios that run at once: `$BASIX_PARALLEL`, default 1."""
    text = os.environ.get("BASIX_PARALLEL", "").strip()
    if not text:
        return DEFAULT_PARALLEL
    try:
        number = int(text)
    except ValueError:
        raise SystemExit(f"BASIX_PARALLEL must be a whole number of 1 or more, not {text!r}") from None
    if number < 1:
        raise SystemExit(f"BASIX_PARALLEL must be a whole number of 1 or more, not {text!r}")
    return number


#: Scenarios with two installs start first, so the short ones fill the gaps.
LONG_FIRST = ["cut-install", "used-disk", "nvme", "small-disk", "no-network", "bios"]


def run_scenario(lab: Lab, name: str) -> tuple[str, list[tuple[bool, str]], float]:
    """Run one scenario in this thread. A failure here never reaches the other scenarios."""
    _HERE.results = []
    started = time.monotonic()
    lab.logs.note(f"=== scenario {name} start")
    try:
        SCENARIO_FUNCTIONS[name](lab)
    except vmlib.Stop as error:
        report(False, f"{name}: {str(error)[-1500:]}")
    except Exception:  # noqa: BLE001 - one broken scenario must not stop the others
        report(False, f"{name}: unexpected error:\n{traceback.format_exc()[-1800:]}")
    seconds = time.monotonic() - started
    lab.logs.note(f"=== scenario {name} took {seconds:.0f} s")
    return name, _HERE.results, seconds


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    os.environ.setdefault("BASIX_WORK", str(workdir.default_work("010/P4/p4-vm")))
    os.environ.setdefault("BASIX_LOGS", str(Path.home() / ".local/state/basix/010/P4/p4-vm/logs"))
    wanted = argv[2:] or SCENARIOS
    unknown = [name for name in wanted if name not in SCENARIO_FUNCTIONS]
    if unknown:
        hint = " (the scenario sata is gone: used-disk runs the SATA shape)" if "sata" in unknown else ""
        print(f"unknown scenario: {unknown}; known: {SCENARIOS}{hint}")
        return 2
    parallel = parallel_count()
    logs = vmlib.Logs.create("install_vm")
    env = vmlib.clean_env()
    env["TMPDIR"] = str(vmlib.work_dir("vmtmp"))
    lab = Lab(logs, root, env)
    summary: list[tuple[str, list[tuple[bool, str]], float]] = []
    began = time.monotonic()
    try:
        logs.note(f"raw logs: {logs.root}; scenarios {wanted}; {parallel} at a time")
        lab.space_check("the ISO build")
        lab.iso = iso_boot.build_iso(logs, root, env)
        lab.inputs = iso_inputs(logs, root, env)
        logs.note(f"iso {lab.iso} sha256 {sha256(lab.iso)}")
        lab.qemu, lab.ovmf = vmlib.qemu_tools(logs, root, env)
        if parallel == 1:
            order = wanted
        else:
            order = sorted(wanted, key=lambda name: LONG_FIRST.index(name))
        with ThreadPoolExecutor(max_workers=min(parallel, len(order)) or 1) as pool:
            futures = [pool.submit(run_scenario, lab, name) for name in order]
            summary = [future.result() for future in futures]
        if "nvme" in wanted and "used-disk" in wanted:
            _HERE.results = []
            compare_workspaces(lab)
    except vmlib.Stop as error:
        report(False, str(error))
    failed = [text for ok, text in RESULTS if not ok]
    print(f"--- scenarios ({parallel} at a time, {time.monotonic() - began:.0f} s in all)")
    for name, results, seconds in sorted(summary, key=lambda item: wanted.index(item[0])):
        bad = sum(1 for ok, _ in results if not ok)
        line = f"{name}: {len(results) - bad} passed, {bad} failed, {seconds:.0f} s"
        print(line)
        logs.note(f"summary {line}")
    # Delete the scratch images after a good run. A bad run keeps them for the next look.
    if not failed:
        shutil.rmtree(lab.work, ignore_errors=True)
    else:
        print(f"scratch kept: {lab.work}")
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed; logs {logs.root}")
    print("GREEN" if not failed else "RED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
