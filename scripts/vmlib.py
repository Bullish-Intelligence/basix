"""Shared helpers of the VM checks (`iso_boot.py`, `guard_vm.py`). Standard library only.

Rules that every VM check keeps (AGENTS.md, host safety):
- QEMU gets image files and the ISO only, never a host block device;
- VM images, state, and scratch live under `$BASIX_WORK` (default `~/.cache/basix/b5`);
- raw logs live under `$BASIX_LOGS` (default `~/.local/state/basix/b5/logs`), never in the repository;
- the check stops before a build when metadata is above 90 % and unallocated space is under 15 GiB.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import os
import re
import resource
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"  # control sequences
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # operating-system commands (window title, shell integration)
    r"|\x1bP[^\x1b]*\x1b\\"  # device control strings
    r"|\x1b[()][0-9A-B]|\x1b[=>]"
)
#: CPU count of every test VM. Set `BASIX_VM_CPUS` to change it. Four CPUs per VM oversubscribed the 8 host CPUs when
#: the install check ran three scenarios at once (`BASIX_PARALLEL`), so the default is 2. See B7-INSTALL-UNDO.md.
VM_CPUS = int(os.environ.get("BASIX_VM_CPUS", "2"))
MIN_UNALLOCATED_GIB = 15.0
MAX_METADATA_PERCENT = 90.0
#: QEMU 11 fails at start with "Failed to initialize io_uring: Cannot allocate memory" on this host
#: when the locked-memory limit is large. A 64 KiB limit makes it skip io_uring.
MEMLOCK_BYTES = 64 * 1024
#: A wait on the serial console stops when the console shows no new output for this many seconds (the rule is "no
#: progress", not "a short total"). Set `BASIX_STALL_S` to change it. A step that is silent by design passes its own
#: bound to `wait_for` or `command` (`stall=`).
STALL_S = float(os.environ.get("BASIX_STALL_S", "120"))
_UNITS = {"B": 1 / 2**30, "KiB": 1 / 2**20, "MiB": 1 / 2**10, "GiB": 1.0, "TiB": 2**10}


# The scripts import `src/` from the checkout. Python must not write `__pycache__` folders there: a `path:` flake would
# copy them into the build, and the ISO would differ from the ISO of a clean checkout. `nix/source.nix` also leaves
# them out (defence in depth).
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workdir  # noqa: E402
from basix.units import ordering_cycles, summary  # noqa: E402


class Stop(Exception):
    """A check cannot go on. The message names the reason."""


class Stall(Stop):
    """The console showed no new output for the stall bound. The VM is hung or lost."""


@dataclass
class _Done:
    code: int
    out: str


class HostRun:
    """The runner of `basix.units`, with a plain subprocess. A missing program gives code 127."""

    def run(self, argv: list[str], *, cwd: Path | None = None) -> _Done:
        try:
            done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return _Done(127, f"{argv[0]}: command not found\n")
        return _Done(done.returncode, done.stdout + done.stderr)


def refuse_cycles(top: Path, label: str) -> None:
    """Stop the check when the built system `top` has a systemd ordering cycle (BSX-411, 010-D56).

    A VM that boots such a system fails in a different way on each boot, so the check proves nothing.
    """
    cycles = ordering_cycles(HostRun(), top)
    if cycles:
        raise Stop(f"{label}: the built system has {len(cycles)} systemd ordering cycle(s). Fix the unit order.\n{summary(cycles)}")


def limit_memlock() -> None:
    """Run in a child before exec: lower the locked-memory limit so QEMU skips io_uring."""
    resource.setrlimit(resource.RLIMIT_MEMLOCK, (MEMLOCK_BYTES, MEMLOCK_BYTES))


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "UV_PROJECT_ENVIRONMENT"}
    env["NO_COLOR"] = "1"
    return env


@dataclass
class Logs:
    """Writes each command, its exit status, and its output outside the repository."""

    root: Path
    count: int = 0
    notes: list[str] = field(default_factory=list)
    #: Guards `count` and `notes.log`, so scenarios that run in threads share one `Logs` safely.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def create(cls, name: str, base: Path | None = None) -> Logs:
        base = base or Path(os.environ.get("BASIX_LOGS", str(Path.home() / ".local/state/basix/b5/logs")))
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        root = base / name / stamp
        root.mkdir(parents=True, exist_ok=True)
        return cls(root)

    def note(self, line: str) -> None:
        stamp = datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")
        with self.lock, (self.root / "notes.log").open("a") as handle:
            handle.write(f"{stamp} {line}\n")
        print(line, flush=True)

    def run(
        self,
        name: str,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        qemu: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        with self.lock:
            self.count += 1
            number = self.count
        stem = f"{number:03d}-{re.sub(r'[^A-Za-z0-9_.-]+', '_', name)}"
        started = time.monotonic()
        try:
            done = subprocess.run(
                argv,
                cwd=cwd,
                env=env or clean_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                preexec_fn=limit_memlock if qemu else None,
            )
        except subprocess.TimeoutExpired as error:
            (self.root / f"{stem}.cmd").write_text(json.dumps({"argv": argv, "timeout": timeout}) + "\n")
            for suffix, data in (("out", error.stdout), ("err", error.stderr)):
                text = data.decode(errors="replace") if isinstance(data, bytes) else (data or "")
                (self.root / f"{stem}.{suffix}").write_text(text)
            raise
        record = {"argv": argv, "cwd": str(cwd), "returncode": done.returncode, "seconds": round(time.monotonic() - started, 1)}
        (self.root / f"{stem}.cmd").write_text(json.dumps(record, indent=1) + "\n")
        (self.root / f"{stem}.out").write_text(done.stdout)
        (self.root / f"{stem}.err").write_text(done.stderr)
        return done


def _gib(number: str, unit: str) -> float:
    return float(number) * _UNITS[unit]


def root_usage(logs: Logs) -> tuple[float, float]:
    """Return unallocated GiB and the metadata use in percent of the root file system.

    When `/` is not btrfs, it has neither: return the free GiB of `/` and 0 % metadata.
    """
    if root_fstype() != "btrfs":
        return shutil.disk_usage("/").free / 2**30, 0.0
    done = logs.run("btrfs-usage", ["btrfs", "filesystem", "usage", "/"])
    unallocated = re.search(r"Device unallocated:\s+([\d.]+)(\w+)", done.stdout)
    metadata = re.search(r"Metadata,\w+: Size:[\d.]+\w+, Used:[\d.]+\w+ \(([\d.]+)%\)", done.stdout)
    if not (unallocated and metadata):
        raise Stop(f"cannot read btrfs usage:\n{done.stdout}\n{done.stderr}")
    return _gib(*unallocated.groups()), float(metadata.group(1))


def root_fstype() -> str:
    """The file system type of `/`: the last mount of `/` in `/proc/mounts`."""
    found = ""
    for line in Path("/proc/mounts").read_text().splitlines():
        fields = line.split()
        if len(fields) > 2 and fields[1] == "/":
            found = fields[2]
    return found


def disk_guard(logs: Logs, label: str) -> None:
    """Stop before a build when metadata is above 90 % while unallocated space is under 15 GiB.

    This is the rule of the plan ("Rules for every slice"). Low unallocated space alone only
    prints a warning, because other work on the host also uses it. When `/` is not btrfs, it has no
    metadata or unallocated space: the guard then stops when free space on `/` is under 15 GiB.
    """
    fstype = root_fstype()
    if fstype != "btrfs":
        free = shutil.disk_usage("/").free / 2**30
        logs.note(f"disk guard {label}: / is {fstype or 'unknown'}, free {free:.2f} GiB")
        if free < MIN_UNALLOCATED_GIB:
            raise Stop(f"stop: only {free:.2f} GiB free on / before {label}")
        return
    for attempt in (1, 2):
        free, meta = root_usage(logs)
        logs.note(f"disk guard {label} try {attempt}: unallocated {free:.2f} GiB, metadata {meta:.1f} %")
        if free < MIN_UNALLOCATED_GIB:
            logs.note(f"warning: unallocated space is under {MIN_UNALLOCATED_GIB:.0f} GiB")
        if not (meta > MAX_METADATA_PERCENT and free < MIN_UNALLOCATED_GIB):
            return
        if attempt == 1:
            time.sleep(60)
    raise Stop(f"stop: metadata {meta:.1f} % and only {free:.2f} GiB unallocated on / before {label}")


def work_dir(sub: str) -> Path:
    """A fresh directory for VM files and scratch (`$BASIX_WORK`, else the cache folder of the user)."""
    base = Path(os.environ.get("BASIX_WORK") or workdir.default_work("b5"))
    path = base / sub
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def locked_rev(root: Path, name: str) -> str:
    data = json.loads((root / "flake.lock").read_text())
    node = data["nodes"]["root"]["inputs"][name]
    return data["nodes"][node]["locked"]["rev"]


def qemu_tools(logs: Logs, root: Path, env: dict[str, str]) -> tuple[Path, Path]:
    """Build QEMU and OVMF from the pinned nixpkgs. Return (qemu, ovmf)."""
    rev = locked_rev(root, "nixpkgs")
    flake = f"github:NixOS/nixpkgs/{rev}"
    done = logs.run(
        "nix-build-tools",
        ["nix", "build", "--no-link", "--print-out-paths", f"{flake}#qemu_kvm", f"{flake}#OVMF.fd"],
        env=env,
        timeout=1800,
    )
    if done.returncode != 0:
        raise Stop(f"cannot build QEMU and OVMF: {done.stderr[-1500:]}")
    paths = [Path(p) for p in done.stdout.split()]
    qemu = next((p for p in paths if (p / "bin/qemu-system-x86_64").exists()), None)
    ovmf = next((p for p in paths if (p / "FV/OVMF_CODE.fd").exists()), None)
    if qemu is None or ovmf is None:
        raise Stop(f"QEMU or OVMF not found in {paths}")
    logs.note(f"qemu {qemu}; ovmf {ovmf}")
    return qemu, ovmf


class Console:
    """A serial console: a unix socket (server side opened by QEMU) or the pipes of a process."""

    def __init__(self, logs: Logs, name: str, *, sock: Path | None = None, proc: subprocess.Popen[bytes] | None = None):
        self.buf = ""
        self.sock: socket.socket | None = None
        self.proc = proc
        self.logs = logs
        self.name = name
        #: The time (monotonic) of the last output byte. `wait_for` measures the stall from it.
        self.last_data = time.monotonic()
        self.log = (logs.root / f"{name}-serial.log").open("ab")
        if sock is not None:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            deadline = time.time() + 120
            while True:
                try:
                    self.sock.connect(str(sock))
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    if time.time() > deadline:
                        raise Stop(f"cannot connect to the serial socket {sock}") from None
                    time.sleep(0.5)

    def _read(self, timeout: float) -> bytes:
        if self.sock is not None:
            self.sock.settimeout(timeout)
            try:
                data = self.sock.recv(65536)
            except TimeoutError:
                return b""
            if not data:
                raise Stop("the VM closed its console")
            self.last_data = time.monotonic()
            return data
        assert self.proc is not None and self.proc.stdout is not None
        import select

        ready, _, _ = select.select([self.proc.stdout], [], [], timeout)
        if not ready:
            return b""
        data = os.read(self.proc.stdout.fileno(), 65536)
        if not data:
            raise Stop("the VM closed its console")
        self.last_data = time.monotonic()
        return data

    def write(self, data: bytes) -> None:
        self.log.write(b"\n>>> SENT " + repr(data).encode() + b"\n")
        self.log.flush()
        if self.sock is not None:
            self.sock.sendall(data)
        else:
            assert self.proc is not None and self.proc.stdin is not None
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def drain_until_exit(self, proc: subprocess.Popen[bytes], timeout: float) -> bool:
        """Read the console until the QEMU process `proc` exits. Return False after `timeout` seconds.

        The guest prints its shutdown messages to the serial line. When nobody reads them, the buffer of
        QEMU fills and the guest stops until the reader comes back. So a wait for the end of a VM reads the
        console too.
        """
        deadline = time.time() + timeout
        while proc.poll() is None:
            if time.time() > deadline:
                return False
            try:
                data = self._read(1.0)
            except (OSError, Stop):
                data = b""  # QEMU closed the console: it is exiting
                time.sleep(0.2)
            if data:
                self.buf += data.decode(errors="replace")
                self.log.write(data)
                self.log.flush()
            elif self.sock is not None and proc.poll() is None:
                time.sleep(0.05)
        return True

    def text(self) -> str:
        """The unconsumed console text with the terminal control sequences removed."""
        return ANSI.sub("", self.buf)

    def wait_for(self, pattern: str, timeout: float, *, consume: bool = True, stall: float | None = None) -> re.Match[str]:
        """Wait until the regular expression matches the stream after the last consumed point.

        Two bounds apply. `timeout` is the total wait. `stall` (default `STALL_S`) is the time with no new console
        output; the wait raises `Stall` when it passes. The stall time counts from the later of the last output and
        the start of this call, so host-side work between two waits never counts.
        """
        rx = re.compile(pattern)
        limit = STALL_S if stall is None else stall
        began = time.monotonic()
        deadline = time.time() + timeout
        while True:
            text = self.text()
            match = rx.search(text)
            if match:
                if consume:
                    self.buf = text[match.end():]
                return match
            if time.time() > deadline:
                tail = self.text()[-2500:]
                raise Stop(f"pattern {pattern!r} not seen in {timeout:.0f} s; console tail:\n{tail}")
            data = self._read(2.0)
            if data:
                self.buf += data.decode(errors="replace")
                self.log.write(data)
                self.log.flush()
                continue
            quiet = time.monotonic() - max(self.last_data, began)
            if quiet > limit:
                tail = self.text()[-2500:]
                self.logs.note(f"{self.name}: STALL: no console output for {quiet:.0f} s (bound {limit:.0f} s) while waiting for {pattern!r}")
                raise Stall(f"no console output for {quiet:.0f} s (bound {limit:.0f} s) while waiting for {pattern!r}; console tail:\n{tail}")

    def command(self, line: str, timeout: float = 120, *, stall: float | None = None) -> tuple[int, str]:
        """Run one shell command and return (exit status, output). The shell must be at a prompt.

        The command goes between two marker lines. The terminal echoes the typed line and can wrap
        it. The typed line never holds a marker as one string (`printf ... __BEGIN_ TOKEN` has a
        space), so only the output of `printf` matches. The output is the text between the markers.
        """
        token = secrets.token_hex(4)
        begin, end = f"__BEGIN_{token}", f"__END_{token}"
        self.write(f"printf '\\n%s%s\\n' __BEGIN_ {token}; {line}; printf '\\n%s%s:%s\\n' __END_ {token} \"$?\"\r".encode())
        match = self.wait_for(rf"(?m)^{end}:(\d+)\r?\n", timeout, consume=False, stall=stall)
        text = self.text()
        out = text[: match.start()]
        self.buf = text[match.end():]
        start = re.search(rf"(?m)^{begin}[ \t]*\r?$", out)
        if start is None:
            raise Stop(f"the start marker of the command {line!r} is missing; console text:\n{out[-1500:]}")
        body = out[start.end():].replace("\r", "")
        return int(match.group(1)), body.strip()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            if self.sock is not None:
                self.sock.close()
        with contextlib.suppress(Exception):
            self.log.close()


def stop_process(proc: subprocess.Popen[bytes] | None, logs: Logs, name: str) -> None:
    """Kill the QEMU process group. The checks power nothing off in the guest."""
    if proc is None:
        return
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    logs.note(f"{name}: QEMU exit status {proc.returncode}")
