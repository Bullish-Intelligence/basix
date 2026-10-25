"""Build the Basix ISO and boot it under OVMF with no disk (P4, BSX-378).

Usage: python3 -I scripts/iso_boot.py [ROOT]      (run by `testee verify opt:basix-iso`)

Steps:
1. Check the root file system (stop when metadata is above 90 % and unallocated space is under 15 GiB).
2. `nix build path:$PWD#packages.x86_64-linux.iso-test`. The test ISO is the shipped ISO with a fast squashfs
   compression (`zstd -Xcompression-level 3`). Set `BASIX_ISO_ATTR=iso` to build and boot the shipped ISO.
3. Boot the ISO under QEMU with KVM and OVMF (UEFI). The VM has no disk at all.
4. Use the GRUB menu over the serial line: press `t` for the text menu, press `e` to edit the first
   entry, append `console=ttyS0,115200n8` to the `linux` line, and boot. The shipped ISO keeps its
   screen console. Only this check adds the serial console, in memory.
5. Log in on the serial line (the live system logs in the `nixos` user by itself) and read the state.

A wait on the serial console stops after `vmlib.STALL_S` (120 s) with no new output. A wait that lasts longer
than that with output still passes.

Expected: the ISO reaches `running` with zero failed units, boots by UEFI, sees no disk, has no Harmonia unit and
no cache-key unit (not running, and no unit file), has Bluetooth on (the service is active, or the unit file and
the adapter tool `bluetoothctl` exist: a VM has no controller, so systemd skips the service), and holds the seed
core and the input sources in its store. The test ISO boots with `basix.serial=ttyS0` (010-D35); the shipped ISO does not.

QEMU and OVMF come from the pinned nixpkgs. The VM files live under `$BASIX_WORK` and the raw
logs under `$BASIX_LOGS/iso_boot/TIME`. Exit 0 when every case passes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vmlib  # noqa: E402

RESULTS: list[tuple[bool, str]] = []
SERIAL_ARG = "console=ttyS0,115200n8"
#: The flake output of the ISO that the checks build and boot. The test ISO has the same content as `packages.iso`.
#: Only its squashfs compression differs: it builds and boots faster. `BASIX_ISO_ATTR=iso` selects the shipped one.
ISO_ATTR = os.environ.get("BASIX_ISO_ATTR", "iso-test")


def report(ok: bool, text: str) -> bool:
    RESULTS.append((ok, text))
    print(("PASS " if ok else "FAIL ") + text, flush=True)
    return ok


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_iso(logs: vmlib.Logs, root: Path, env: dict[str, str], attr: str | None = None) -> Path:
    attr = attr or ISO_ATTR
    vmlib.disk_guard(logs, f"nix build the ISO package {attr}")
    before = vmlib.root_usage(logs)
    started = time.monotonic()
    done = logs.run(
        "build-iso",
        ["nix", "build", "--no-link", "--print-out-paths", f"path:{root}#packages.x86_64-linux.{attr}"],
        cwd=root,
        env=env,
        timeout=7200,
    )
    after = vmlib.root_usage(logs)
    logs.note(
        f"build iso {attr}: rc {done.returncode}, {time.monotonic() - started:.0f} s; "
        f"unallocated {before[0]:.2f} -> {after[0]:.2f} GiB; metadata {before[1]:.1f} -> {after[1]:.1f} %"
    )
    if done.returncode != 0:
        raise vmlib.Stop(f"the ISO build failed:\n{done.stderr[-2500:]}")
    out = done.stdout.strip().splitlines()[-1]
    isos = sorted((Path(out) / "iso").glob("*.iso"))
    if not isos:
        raise vmlib.Stop(f"no .iso file in {out}/iso")
    return isos[0]


def read_raw(console: vmlib.Console, seconds: float) -> bytes:
    """Collect the raw bytes of the console for a short time (cursor codes included)."""
    raw = b""
    end = time.time() + seconds
    while time.time() < end:
        data = console._read(0.5)
        if data:
            raw += data
            console.log.write(data)
            console.buf += data.decode(errors="replace")
    return raw


def boot_with_serial_console(console: vmlib.Console) -> None:
    """Drive GRUB: text menu, edit the first entry, add the serial console, boot."""
    console.wait_for(r"Press 't' to use the text boot menu", 240)
    console.write(b"t")
    console.wait_for(r"GNU GRUB\s+version", 60)
    time.sleep(2)
    console.write(b"e")
    raw = read_raw(console, 4)
    setparams = re.search(rb"\x1b\[0*(\d+);003H\s*setparams ", raw)
    linux = re.search(rb"\x1b\[0*(\d+);003H\s*linux /boot", raw)
    if not (setparams and linux):
        raise vmlib.Stop("the GRUB editor did not show the `setparams` and `linux` lines")
    downs = int(linux.group(1)) - int(setparams.group(1))
    for _ in range(downs):
        console.write(b"\x0e")  # Ctrl-N: next line
        time.sleep(0.2)
    console.write(b"\x05")  # Ctrl-E: end of the line
    time.sleep(0.2)
    console.write(f" {SERIAL_ARG}".encode())
    time.sleep(1)
    console.write(b"\x18")  # Ctrl-X: boot


def run_checks(console: vmlib.Console, root: Path, logs: vmlib.Logs, booted: float) -> None:
    console.wait_for(r"\[nixos@[^\]]*\]\$ ", 420)
    logs.note(f"login prompt reached on the serial line {time.monotonic() - booted:.0f} s after the QEMU start")
    state_rc, state = console.command("systemctl is-system-running --wait", 420)
    report(state == "running", f"ISO reaches system state running (got {state!r})")
    _, failed = console.command("systemctl --failed --no-legend --plain")
    report(failed.strip() == "", f"ISO has zero failed units (got {failed.strip()!r})")
    _, multi = console.command("systemctl is-active multi-user.target")
    report(multi == "active", f"multi-user.target is active (got {multi!r})")
    rc, _ = console.command("test -d /sys/firmware/efi")
    report(rc == 0, "ISO booted by UEFI (/sys/firmware/efi exists)")
    _, cmdline = console.command("cat /proc/cmdline")
    report(SERIAL_ARG in cmdline, "the serial console was added in memory by this check")
    # The test ISO carries the VM-only parameter `basix.serial=ttyS0` (010-D35). The shipped ISO must not.
    has_serial_parameter = "basix.serial=ttyS0" in cmdline
    report(
        has_serial_parameter == (ISO_ATTR == "iso-test"),
        f"basix.serial=ttyS0 is on the command line only for the test ISO (ISO {ISO_ATTR}; present {has_serial_parameter})",
    )

    # A zram swap device can have TYPE=disk, but it has no storage hardware. Read sysfs identity
    # before checking writable disks. Keep every other TYPE=disk device in the assertion.
    rc, raw_devices = console.command("lsblk --json --nodeps --paths --output NAME,TYPE,RO,MAJ:MIN")
    sysfs_rc, raw_sysfs = console.command(
        "for d in /sys/class/block/*; do "
        "[ -e \"$d\" ] || continue; name=${d##*/}; "
        "path=$(readlink -f \"$d\") || exit 1; "
        "type=$(cat \"$d/device/type\" 2>/dev/null || printf '-'); "
        "printf '%s|%s|%s\\n' \"$name\" \"$path\" \"$type\"; "
        "done"
    )
    logs.note(f"ISO lsblk JSON: {raw_devices!r}")
    logs.note(f"ISO block-device sysfs identity (name|path|device type): {raw_sysfs!r}")

    malformed: list[str] = []
    devices: list[dict[str, object]] = []
    try:
        payload = json.loads(raw_devices)
        blockdevices = payload["blockdevices"]
        if not isinstance(blockdevices, list):
            raise TypeError("blockdevices is not a list")
        for item in blockdevices:
            if not isinstance(item, dict):
                malformed.append(repr(item))
                continue
            name, kind, ro, major_minor = (item.get(k) for k in ("name", "type", "ro", "maj:min"))
            valid_ro = isinstance(ro, bool) or (isinstance(ro, int) and ro in (0, 1))
            if (
                not isinstance(name, str) or not name.startswith("/dev/")
                or not isinstance(kind, str) or not kind
                or not valid_ro
                or not isinstance(major_minor, str) or re.fullmatch(r"\d+:\d+", major_minor) is None
            ):
                malformed.append(repr(item))
                continue
            devices.append({"name": name, "type": kind, "ro": bool(ro), "maj:min": major_minor})
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        malformed.append(f"invalid lsblk JSON: {error}")

    sysfs: dict[str, tuple[str, str]] = {}
    for line in raw_sysfs.splitlines():
        fields = line.split("|")
        if len(fields) != 3 or not fields[0] or not fields[1].startswith("/sys/") or not fields[2]:
            malformed.append(f"invalid sysfs record: {line!r}")
            continue
        if fields[0] in sysfs:
            malformed.append(f"duplicate sysfs device: {fields[0]}")
            continue
        sysfs[fields[0]] = (fields[1], fields[2])

    writable: list[str] = []
    identity_errors: list[str] = []
    identities: list[str] = []
    seen_devices: set[str] = set()
    for device in devices:
        name = Path(str(device["name"])).name
        if name in seen_devices:
            malformed.append(f"duplicate lsblk device: {name}")
            continue
        seen_devices.add(name)
        identity = sysfs.get(name)
        if identity is None:
            identity_errors.append(f"{device['name']}: no sysfs identity")
            continue
        path, device_type = identity
        if Path(path).name != name:
            identity_errors.append(f"{device['name']}: sysfs path ends in {Path(path).name!r}")
            continue
        kind = str(device["type"])
        ro = bool(device["ro"])
        zram = kind == "disk" and re.search(r"/devices/virtual/block/zram\d+$", path) is not None
        identities.append(
            f"{device['name']} type={kind} ro={int(ro)} maj:min={device['maj:min']} "
            f"sysfs={path} device-type={device_type}" + (" [zram]" if zram else "")
        )
        if kind == "disk" and not zram and not ro:
            writable.append(identities[-1])
        if kind == "rom" and not ro:
            writable.append(identities[-1])
    logs.note(f"ISO block-device classifications: {identities!r}")
    report(
        rc == 0 and sysfs_rc == 0 and not malformed and not identity_errors and not writable,
        "no writable install disk "
        f"(lsblk rc {rc}; sysfs rc {sysfs_rc}; malformed {malformed!r}; "
        f"identity errors {identity_errors!r}; writable devices {writable!r})",
    )

    # The deny list (BSX-097, R39): no Harmonia and no cache-key unit on the live ISO. Neither a unit file nor a
    # loaded unit may exist, and nothing may listen on the Harmonia port 5000.
    _, files = console.command("systemctl list-unit-files --no-legend --plain 'harmonia*' 'vendomat-cache-key*'")
    report(files.strip() == "", f"the ISO has no Harmonia and no cache-key unit file (got {files.strip()!r})")
    _, loaded = console.command("systemctl list-units --all --no-legend --plain 'harmonia*' 'vendomat-cache-key*'")
    report(loaded.strip() == "", f"the ISO has no Harmonia and no cache-key unit loaded (got {loaded.strip()!r})")
    _, port = console.command("ss -ltn | grep -c ':5000 ' || true")
    report(port.strip() in ("", "0"), f"nothing listens on TCP 5000 (Harmonia) on the ISO (count {port.strip()!r})")

    # Bluetooth is on (BSX-325). A VM has no controller, so systemd skips the service through its condition on
    # /sys/class/bluetooth. Then the unit file and the adapter tool prove that the ISO has Bluetooth on.
    _, bluetooth = console.command("systemctl is-active bluetooth.service")
    unit_rc, _ = console.command("systemctl cat --no-pager bluetooth.service > /dev/null")
    tool_rc, tool = console.command("bluetoothctl --version")
    report(
        bluetooth == "active" or (unit_rc == 0 and tool_rc == 0),
        f"Bluetooth is on in the ISO (service {bluetooth!r}; unit file rc {unit_rc}; bluetoothctl rc {tool_rc} {tool!r})",
    )

    # The live system runs no SSH server.
    _, ssh_state = console.command("systemctl is-active sshd.service")
    report(ssh_state in ("inactive", "unknown"), f"sshd.service is not active on the ISO (got {ssh_state!r})")
    _, ssh = console.command("ss -ltn | grep -c ':22 ' || true")
    report(ssh.strip() in ("", "0"), f"nothing listens on TCP 22 on the ISO (count {ssh.strip()!r})")

    # The compiler, command, lock, runtime source, and input sources must be on the ISO.
    for tool in ("basix", "basix-compile", "git", "jq", "nixos-facter", "nmtui", "sgdisk"):
        rc, _ = console.command(f"command -v {tool}")
        report(rc == 0, f"{tool} is on the PATH of the ISO")
    rc, _ = console.command("test -s /etc/basix/flake.lock && test -d /etc/basix/source")
    report(rc == 0, "the ISO holds the Basix lock and source tree")
    _, etc = console.command("find /etc/basix /etc/static/basix -maxdepth 4 -print 2>/dev/null | sort")
    logs.note(f"ISO Basix etc paths: {etc!r}")
    rc, _ = console.command("test -s /etc/basix/drop.jq")
    report(rc == 0, "the ISO holds the hardware report filter")
    lock_text = (root / "flake.lock").read_text()
    lock = json.loads(lock_text)
    names = sorted(set(lock["nodes"]) - {"root"})
    missing = []
    for name in names:
        rc, _ = console.command(f"test -d /etc/basix/inputs/{name}")
        if rc:
            missing.append(name)
    report(not missing, f"the ISO holds every locked input source ({len(names)} inputs; missing: {missing})")


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    os.environ.setdefault("BASIX_WORK", str(vmlib.workdir.default_work("010/P4/p4-vm")))
    os.environ.setdefault("BASIX_LOGS", str(Path.home() / ".local/state/basix/010/P4/p4-vm/logs"))
    logs = vmlib.Logs.create("iso_boot")
    env = vmlib.clean_env()
    work = vmlib.work_dir("iso")
    env["TMPDIR"] = str(vmlib.work_dir("vmtmp"))
    proc: subprocess.Popen[bytes] | None = None
    console: vmlib.Console | None = None
    try:
        logs.note(f"raw logs: {logs.root}")
        iso = build_iso(logs, root, env)
        size = iso.stat().st_size
        digest = sha256(iso)
        logs.note(f"iso {iso} size {size} sha256 {digest}")
        report(size > 0, f"ISO built: {iso.name}, {size} bytes, sha256 {digest}")
        qemu, ovmf = vmlib.qemu_tools(logs, root, env)

        variables = work / "vars.fd"
        shutil.copyfile(ovmf / "FV/OVMF_VARS.fd", variables)
        variables.chmod(0o600)
        sock = work / "serial.sock"
        argv_qemu = [
            str(qemu / "bin/qemu-system-x86_64"),
            *["-enable-kvm", "-cpu", "host", "-machine", "q35", "-m", "4096", "-smp", str(vmlib.VM_CPUS)],
            *["-drive", f"if=pflash,format=raw,readonly=on,file={ovmf}/FV/OVMF_CODE.fd"],
            *["-drive", f"if=pflash,format=raw,file={variables}"],
            *["-drive", f"file={iso},media=cdrom,readonly=on,if=ide"],
            *["-nic", "user,model=virtio-net-pci"],
            *["-display", "none", "-monitor", "none"],
            *["-chardev", f"socket,id=ser0,path={sock},server=on,wait=off", "-serial", "chardev:ser0"],
        ]
        (logs.root / "qemu-argv.json").write_text(json.dumps(argv_qemu, indent=1) + "\n")
        proc = subprocess.Popen(
            argv_qemu,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=(logs.root / "qemu.out").open("wb"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=vmlib.limit_memlock,
        )
        booted = time.monotonic()
        console = vmlib.Console(logs, "iso", sock=sock)
        boot_with_serial_console(console)
        run_checks(console, root, logs, booted)
        logs.note(f"VM checks done {time.monotonic() - booted:.0f} s after the QEMU start")
    except vmlib.Stop as error:
        report(False, str(error))
    finally:
        if console is not None:
            console.close()
        vmlib.stop_process(proc, logs, "iso")
        shutil.rmtree(work, ignore_errors=True)
    failed = [text for ok, text in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed; logs {logs.root}")
    print("GREEN" if not failed else "RED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
