"""Boot the desktop machine with a screen, show the login screen, and log in (`opt:basix-desktop`, L66, L67, P6).

Usage: python3 -I scripts/desktop_vm.py [ROOT]
       (run by `testee verify opt:basix-desktop`; the name of the check is proposed)

The check builds the machine `Desktop(greeting="Welcome to Basix")` with the test VM module `fixtures/vmdesk.nix` (a
virtio GPU, a serial line with a root shell, and a known password for the account `learner`). It compiles `system.py`
with the compiler of the flake (`BASIX_COMPILER=PATH` names another one), builds
`nixosConfigurations.vmcheck.config.system.build.vm` with the input `basix` replaced by a clean copy of this repository,
and boots it. No block device of the host reaches QEMU.

The check uses three channels:
- the serial line: the root shell, for the commands and the answers;
- the QMP socket of QEMU: `screendump` for the screen, and `send-key` to type on the login screen;
- the files of the check in `$BASIX_LOGS`: the serial log and one PNG per screen.

What it proves, in order:
1. the cases of `profiles/desktop/check.nix` hold in the booted machine;
2. greetd runs tuigreet on the screen. The screen is not blank, and the command line of tuigreet holds the greeting and
   the session `basix-session`, with no store path after `--cmd` (L67);
3. the learner logs in on the login screen. Hyprland runs as the learner under UWSM, `noctalia.service` of the user is
   active, and Hyprland reads the Basix default look from `/etc/xdg/hypr/hyprland.lua` (the option `general:gaps_out` is
   "20 20 20 20", the value of that file);
4. the screen after the login is not blank and differs from the login screen.

A PNG is the proof for a person. The check also reads the same screen as PPM and counts the colours, so a black screen
fails. The screens are in `$BASIX_LOGS/desktop_vm/TIME/`. QEMU shows no real GPU (llvmpipe), so this is not a proof for
real hardware.

The check stops before a build when metadata is above 90 % and unallocated space is under 15 GiB (`vmlib.disk_guard`).
Run `btrfs filesystem usage /` first. Do not run it beside another VM check.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vmlib  # noqa: E402

RESULTS: list[tuple[bool, str]] = []

VM_MACHINE = "vmcheck"
VM_USER = "learner"
VM_PASSWORD = "basix-try"  # TEST ONLY: `fixtures/vmdesk.nix` sets it
VM_DISK = "/dev/disk/by-id/virtio-vd-target"
GREETING = "Welcome to Basix"
#: The files of the flake. The VM build and the compiler build read them.
FLAKE_FILES = ["flake.nix", "flake.lock", "pyproject.toml", "uv.lock", "README.md", "lib", "modules", "profiles", "capabilities", "nix", "src"]
#: The screen of the VM (QEMU virtio GPU default), for the pointer click on the first-run window of Noctalia.
SCREEN = (1280, 800)

SYSTEM_PY = f"""\
from basix_config import *

