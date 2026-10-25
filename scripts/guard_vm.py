"""Check the Basix disk guard in a NixOS VM with qcow2 fixture disks.

Usage: python3 -I scripts/guard_vm.py [ROOT]      (run by `testee verify opt:basix-guard-vm`)

The check compiles a copy of `fixtures/p4-guard` in the scratch folder; it never writes to the repository.

One VM boots once with four pairs of fresh qcow2 disks, one pair for each case: `used` (a partition on the disk),
`mnt` (a filesystem mounted below /mnt), `under` (a disk of 55 GB), and `blank` (two good disks). The cases run in
turn. Each case uses its own pair, and removes its mount and its disk names at the end, so the result of one case
never depends on the case before. A case stops at its first failed precondition. A wait on the console stops after
`vmlib.STALL_S` seconds with no new output. At the end the check compares the hash of every image with its hash before
the VM started.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import vmlib  # noqa: E402

GIB = 1024**3
EXTRA = "/dev/disk/by-id/virtio-vd-extra"
TARGET = "/dev/disk/by-id/virtio-vd-target"
STOP_TEXT = "the guard stopped disko before it wrote to the disk"
RESULTS: list[tuple[bool, str]] = []


def report(ok: bool, text: str) -> None:
    RESULTS.append((ok, text))
    print(("PASS " if ok else "FAIL ") + text, flush=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


def mbr_with_partition() -> bytes:
    sector = bytearray(512)
    sector[446:462] = struct.pack("<B3sB3sII", 0, b"\x00\x21\x00", 0x83, b"\xff\xff\xff", 2048, 2048)
    sector[510:512] = b"\x55\xaa"
    return bytes(sector)


def build(logs: vmlib.Logs, root: Path, env: dict[str, str]) -> tuple[Path, Path]:
    # The compiler writes `system.json`. Copy the fixture to the scratch folder first, so the check never rewrites the
    # tracked file `fixtures/p4-guard/system.json`.
    fixture = vmlib.work_dir("guard-fixture") / "p4-guard"
    shutil.copytree(root / "fixtures/p4-guard", fixture)
    compiler = logs.run(
        "build-compiler",
        ["nix", "build", f"path:{root}#compiler", "--no-link", "--print-out-paths", "--no-write-lock-file"],
        cwd=root,
        env=env,
        timeout=3600,
    )
    if compiler.returncode or not compiler.stdout.strip():
        raise vmlib.Stop(f"the compiler did not build: {compiler.stderr[-1500:]}")
    compiler_out = compiler.stdout.strip().splitlines()[-1]
    compiled = logs.run(
        "compile-fixture",
        [f"{compiler_out}/bin/basix-compile", "system.py", "system.json"],
        cwd=fixture,
        env=env,
        timeout=300,
    )
    if compiled.returncode:
        raise vmlib.Stop(f"guard fixture compile failed: {compiled.stderr[-1200:]}")

    flake = f"path:{fixture}"
    build_start = time.monotonic()
    for name, attr in (
        ("disko-script", "config.system.build.diskoScript"),
        ("vm", "config.system.build.vm"),
    ):
        done = logs.run(
            f"nix-build-{name}",
            [
                "nix", "build", "--no-link", "--print-out-paths",
                "--override-input", "basix", f"path:{root}",
                f"{flake}#nixosConfigurations.guardvm.{attr}",
            ],
            cwd=root,
            env=env,
            timeout=3600,
        )
        if done.returncode:
            raise vmlib.Stop(f"cannot build guard {name}: {done.stderr[-2000:]}")
        path = Path(done.stdout.strip().splitlines()[-1])
        if name == "disko-script":
            script = path
        else:
            vm_out = path
    runner = next((vm_out / "bin").glob("run-*-vm"), None)
    if runner is None:
        raise vmlib.Stop(f"no VM runner in {vm_out}")
    logs.note(f"guard fixture compiled; disk script {script}; VM runner {runner}; build seconds {time.monotonic() - build_start:.1f}")
    return runner, script


def make_image(logs: vmlib.Logs, qemu: Path, folder: Path, name: str, size_gib: int, used: bool) -> Path:
    image = folder / f"{name}.qcow2"
    tool = qemu / "bin/qemu-img"
    if used:
        raw = Path("/dev/shm") / f"p4-guard-{os.getpid()}-{name}.raw"
        try:
            with raw.open("wb") as handle:
                handle.truncate(size_gib * GIB)
                handle.write(mbr_with_partition())
            allocated = raw.stat().st_blocks * 512
            if allocated > 64 * 1024**2:
                raise vmlib.Stop(f"raw fixture is not sparse: {allocated} bytes allocated")
            done = logs.run("qemu-img-used", [str(tool), "convert", "-f", "raw", "-O", "qcow2", str(raw), str(image)], qemu=True)
        finally:
            raw.unlink(missing_ok=True)
    else:
        done = logs.run("qemu-img-create", [str(tool), "create", "-q", "-f", "qcow2", str(image), f"{size_gib}G"], qemu=True)
    if done.returncode:
        raise vmlib.Stop(f"qemu-img failed: {done.stderr[-1000:]}")
    return image




@dataclass(frozen=True)
class Case:
    """One case and the shape of its disk pair. Each case has its own pair."""

    name: str
    size_gib: int = 64  # the size of the extra disk
    used: bool = False  # the extra disk holds a partition
    mount: bool = False  # the case mounts a filesystem below /mnt


#: The cases of the check, in order.
CASES = [Case("used", used=True), Case("mnt", mount=True), Case("under", size_gib=55), Case("blank")]


def disk_name(role: str, case: str) -> str:
    """The `serial` of a case disk. The guest gets the link `/dev/disk/by-id/virtio-SERIAL` from it."""
    return f"vd-{role}-{case}"


def make_pairs(logs: vmlib.Logs, qemu: Path, folder: Path) -> dict[str, dict[str, Path]]:
    """Make a fresh pair of qcow2 disks (target and extra) for each case."""
    pairs: dict[str, dict[str, Path]] = {}
    for case in CASES:
        pairs[case.name] = {
            "target": make_image(logs, qemu, folder, f"target-{case.name}", 64, False),
            "extra": make_image(logs, qemu, folder, f"extra-{case.name}", case.size_gib, case.used),
        }
    return pairs


def run_case(logs: vmlib.Logs, console: vmlib.Console, case: Case) -> None:
    """Run one case in the booted VM, on the pair of disks that belongs to this case.

    The fixture disko script and the guard read the fixed names `virtio-vd-target` and `virtio-vd-extra`. The case disks
    have their own serials, so the guest has the links `virtio-vd-target-CASE` and `virtio-vd-extra-CASE`. The case
    checks those links first. Then it points the fixed names at the case disks, runs the guard, and removes the fixed
    names and any mount, so the next case starts from the same state.
    """
    name, used, mount = case.name, case.used, case.mount
    own = {role: f"/dev/disk/by-id/virtio-{disk_name(role, name)}" for role in ("target", "extra")}
    rc, _ = console.command(f"test -e {own['target']} && test -e {own['extra']}")
    report(rc == 0, f"{name}: both fixture disks use their by-id names")
    if rc != 0:
        return  # The guard check needs the pair. Skip the rest of this case.
    rc, mounts = console.command("findmnt -rn -o TARGET | grep -E '^/mnt(/|$)' || true")
    report(rc == 0 and mounts == "", f"{name}: nothing is mounted under /mnt before the case (got {mounts!r})")
    rc, _ = console.command(f"ln -sfn $(readlink -f {own['target']}) {TARGET} && ln -sfn $(readlink -f {own['extra']}) {EXTRA}")
    if rc != 0:
        report(False, f"{name}: cannot point the fixed disk names at the case disks")
        return
    try:
        if mount:
            rc, _ = console.command("mkdir -p /mnt/keep && mount -t tmpfs tmpfs /mnt/keep")
            report(rc == 0, f"{name}: mounted a filesystem below /mnt")
        if name == "blank":
            rc, out = console.command(f"{Path('/run/current-system/sw/bin/basix-disk-guard')} all {EXTRA} - {TARGET} -", 120)
            report(rc == 0 and "basix-disk-guard: OK" in out, f"{name}: guard accepts two blank disks (rc {rc}; {out[-500:]})")
        else:
            rc, out = console.command("basix-p4-disko-script 2>&1", 300)
            expected = (
                "has 1 partition(s): not blank" if used else
                "a filesystem is mounted under /mnt" if mount else
                "below the 64 GB minimum"
            )
            report(rc != 0, f"{name}: disko script exits before writing (rc {rc})")
            report(expected in out, f"{name}: guard reports `{expected}`")
            report(STOP_TEXT in out, f"{name}: guard says it stopped disko")
            if expected not in out or STOP_TEXT not in out:
                logs.note(f"{name}: output tail: {out[-1800:]}")
    finally:
        # Leave the guest as it was: no mount under /mnt, no fixed disk name.
        console.command(f"(mountpoint -q /mnt/keep && umount /mnt/keep); rmdir /mnt/keep 2>/dev/null; rm -f {TARGET} {EXTRA}; true")


def run_vm(logs: vmlib.Logs, runner: Path, qemu: Path, env: dict[str, str]) -> None:
    """Boot one VM with four disk pairs, run the cases in turn, and compare the image hashes at the end."""
    folder = vmlib.work_dir("guard")
    pairs = make_pairs(logs, qemu, folder)
    opts: list[str] = []
    for name, pair in pairs.items():
        for role, image in pair.items():
            opts += [
                "-drive", f"file={image},if=none,id=p4{role}{name},format=qcow2",
                "-device", f"virtio-blk-pci,drive=p4{role}{name},serial={disk_name(role, name)}",
            ]
    before = {(name, role): sha256(image) for name, pair in pairs.items() for role, image in pair.items()}
    vm_env = dict(env)
    vm_env["NIX_DISK_IMAGE"] = str(folder / "root.qcow2")
    vm_env["QEMU_OPTS"] = " ".join(opts)
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(runner)], cwd=folder, env=vm_env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        preexec_fn=vmlib.limit_memlock,
    )
    console = None
    try:
        console = vmlib.Console(logs, "guard", proc=proc)
        console.wait_for(r"\[root@guardvm:[^\]]*\]# ", 420)
        logs.note(f"guard VM: prompt reached {time.monotonic() - started:.1f} s after the start")
        for case in CASES:
            case_started = time.monotonic()
            run_case(logs, console, case)
            logs.note(f"{case.name}: elapsed {time.monotonic() - case_started:.1f} s")
        console.write(b"poweroff\r")
        if not console.drain_until_exit(proc, 60):
            raise vmlib.Stop("guard VM did not stop after poweroff")
    finally:
        if console:
            console.close()
        vmlib.stop_process(proc, logs, "guard")
    logs.note(f"guard VM: {time.monotonic() - started:.1f} s from start to stop")
    for (name, role), digest in before.items():
        report(digest == sha256(pairs[name][role]), f"{name}: {role} image is unchanged")


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    os.environ.setdefault("BASIX_WORK", str(vmlib.workdir.default_work("010/P4/p4-vm")))
    os.environ.setdefault("BASIX_LOGS", str(Path.home() / ".local/state/basix/010/P4/p4-vm/logs"))
    logs = vmlib.Logs.create("guard_vm")
    env = vmlib.clean_env()
    work = Path(os.environ["BASIX_WORK"])
    work.mkdir(parents=True, exist_ok=True)
    (work / "tmp").mkdir(exist_ok=True)
    env["TMPDIR"] = str(work / "tmp")
    try:
        logs.note(f"raw logs: {logs.root}; work: {work}")
        vmlib.disk_guard(logs, "guard VM build")
        runner, script = build(logs, root, env)
        qemu, _ = vmlib.qemu_tools(logs, root, env)
        run_vm(logs, runner, qemu, env)
    except vmlib.Stop as error:
        report(False, str(error))
    failed = sum(not ok for ok, _ in RESULTS)
    print(f"{len(RESULTS) - failed} passed, {failed} failed; logs {logs.root}")
    print("PASS" if failed == 0 else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