machine = Machine(
    name="{VM_MACHINE}",
    user="{VM_USER}",
    disk="{VM_DISK}",
    profile=Desktop(greeting="{GREETING}"),
)
"""
STUB = """\
# A fixture stub in the form of CONCEPT Appendix C, with the test VM module of the desktop check.
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }: {
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
      extraModules = [ ./vmdesk.nix ];
    };
  };
}
"""

# The US keyboard, as QMP `send-key` names. A letter is itself; a capital letter is shift plus the letter.
PLAIN = {" ": "spc", "\n": "ret", "-": "minus", ".": "dot", ",": "comma", "'": "apostrophe"}


def report(ok: bool, text: str) -> bool:
    RESULTS.append((ok, text))
    print(("PASS " if ok else "FAIL ") + text, flush=True)
    return ok


class Qmp:
    """The control socket of QEMU: the screen and the keyboard."""

    def __init__(self, path: Path, timeout: float = 120) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        end = time.time() + timeout
        while True:
            try:
                self.sock.connect(str(path))
                break
            except OSError:
                if time.time() > end:
                    raise vmlib.Stop(f"cannot connect to the QMP socket {path}") from None
                time.sleep(0.5)
        self.file = self.sock.makefile("rw")
        self.file.readline()  # the greeting of QEMU
        self.command("qmp_capabilities")

    def command(self, name: str, **arguments: object) -> dict:
        self.file.write(json.dumps({"execute": name, "arguments": arguments}) + "\n")
        self.file.flush()
        while True:
            answer = json.loads(self.file.readline())
            if "return" in answer or "error" in answer:
                return answer

    def key(self, combination: str) -> None:
        keys = [{"type": "qcode", "data": part} for part in combination.split("-")]
        answer = self.command("send-key", keys=keys, **{"hold-time": 60})
        if "error" in answer:
            raise vmlib.Stop(f"send-key {combination} failed: {answer}")
        time.sleep(0.05)

    def type(self, text: str) -> None:
        for char in text:
            if char in PLAIN:
                self.key(PLAIN[char])
            elif char.isalnum() and char.isascii():
                self.key(("shift-" + char.lower()) if char.isupper() else char)
            else:
                raise vmlib.Stop(f"no key for the character {char!r}")

    def screendump(self, path: Path, format: str) -> None:
        answer = self.command("screendump", filename=str(path), format=format)
        if "error" in answer:
            raise vmlib.Stop(f"screendump failed: {answer}")

    def click(self, x: int, y: int) -> None:
        """Move the pointer to (x, y) and press the left button (needs `-device virtio-tablet-pci`)."""
        width, height = SCREEN

        def event(kind: str, data: dict) -> dict:
            return {"type": kind, "data": data}

        point = [
            event("abs", {"axis": "x", "value": int(x * 32767 / (width - 1))}),
            event("abs", {"axis": "y", "value": int(y * 32767 / (height - 1))}),
        ]
        self.command("input-send-event", events=point)
        time.sleep(0.3)
        self.command("input-send-event", events=[event("btn", {"button": "left", "down": True})])
        time.sleep(0.1)
        self.command("input-send-event", events=[event("btn", {"button": "left", "down": False})])

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def read_ppm(path: Path) -> tuple[int, int, bytes]:
    """The width, the height, and the RGB bytes of a binary PPM file."""
    data = path.read_bytes()
    fields: list[bytes] = []
    at = 0
    while len(fields) < 4:  # P6, width, height, maximum value
        while data[at : at + 1].isspace():
            at += 1
        end = at
        while not data[end : end + 1].isspace():
            end += 1
        fields.append(data[at:end])
        at = end
    return int(fields[1]), int(fields[2]), data[at + 1 :]


def screen_facts(path: Path) -> dict:
    """Facts about a screen that tell it from a blank one: the number of colours and of bright pixels."""
    width, height, pixels = read_ppm(path)
    colours = {pixels[i : i + 3] for i in range(0, len(pixels), 3)}
    bright = sum(1 for i in range(0, len(pixels), 3) if pixels[i] + pixels[i + 1] + pixels[i + 2] > 384)
    sha = hashlib.sha256(pixels).hexdigest()[:12]
    return {"width": width, "height": height, "colours": len(colours), "bright": bright, "sha": sha}


class Machine:
    """The booted VM: a root shell on the serial line and the control socket."""

    def __init__(self, logs: vmlib.Logs, console: vmlib.Console, qmp: Qmp) -> None:
        self.logs, self.console, self.qmp = logs, console, qmp

    def root(self, line: str, timeout: float = 120) -> tuple[int, str]:
        return self.console.command(line, timeout)

    def learner(self, line: str, timeout: float = 120) -> tuple[int, str]:
        """A command as the learner, with the session variables of the user manager."""
        env = f"XDG_RUNTIME_DIR=/run/user/$(id -u {VM_USER}) DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u {VM_USER})/bus"
        signature = f"HYPRLAND_INSTANCE_SIGNATURE=$(ls -t /run/user/$(id -u {VM_USER})/hypr 2>/dev/null | head -1)"
        return self.root(f"runuser -u {VM_USER} -- env {env} {signature} bash -c {shlex.quote(line)}", timeout)

    def wait(self, what: str, run: str, seconds: float, user: bool = False) -> bool:
        """Run a command each 2 s until it exits 0."""
        end = time.time() + seconds
        while time.time() < end:
            code, _ = (self.learner if user else self.root)(run, 60)
            if code == 0:
                return True
            time.sleep(2)
        self.logs.note(f"timeout ({seconds:.0f} s) while waiting for {what}")
        return False

    def screen(self, name: str, folder: Path) -> dict:
        """Take the screen as a PNG for a person, and as a PPM for the check. Return the facts of the screen."""
        time.sleep(2)  # let the screen settle
        self.qmp.screendump(folder / f"{name}.png", "png")
        self.qmp.screendump(folder / f"{name}.ppm", "ppm")
        facts = screen_facts(folder / f"{name}.ppm")
        (folder / f"{name}.ppm").unlink()  # the PNG stays; the PPM is big
        self.logs.note(f"screen {name}: {facts}")
        return facts


def stage_flake(repo: Path) -> Path:
    target = vmlib.work_dir("desktop/basix")
    for name in FLAKE_FILES:
        source = repo / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__", ".devenv", ".direnv"))
        elif source.is_file():
            shutil.copy2(source, target / name)
    return target


def compiler_command(logs: vmlib.Logs, flake: Path, env: dict[str, str]) -> list[str]:
    named = os.environ.get("BASIX_COMPILER")
    if named:
        return [named]
    done = logs.run("build-compiler", ["nix", "build", f"path:{flake}#compiler", "--no-link", "--print-out-paths", "--no-write-lock-file"], env=env, timeout=3600)
    if done.returncode != 0 or not done.stdout.strip():
        raise vmlib.Stop(f"the compiler did not build:\n{done.stderr[-2500:]}")
    return [f"{done.stdout.strip().splitlines()[-1]}/bin/basix-compile"]


def build_vm(logs: vmlib.Logs, repo: Path, env: dict[str, str]) -> tuple[Path, Path, Path]:
    """Make the fixture workspace and build the VM. Return (runner, folder for the VM files, flake copy)."""
    flake = stage_flake(repo)
    compiler = compiler_command(logs, flake, env)
    folder = vmlib.work_dir("desktop/vm")
    ws = folder / "ws"
    ws.mkdir()
    (ws / "system.py").write_text(SYSTEM_PY)
    (ws / "flake.nix").write_text(STUB)
    shutil.copy2(repo / "fixtures" / "vmdesk.nix", ws / "vmdesk.nix")
    done = logs.run("compile", [*compiler, "system.py", "system.json"], cwd=ws, env=env, timeout=900)
    if done.returncode != 0:
        raise vmlib.Stop(f"the compile failed:\n{done.stderr[-2500:]}")
    attr = f"nixosConfigurations.{VM_MACHINE}.config.system.build.vm"
    vmlib.disk_guard(logs, f"nix build {attr} (desktop)")
    argv = [
        "nix", "build", "--no-link", "--print-out-paths", "--no-write-lock-file", "--no-warn-dirty",
        "--override-input", "basix", f"path:{flake}", f"path:{ws}#{attr}",
    ]
    done = logs.run("build-vm", argv, env=env, timeout=7200)
    if done.returncode != 0:
        raise vmlib.Stop(f"the VM build failed:\n{done.stderr[-2500:]}")
    out = Path(done.stdout.strip().splitlines()[-1])
    vmlib.refuse_cycles(out / "system", "the VM build (desktop)")
    return next((out / "bin").glob("run-*-vm")), folder, flake


def case_data(logs: vmlib.Logs, flake: Path, env: dict[str, str]) -> list[dict]:
    """The `cases` of `profiles/desktop/check.nix`."""
    expression = (
        f'let f = builtins.getFlake "path:{flake}"; pkgs = f.inputs.nixpkgs.legacyPackages.x86_64-linux; '
        f"c = import {flake}/profiles/desktop/check.nix {{ inherit pkgs; }}; in c.cases or [ ]"
    )
    done = logs.run("check-data", ["nix", "eval", "--impure", "--json", "--expr", expression], env=env, timeout=900)
    if done.returncode != 0:
        raise vmlib.Stop(f"cannot read the check of the profile desktop:\n{done.stderr[-1500:]}")
    return json.loads(done.stdout)


def log_in(vm: Machine) -> bool:
    """Type the account name and the password at tuigreet. Try twice: the first keys may come before the prompt."""
    for attempt in (1, 2):
        time.sleep(2.5)
        vm.qmp.type(f"{VM_USER}\n")
        time.sleep(1.5)
        vm.qmp.type(f"{VM_PASSWORD}\n")
        if vm.wait("Hyprland of the learner", f"pgrep -u {VM_USER} -f Hyprland >/dev/null", 60):
            return True
        vm.logs.note(f"login attempt {attempt}: no session after 60 s")
        vm.qmp.key("esc")
        time.sleep(2)
    return False


def check_desktop(vm: Machine, cases: list[dict], shots: Path) -> None:
    # 1. The cases of the profile.
    for case in cases:
        code, output = vm.root(case["run"] + " 2>&1", 120)
        if not report(code == 0, f"desktop: {case['name']}"):
            vm.logs.note(f"case `{case['run']}` exited {code}; output:\n{output[-1500:]}")

    # 2. The login screen.
    up = vm.wait("greetd", "systemctl is-active greetd.service && pgrep -x tuigreet >/dev/null", 180)
    report(up, "greeter: greetd runs and tuigreet is its program")
    if not up:
        return
    code, command = vm.root("pgrep -a -x tuigreet | head -1")
    session = command.partition(" --cmd ")[2].strip()
    # `pgrep -a` prints the arguments of the process with no shell quotes: the greeting is three words.
    report(code == 0 and f"--greeting {GREETING} --cmd" in command, f"greeter: the command line of tuigreet holds the greeting (got {command!r})")
    report(session == "basix-session", f"greeter: the session is the wrapper `basix-session`, with no store path after --cmd (got {session!r})")
    _, active = vm.root("cat /sys/class/tty/tty0/active")
    report(active.strip() == "tty1", f"greeter: the screen shows tty1 (got {active.strip()!r})")
    login = vm.screen("01-greeter", shots)
    report(login["bright"] > 200 and login["colours"] >= 2, f"greeter: the screen is not blank (bright pixels {login['bright']}, colours {login['colours']})")

    # 3. The login.
    report(log_in(vm), "login: Hyprland runs as the learner after the password on the login screen")
    started = vm.wait("noctalia.service", "systemctl --user is-active noctalia.service", 120, user=True)
    report(started, "login: noctalia.service of the learner is active")
    _, process = vm.root(f"ps -u {VM_USER} -o args= | grep -E 'uwsm|Hyprland' | head -3")
    report("uwsm" in process.lower(), f"login: the session runs under UWSM (got {process.splitlines()[:2]!r})")
    # Hyprland has no config in the home of the learner, so it reads the Basix default look (009-D30).
    _, gaps = vm.learner("hyprctl -j getoption general:gaps_out")
    try:
        value = json.loads(gaps).get("css")
    except ValueError:
        value = None
    report(value == "20 20 20 20", f"login: Hyprland reads /etc/xdg/hypr/hyprland.lua, so general:gaps_out is 20 (got {value!r})")
    _, failed = vm.root("systemctl --failed --no-legend | wc -l")
    report(failed.strip() == "0", f"login: the machine has no failed system unit (got {failed.strip()!r})")
    _, failed = vm.learner("systemctl --user --failed --no-legend | wc -l")
    report(failed.strip() == "0", f"login: the session has no failed user unit (got {failed.strip()!r})")

    # 4. The screen after the login. The first start of Noctalia shows a welcome window that takes the keyboard.
    welcome = vm.screen("02-session", shots)
    vm.qmp.click(842, 558)  # the button "Get Started" (S19)
    after = vm.screen("03-session-after-welcome", shots)
    report(
        welcome["colours"] >= 8 and welcome["sha"] != login["sha"],
        f"session: the screen after the login is not blank and not the login screen (colours {welcome['colours']})",
    )
    vm.logs.note(f"screen after the welcome window: {after}")


def main(argv: list[str]) -> int:
    repo = Path(argv[1] if len(argv) > 1 else ".").resolve()
    logs = vmlib.Logs.create("desktop_vm")
    env = vmlib.clean_env()
    env["TMPDIR"] = str(vmlib.work_dir("vmtmp"))
    shots = logs.root
    sockets = Path(f"/dev/shm/basix-desktop-{os.getpid()}")  # a short path: a unix socket path has at most 107 bytes
    proc = console = qmp = None
    try:
        logs.note(f"raw logs and screens: {logs.root}")
        started = time.monotonic()
        runner, folder, flake = build_vm(logs, repo, env)
        cases = case_data(logs, flake, env)
        if not cases:
            raise vmlib.Stop("profiles/desktop/check.nix lists no case")
        (folder / "tmp").mkdir(parents=True, exist_ok=True)
        sockets.mkdir(mode=0o700)
        run_env = dict(env)
        run_env["NIX_DISK_IMAGE"] = str(folder / "root.qcow2")
        run_env["TMPDIR"] = str(folder / "tmp")
        run_env["USE_TMPDIR"] = "1"
        # A screen of its own for QMP (no window), a pointer for the welcome window, the serial line on a socket.
        run_env["QEMU_OPTS"] = (
            "-display none -vga none -device virtio-gpu-pci -device virtio-tablet-pci "
            f"-serial unix:{sockets}/serial.sock,server=on,wait=off -qmp unix:{sockets}/qmp.sock,server=on,wait=off"
        )
        log = (logs.root / "qemu.log").open("ab")
        proc = subprocess.Popen(
            [str(runner)], env=run_env, cwd=folder, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True, preexec_fn=vmlib.limit_memlock,
        )
        time.sleep(3)
        if proc.poll() is not None:
            raise vmlib.Stop(f"the VM runner exited with {proc.returncode}; see {logs.root}/qemu.log")
        console = vmlib.Console(logs, "desktop", sock=sockets / "serial.sock")
        qmp = Qmp(sockets / "qmp.sock")
        console.wait_for(rf"\[root@{VM_MACHINE}:[^\]]*\]# ", 900)
        console.command("set +H")
        check_desktop(Machine(logs, console, qmp), cases, shots)
        console.write(b"poweroff\r")
        console.drain_until_exit(proc, 90)
        logs.note(f"desktop: {time.monotonic() - started:.0f} s")
    except vmlib.Stop as error:
        report(False, str(error))
    finally:
        if qmp is not None:
            qmp.close()
        if console is not None:
            console.close()
        vmlib.stop_process(proc, logs, "desktop")
        shutil.rmtree(sockets, ignore_errors=True)
    failed = [text for ok, text in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed; logs {logs.root}")
    print("GREEN" if not failed else "RED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
