"""Evaluation checks of `lib.machine`, the core, the disk layout, and the guard (P2, 010 PLAN).

Usage: python3 -I scripts/check_eval.py [ROOT]

Run it from the repository root, with `nix` on PATH. A fixture is a workspace under `fixtures/`: it holds
`system.py`, a stub `flake.nix` (the form of CONCEPT Appendix C), and the compiled `system.json`. The check copies
the files of the flake to a clean folder, copies each fixture next to it, and evaluates the stub with the input
`basix` replaced by that folder. It builds no system and no image. It builds the disk script of the two-disk case (the
script is small).

The compile (`COMPILE`):
- By default the check compiles each `system.py` with the compiler of the flake: it builds `.#compiler` and runs
  `bin/basix-compile system.py system.json` in a copy of the fixture.
- `BASIX_COMPILER=PATH` names another compiler. The check runs `PATH system.py system.json` in the copy.
- `BASIX_COMPILE=0` does not compile. The check uses the `system.json` stored beside `system.py`.
Two fixtures never compile: `input` (its input is a stand-in) uses its stored `system.json` always.

What it proves, with the row IDs:
- the core has no SSH key and no password option, a locked root, and the cuts of Basix R7 (BSX-131, BSX-134, BSX-148);
- a value that a machine may change is `mkDefault`: a machine value wins with no conflict (009-D39);
- a fixed value refuses with a Basix line (`users.mutableUsers`, the locked root);
- `disk = None` writes no disk layout and no guard (009-D39);
- evaluation fails and names the disk when a disk has no guard, sets `destroy = true`, or lost its hook
  (BSX-096, BSX-114, BSX-140); the guard file is unchanged; in a two-disk script one all-disk guard call stands
  before the first write (BSX-115, BSX-141);
- `Force` from `nixdantic` wins over a normal value and the core, and `Before` and `After` order a list (L128, BSX-396);
- a trusted link maps to `networking.firewall.trustedInterfaces`, and `RemoteLogin()` opens port 22 on it only (L125);
- a machine with no trusted link and `Port(22, interface=...)` gets Avahi and UDP 5353 on those links only (010-D45);
- two items of `options=[...]` that set one option to two values: the NixOS error names the `system.py` line of each
  item (BSX-385);
- the guards of `lib.machine`: a stale `system.py`, a stale or missing read file, a `pynix` library from another
  nixpkgs (009-D28), an input the stub lacks, and an input package from another version (009-D48);
- the drvPath of the anchor machine T3, with the module `basix-command.nix` disabled, is the recorded one (BSX-363),
  and with `linger` unset and the module of the script `minix` disabled too it is the one of the spike ACC1
  (010-D16, P3);
- the stub writer: a workspace with the stub of `basix_config.stub` (no `Input`) and one with a local `Input` evaluate
  through `lib.machine` (L129);
- one typed `Input(flake=True)` end to end (BSX-397): the stub with `lib.inputPackages`, the lock, the input package
  `pynix_demo`, the compile of a typed value, and the toplevel with the file that the module of the input writes;
- one `Input(python="src")` end to end, with and without `flake=False`: the stub with `pythonDirs`, the lock, the input
  package with the linked code, a compile that imports the code (no file in `reads`), and the toplevel with the value
  from that code. An error in the code names its file, and a moved input fails the evaluation;
- a second, named nixpkgs end to end (PF-2): `NixpkgsPin("old", COMMIT)` and `Program(old.pkgs.hello)` give the input
  `nixpkgs-old` in the stub, the package list `pynix_nixpkgs_old`, a compile, and a machine that holds the `hello` of
  that commit. The case skips when the pynix pin of the flake predates `NixpkgsPin`; `BASIX_PYNIX_PATH=FOLDER`
  replaces the pin with a pynix source folder, for the compiler and for the other builds;
- the script `minix` of the core holds the one public URL (L113, 010-D15);
- the full T3 machine holds the `basix` command and the compiler of its own Basix flake (010-D37);
- the Basix host of the minix VM check (`fixtures/vmhost`, `disk = None`, a root file system of its own) evaluates;
- the profile `Desktop()` (L66, P6): the machine evaluates; Hyprland, Noctalia, greetd, foot, and Firefox are in the
  system; `/etc/xdg/hypr/hyprland.lua` and the Noctalia default exist; the login screen runs the wrapper `basix-session`
  and shows no store path (L67); a native value at normal priority wins over each `Default(...)` value (L42); the machine
  equals the same desktop written by hand in Nix; a console machine has none of it;
- the source trees of the outputs (`nix/source.nix`): a checkout with `__pycache__` folders, `*.pyc` files, tool
  caches, editor files, and a `result` link gives the same three store paths as a clean checkout, and the trees hold none of
  these files. So a stray file cannot change the ISO;
- `basix explain` on the evaluated machine of a fixture: a field, a capability, and an option path give the value and
  the line of `system.py` that sets it, and a wrong name gives "did you mean" (009-D14).

Jobs (`BASIX_EVAL_JOBS`):
- The steps run side by side. A job is one child process (`nix`, the compiler, or a stub writer). At most `BASIX_EVAL_JOBS`
  jobs run at a time. The default is the CPU count; `BASIX_EVAL_JOBS=1` runs one job at a time.
- The check compiles all fixtures once at the start and reuses the result.
- Each step collects its lines. The check prints them in the order of the steps, so the output does not depend on the
  job count.

It prints PASS or FAIL for each case and exits 1 when any case fails. Raw output goes to
`$BASIX_CHECK_LOGS` (default: `~/.local/state/basix/p2/logs/check_eval/TIME`), never into the repository. The
copies go to `$BASIX_CHECK_WORK` (default: `~/.cache/basix/p2/check_eval`).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workdir  # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;]*m")
RESULTS: list[tuple[bool, str]] = []
LOGS = Path()
WORK = Path()
#: `RESULTS` and the case counter are shared by all threads. `SLOTS` limits the child processes (`main` sets its size).
LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(1)
#: The lines of the step that runs in this thread (see `emit`). A thread with no step prints at once.
STEP = threading.local()
#: The threads of the sub-jobs (see `go`). A sub-job waits for a slot, not for another sub-job, so the size is a bound only.
JOBS_POOL = ThreadPoolExecutor(max_workers=64, thread_name_prefix="job")

#: The drvPath hash of the T3 anchor machine (`RemoteLogin()`, the test VM module). It changed three times. The value of ACC1
#: was `p9rl5qxp29h4pk22jhm26vdgxz4rbjk8`. First the core set `users.users.<user>.linger = mkDefault true` (010-D16,
#: L79): `b0rsqy8ild5k11hnh8rjlvbhc8aqa42d`. Then the core added the script `minix` to the system packages (L113, P3):
#: `z4x1vr1c3gj63g4666xwg5nvssvpdmv4`. Then the launcher URL changed to `Bullish-Intelligence` (012-D6, S4): the value
#: below. The anchor evaluates T3 with `modules/basix-command.nix` disabled (010-D37): that module adds the
#: packages `basix` and the compiler, which are built from the Basix source, so their paths move with every change of
#: `src/`. `check_command` proves the module on the full T3 machine.
T3_HASH = "x0fjwm2z5bzxl73hlppg5y0mmmgphwxf"
#: The T3 hash with `linger` unset (`mkForce null`) and the script `minix` left out (`disabledModules`): the value of
#: ACC1 returns, so linger and the launcher are the only changes (010-D16, P3).
T3_NO_LINGER_HASH = "p9rl5qxp29h4pk22jhm26vdgxz4rbjk8"
#: SHA-256 of `modules/disk-guard.sh`, a byte copy of the B3 spike file. A change needs a new fixture run.
GUARD_SHA256 = "04945472dc3f09d8f039c198cec65e3262d9c3c76142fb42765ed5426b32635c"
#: The files of the flake that the evaluation and the compiler build read.
FLAKE_FILES = [
    "flake.nix",
    "flake.lock",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "lib",
    "modules",
    "profiles",
    "capabilities",
    "nix",
    "src",
]

#: The line of the script `minix` of the core (L113, 010-D15). The URL is the public one.
#: The fixtures that the steps compile. The check compiles them all at the start, the slow steps first.
FIXTURES = (
    "desktop",
    "desktop-full",
    "core",
    "t3",
    "trusted",
    "ownlink",
    "priority",
    "disk-none",
    "typed-core",
    "vmhost",
    "conflict",
    "net",
    "reads",
    "input",
)
#: The folders and files that the three source trees (`lib.sourceTrees`) read, beyond `FLAKE_FILES`.
SOURCE_TREE_FILES = [*FLAKE_FILES, "CHANGELOG.md", "install", "iso", "provenance", "template"]
#: Stray files that a developer tool can leave in a checkout, as paths below the checkout. A `None` text makes a link.
STRAY_FILES = {
    "src/basix/__pycache__/cli.cpython-313.pyc": "x",
    "src/basix_config/capabilities/__pycache__/remote_login.cpython-313.pyc": "x",
    "src/basix/stray.pyc": "x",
    "src/.pytest_cache/CACHEDIR.TAG": "x",
    "src/.ruff_cache/CACHEDIR.TAG": "x",
    "src/basix/cli.py.swp": "x",
    "capabilities/remote-login/__pycache__/component.cpython-313.pyc": "x",
    "capabilities/remote-login/component.py~": "x",
    "iso/__pycache__/x.cpython-313.pyc": "x",
    "lib/.DS_Store": "x",
    "modules/__pycache__/x.cpython-313.pyc": "x",
    "profiles/desktop/.venv/pyvenv.cfg": "x",
    "nix/result": None,
}
#: The steps that take longest. They start first.
SLOW_FIRST = (
    "desktop",
    "variants",
    "typed-input",
    "python-input",
    "named-nixpkgs",
    "stub",
    "anchor",
    "guards",
    "refusals",
    "explain",
    "core",
)

MINIX_COMMAND = (
    'exec nix --extra-experimental-features "nix-command flakes" run '
    'git+https://github.com/Bullish-Intelligence/minix?ref=release -- "$@"'
)

STUB = """\
# A fixture stub in the form of CONCEPT Appendix C.
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }: {
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
%(extra)s    };
  };
}
"""
EXTRA_MODULE = "      extraModules = [ ./extra.nix ];\n"
LIVE_STUB = """\
# A fixture stub: the machine, and the same machine with `liveOff` applied (the ISO policy, BSX-097).
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }:
    let
      machines = basix.lib.machine {
        compiled = builtins.fromJSON (builtins.readFile ./system.json);
        source = ./system.py;
        root = ./.;
        extraModules = [ ./extra.nix ];
      };
    in {
      nixosConfigurations = machines // {
        trybox-off = machines.trybox.extendModules {
          modules = [ (import "${basix}/modules/live-off.nix" [ "basix-live-test" ]) ];
        };
      };
    };
}
"""

# The modules of the variants: the v1 fixture machines of `tests/machines.nix`, as extra modules of the stub.
EXTRA_DISK = """\
{
  disko.devices.disk.extra = {
    device = "/dev/disk/by-id/virtio-vd-extra";
    type = "disk";
    content = {
      type = "gpt";
      partitions.data = { size = "100%"; content = { type = "filesystem"; format = "ext4"; mountpoint = "/data"; }; };
    };
  };
}
"""
TWO_DISKS = '{ lib, ... }: { basix.disk.guarded = [ "extra" ]; imports = [ ./disk-extra.nix ]; }\n'
LIVE_UNITS = """\
{ pkgs, ... }: {
  systemd.services.basix-live-test = {
    wantedBy = [ "multi-user.target" ];
    serviceConfig.ExecStart = "${pkgs.coreutils}/bin/true";
  };
  systemd.sockets.basix-live-test = { wantedBy = [ "sockets.target" ]; listenStreams = [ "127.0.0.1:59999" ]; };
  systemd.timers.basix-live-test = { wantedBy = [ "timers.target" ]; timerConfig.OnCalendar = "daily"; };
}
"""
MODULES = {
    "extra-disk": EXTRA_DISK,
    "forced-destroy": "{ lib, ... }: { disko.devices.disk.main.destroy = lib.mkForce true; }\n",
    "hookless": '{ lib, ... }: { disko.devices.disk.main.preCreateHook = lib.mkForce ""; }\n',
    "no-user": "{ lib, ... }: { basix.user = lib.mkForce null; }\n",
    "bt-off": "{ ... }: { hardware.bluetooth.enable = false; }\n",
    # SG31 F1 and F2: the fixed values.
    "mutable-users": "{ ... }: { users.mutableUsers = false; }\n",
    "root-password": '{ ... }: { users.users.root.hashedPassword = "$6$abc$def"; }\n',
    # SG31 F5: the five values that a machine may change, set together at normal priority.
    "free-values": (
        "{ ... }: { security.sudo.wheelNeedsPassword = false; zramSwap.enable = false; networking.networkmanager.enable = false;"
        " boot.loader.systemd-boot.configurationLimit = 20; hardware.enableRedistributableFirmware = false; }\n"
    ),
    "live-units": LIVE_UNITS,
    # L128: a rival value at normal priority (stands for a nixpkgs module). `Force` in `system.py` must win over it.
    "priority-rival": '{ ... }: { time.timeZone = "UTC"; networking.nameservers = [ "9.9.9.9" ]; }\n',
    # The control: a rival at the priority of `Force` conflicts with the `Force` of the entry.
    "priority-clash": '{ lib, ... }: { time.timeZone = lib.mkForce "UTC"; }\n',
}

#: The desktop written by hand in Nix (the form of 007 S8, with the session wrapper of L67). The component `Desktop` must
#: build the same machine (S18 equality). The stub lists the native part of the profile before this file, as `lib.machine`
#: does for a profile. The order of the modules changes the order of `environment.systemPackages`, and so the derivation.
DESKTOP_NATIVE = """\
{ config, lib, pkgs, ... }: {
  programs.hyprland = { enable = true; withUWSM = true; };
  programs.noctalia = { enable = true; systemd.enable = true; recommendedServices.enable = true; };
  services.greetd = {
    enable = true;
    useTextGreeter = true;
    settings.default_session.command = lib.mkDefault (lib.concatStringsSep " " [
      (lib.getExe pkgs.tuigreet) "--time" "--remember" @GREETING@ "--cmd basix-session"
    ]);
  };
  i18n.defaultLocale = lib.mkDefault "@LOCALE@";
  @IMPERATIVE@
  @KEYMAP@
  environment.systemPackages = [ pkgs.foot pkgs.firefox @EXTRA@ ];
  systemd.user.tmpfiles.rules = [
    "d %h/.config/noctalia 0755 - - -"
    "L %h/.config/noctalia/00-basix.toml - - - - /etc/xdg/noctalia/00-basix.toml"
  ];
}
"""
DESKTOP_PLAIN = {"@GREETING@": "", "@LOCALE@": "C.UTF-8", "@IMPERATIVE@": "", "@KEYMAP@": "", "@EXTRA@": ""}
DESKTOP_FULL = {
    "@GREETING@": "\"--greeting 'Welcome to Basix, Ada!'\"",
    "@LOCALE@": "en_US.UTF-8",
    "@IMPERATIVE@": "i18n.imperativeLocale = lib.mkDefault false;",  # 010-D24: the learner set the locale
    "@KEYMAP@": 'console.keyMap = lib.mkDefault "de";',
    "@EXTRA@": "pkgs.htop",
}
#: L42: a native module sets four `Default(...)` values at normal priority. No `mkForce`, and no conflict.
DESKTOP_OVERRIDE = """\
{ ... }: {
  i18n.defaultLocale = "de_DE.UTF-8";
  console.keyMap = "us";
  services.greetd.settings.default_session.command = "agreety --cmd bash";
  environment.etc."xdg/hypr/hyprland.lua".text = "-- my own default";
  environment.etc."xdg/noctalia/00-basix.toml".text = "# my own default";
}
"""
#: The stub of the hand-written desktop: the native part of the profile, then the file `extra.nix`.
DESKTOP_STUB = STUB % {"extra": '      extraModules = [ "${basix}/profiles/desktop/module.nix" ./extra.nix ];\n'}
#: A stub for the VM check: the test VM module of the desktop check (`fixtures/vmdesk.nix`) joins the machine.
VM_STUB = """\
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


def clean_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "UV_PROJECT_ENVIRONMENT"}
    env["NO_COLOR"] = "1"
    return env


def child(
    argv: list[str], cwd: Path, env: dict[str, str], timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a child process when a slot is free."""
    with SLOTS:
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)


def go[T](job: Callable[[], T]) -> Future[T]:
    """Start a sub-job in its own thread. Read the result with `.result()`, in the order of the report."""
    return JOBS_POOL.submit(job)


def run(name: str, argv: list[str], cwd: Path, timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    done = child(argv, cwd, clean_env(), timeout)
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    (LOGS / f"{stem}.cmd").write_text(json.dumps({"argv": argv, "returncode": done.returncode}) + "\n")
    (LOGS / f"{stem}.out").write_text(done.stdout)
    (LOGS / f"{stem}.err").write_text(done.stderr)
    return done


def emit(text: str) -> None:
    """Print a line of a step. A step keeps its lines, so the steps print in order."""
    lines = getattr(STEP, "lines", None)
    if lines is None:
        print(text, flush=True)
    else:
        lines.append(text)


def report(ok: bool, text: str) -> None:
    with LOCK:
        RESULTS.append((ok, text))
    emit(("PASS " if ok else "FAIL ") + text)


def parts(*jobs: Callable[[], None]) -> None:
    """Run the parts of a step side by side. Replay the lines of each part in order, as a serial run prints them.

    A part that raises `RuntimeError` stops the replay, as the same error stops a serial step."""

    def collect(job: Callable[[], None]) -> tuple[list[str], BaseException | None]:
        STEP.lines = []
        error = None
        try:
            job()
        except BaseException as caught:  # the caller replays it
            error = caught
        lines, STEP.lines = STEP.lines, None
        return lines, error

    for future in [go(lambda job=job: collect(job)) for job in jobs]:
        lines, error = future.result()
        for line in lines:
            emit(line)
        if error is not None:
            raise error


def error_text(done: subprocess.CompletedProcess[str]) -> str:
    return ANSI.sub("", done.stderr + done.stdout)


# --- the copies -------------------------------------------------------------------------------------------------------


def stage_flake(root: Path) -> Path:
    """Copy the files of the flake to a clean folder, so the evaluation reads exactly these files."""
    target = WORK / "basix"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for name in FLAKE_FILES:
        source = root / name
        if source.is_dir():
            shutil.copytree(source, target / name, ignore=shutil.ignore_patterns("__pycache__", ".devenv", ".direnv"))
        elif source.is_file():
            shutil.copy2(source, target / name)
    chosen = os.environ.get("BASIX_PYNIX_PATH")
    if chosen:  # the compiler of the staged flake then holds the same pynix as the other steps
        locked = run("stage-pynix", ["nix", "flake", "lock", "--override-input", "pynix", f"path:{chosen}"], target)
        if locked.returncode != 0:
            raise RuntimeError(f"BASIX_PYNIX_PATH does not lock: {error_text(locked)[-500:]}")
    return target


class Compiler:
    """The compile step of the fixtures (see the module text)."""

    def __init__(self, root: Path, flake: Path) -> None:
        self.mode = "stored" if os.environ.get("BASIX_COMPILE") == "0" else "compile"
        self.command: list[str] = []
        self.note = ""
        if self.mode == "compile":
            named = os.environ.get("BASIX_COMPILER")
            if named:
                self.command = [named]
                self.note = f"compiler {named}"
            else:
                done = run(
                    "build-compiler",
                    [
                        "nix",
                        "build",
                        f"path:{flake}#compiler",
                        "--no-link",
                        "--print-out-paths",
                        "--no-write-lock-file",
                    ],
                    root,
                )
                out = done.stdout.strip().splitlines()
                if done.returncode != 0 or not out:
                    self.mode = "failed"
                    self.note = error_text(done)[-1500:]
                else:
                    self.command = [f"{out[-1]}/bin/basix-compile"]
                    self.note = f"compiler {out[-1]}"
        else:
            self.note = "stored system.json"

    def compile(self, folder: Path, name: str, enabled: bool) -> str | None:
        """Make `system.json` in `folder`. Return an error text, or None."""
        if not enabled or self.mode == "stored":
            return None
        if self.mode == "failed":
            return "the compiler did not build: " + self.note
        done = run(f"compile-{name}", [*self.command, "system.py", "system.json"], folder)
        return None if done.returncode == 0 else error_text(done)[-1500:]


class Stages:
    """Copy fixtures into the work folder. A compiled fixture is compiled once and copied for each variant.

    All threads share one object. A lock for each fixture makes the compile run once."""

    def __init__(self, root: Path, flake: Path, compiler: Compiler) -> None:
        self.root = root
        self.flake = flake
        self.compiler = compiler
        self.compiled: dict[str, Path] = {}
        self.failed: dict[str, str] = {}
        self.locks: dict[str, threading.Lock] = {}
        self.count = 0

    def prefetch(self, fixtures: tuple[str, ...]) -> None:
        """Compile the fixtures side by side and wait. A failed compile stays in `failed` and shows at its first use."""
        for future in [go(lambda name=name: self.base(name)) for name in fixtures]:
            try:
                future.result()
            except RuntimeError:
                pass

    def base(self, fixture: str) -> Path:
        with self.locks.setdefault(fixture, threading.Lock()):
            if fixture in self.failed:
                raise RuntimeError(self.failed[fixture])
            if fixture not in self.compiled:
                folder = WORK / "fixtures" / fixture
                if folder.exists():
                    shutil.rmtree(folder)
                shutil.copytree(self.root / "fixtures" / fixture, folder)
                problem = self.compiler.compile(folder, fixture, enabled=fixture != "input")
                if problem:
                    self.failed[fixture] = f"compile of fixture {fixture} failed: {problem}"
                    raise RuntimeError(self.failed[fixture])
                self.compiled[fixture] = folder
            return self.compiled[fixture]

    def make(
        self,
        fixture: str,
        label: str,
        *,
        module: str | None = None,
        stub: str | None = None,
        edit: Callable[[Path], None] | None = None,
        drop: str | None = None,
    ) -> Path:
        """A copy of a compiled fixture. `module` is the text of `extra.nix`; `stub` replaces `flake.nix`; `drop` removes a file."""
        source = self.base(fixture)
        with LOCK:
            self.count += 1
            number = self.count
        target = WORK / "cases" / f"{number:02d}-{label}"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        if drop:
            (target / drop).unlink()
        if module is not None:
            (target / "extra.nix").write_text(module)
            if stub is None:
                stub = STUB % {"extra": EXTRA_MODULE}
            if "imports = [ ./disk-extra.nix ]" in module:
                (target / "disk-extra.nix").write_text(EXTRA_DISK)
        if stub is not None:
            (target / "flake.nix").write_text(stub)
        if edit:
            edit(target)
        return target


def nix_args(flake: Path, stage: Path, attr: str, *, override_pynix: Path | None = None) -> list[str]:
    args = ["--no-warn-dirty", "--no-write-lock-file", "--override-input", "basix", f"path:{flake}"]
    if override_pynix is not None:
        args += ["--override-input", "basix/pynix", f"path:{override_pynix}"]
    return [*args, f"path:{stage}#{attr}"]


class Evaluator:
    def __init__(self, root: Path, flake: Path) -> None:
        self.root = root
        self.flake = flake
        self.facts_text = (root / "fixtures" / "facts.nix").read_text()

    def drv(
        self, name: str, stage: Path, attr: str = "trybox.config.system.build.toplevel", *, pynix: Path | None = None
    ):
        argv = [
            "nix",
            "eval",
            "--raw",
            *nix_args(self.flake, stage, f"nixosConfigurations.{attr}.drvPath", override_pynix=pynix),
        ]
        return run(name, argv, self.root)

    def facts_run(self, name: str, stage: Path, machine: str = "trybox") -> subprocess.CompletedProcess[str]:
        argv = [
            "nix",
            "eval",
            "--json",
            *nix_args(self.flake, stage, f"nixosConfigurations.{machine}.config"),
            "--apply",
            self.facts_text,
        ]
        return run(name, argv, self.root)

    @staticmethod
    def facts_read(name: str, done: subprocess.CompletedProcess[str]) -> dict | None:
        """The facts of a finished `facts_run`. A failure reports here, so the line stands where a serial run puts it."""
        if done.returncode != 0:
            report(False, f"{name}: the facts evaluate")
            emit(error_text(done)[-1500:])
            return None
        return json.loads(done.stdout)

    def facts(self, name: str, stage: Path, machine: str = "trybox") -> dict | None:
        return self.facts_read(name, self.facts_run(name, stage, machine))


# --- the cases --------------------------------------------------------------------------------------------------------


def expect_equal(facts: dict, label: str, expected: dict) -> None:
    for key, want in expected.items():
        report(facts.get(key) == want, f"{label}: {key} == {want!r} (got {facts.get(key)!r})")


def refusal(
    ev: Evaluator,
    name: str,
    stage: Path,
    wanted: str,
    attr: str = "trybox.config.system.build.toplevel",
    pynix: Path | None = None,
) -> tuple[bool, str]:
    done = ev.drv(name, stage, attr, pynix=pynix)
    text = error_text(done)
    return done.returncode != 0 and wanted in text, text


def check_guard_file(root: Path) -> None:
    digest = hashlib.sha256((root / "modules" / "disk-guard.sh").read_bytes()).hexdigest()
    report(digest == GUARD_SHA256, f"the disk guard file is unchanged (sha256 {digest})")


def check_core(ev: Evaluator, stages: Stages) -> None:
    core = stages.make("core", "core")
    both = ["trybox.config.system.build.toplevel", "trybox.config.system.build.diskoScript"]
    drvs = [go(lambda attr=attr: ev.drv(f"core-{attr.split('.')[-1]}", core, attr)) for attr in both]
    facts_job = go(lambda: ev.facts_run("core-facts", core))
    for attr, future in zip(both, drvs, strict=True):
        done = future.result()
        report(done.returncode == 0, f"core: the machine with one guarded disk evaluates ({attr.split('.')[-1]})")
        if done.returncode != 0:
            emit(error_text(done)[-1500:])
    facts = ev.facts_read("core-facts", facts_job.result())
    if facts is None:
        return
    # BSX-298: terminfo of common terminals.
    for term in ("kitty", "foot", "alacritty", "wezterm", "ghostty"):
        report(
            any(p.startswith(term) for p in facts["terminfo"]),
            f"core: the terminfo of {term} is in the system packages (BSX-298)",
        )
    # L113, 010-D15: the launcher runs the public release of minix; the flake has no `minix` input.
    launcher = facts["minixLauncher"]
    report(len(launcher) == 1, f"core: one script `minix` is in the system packages (got {len(launcher)})")
    if len(launcher) == 1:
        report(MINIX_COMMAND in launcher[0], "core: the script `minix` runs the pinned public release of minix")
    packages = set(facts["packages"])
    for tool in ("git", "networkmanager"):
        report(
            any(p == tool or p.startswith(tool + "-") for p in packages),
            f"core: {tool} is in the system packages (BSX-203)",
        )
    for tool in ("devenv", "vendomat-launcher"):
        report(
            not any(p == tool or p.startswith(tool + "-") for p in packages),
            f"core: {tool} is not in the system packages (v2 has no Vendomat)",
        )
    expect_equal(
        facts,
        "core",
        {
            "rootKeys": [],
            "rootHashedPassword": "!",
            "passwordUsers": [],
            "keyUsers": [],
            "learnerGroups": ["wheel", "networkmanager"],
            "linger": True,  # 010-D16
            "mutableUsers": True,
            "sshd": False,
            "timeZone": None,
            "imperativeLocale": True,
            "zram": True,
            "bluetooth": {"enable": True, "powerOnBoot": True},
            "swapDevices": [],
            "networkManager": True,
            "firmware": True,
            "systemdBoot": True,
            "bootLimit": 10,
            "kernelConsole": [],
            "stateVersion": "26.11",
            "wheelNeedsPassword": True,
            "hostName": "trybox",
            "trustedInterfaces": [],
            "diskNames": ["main"],
            "gc": {"automatic": True, "dates": ["weekly"], "options": "--delete-older-than 30d"},
            # P6: the console profile carries nothing of the desktop.
            "hyprland": False,
            "noctalia": False,
            "greetd": False,
            "greeterCommand": None,
            "etcHyprland": None,
            "etcNoctalia": None,
        },
    )


def check_net(ev: Evaluator, stages: Stages) -> None:
    facts = ev.facts("net-facts", stages.make("net", "net"))
    if facts is None:
        return
    # BSX-297: a report with one NIC must not start dhcpcd next to NetworkManager.
    expect_equal(
        facts,
        "net fixture",
        {
            "networkManager": True,
            "dhcpInterfaces": [],
            "dhcpcdService": False,
            "globalDhcp": False,
            "bluetooth": {
                "enable": True,
                "powerOnBoot": True,
            },  # BSX-325: a report with no controller keeps Bluetooth on
        },
    )


def check_variants(ev: Evaluator, stages: Stages) -> None:
    parts(
        lambda: variant_bluetooth(ev, stages),
        lambda: variant_free_values(ev, stages),
        lambda: variant_typed_core(ev, stages),
        lambda: variant_priority(ev, stages),
        lambda: variant_disk_none(ev, stages),
        lambda: variant_trusted(ev, stages),
        lambda: variant_ownlink(ev, stages),
        lambda: variant_vmhost(ev, stages),
        lambda: variant_live(ev, stages),
    )


def variant_bluetooth(ev: Evaluator, stages: Stages) -> None:
    # A plain `false` turns Bluetooth off, so a later ruling can do it (BSX-325).
    facts = ev.facts("bt-off-facts", stages.make("core", "bt-off", module=MODULES["bt-off"]))
    if facts is not None:
        expect_equal(facts, "bt-off", {"bluetooth": {"enable": False, "powerOnBoot": True}})


def variant_free_values(ev: Evaluator, stages: Stages) -> None:
    # SG31 F5: the five values together, at normal priority, with no `mkForce`.
    facts = ev.facts("free-values-facts", stages.make("core", "free-values", module=MODULES["free-values"]))
    if facts is not None:
        expect_equal(
            facts,
            "mkDefault",
            {"wheelNeedsPassword": False, "zram": False, "networkManager": False, "bootLimit": 20, "firmware": False},
        )


def variant_typed_core(ev: Evaluator, stages: Stages) -> None:
    # SG31 EX1B: the machine sets its own state version, time zone, and garbage collection.
    facts = ev.facts("typed-core-facts", stages.make("typed-core", "typed-core"))
    if facts is not None:
        expect_equal(
            facts,
            "typed-core",
            {
                "stateVersion": "26.05",
                "timeZone": "America/New_York",
                "gc": {"automatic": True, "dates": ["weekly"], "options": "--delete-older-than 14d"},
                "diskNames": ["main"],
            },
        )


def variant_priority(ev: Evaluator, stages: Stages) -> None:
    # L128, BSX-396: `Force` wins over a normal value and over the core; `Before` and `After` order a list.
    priority = stages.make("priority", "priority", module=MODULES["priority-rival"])
    facts_job = go(lambda: ev.facts_run("priority-facts", priority))
    drv_job = go(lambda: ev.drv("priority-drv", priority))
    facts = ev.facts_read("priority-facts", facts_job.result())
    if facts is not None:
        expect_equal(
            facts, "priority", {"timeZone": "America/New_York", "nameservers": ["1.1.1.1", "9.9.9.9", "8.8.8.8"]}
        )
    done = drv_job.result()
    report(done.returncode == 0, f"priority: the toplevel evaluates (drvPath {done.stdout.strip()})")


def variant_disk_none(ev: Evaluator, stages: Stages) -> None:
    # SG31 EX1M: `disk = None` adds no disko disk and no guard; the machine keeps its file systems.
    none = stages.make("disk-none", "disk-none")
    facts = ev.facts("disk-none-facts", none)
    if facts is not None:
        expect_equal(
            facts, "disk-none", {"diskNames": [], "rootFsType": "btrfs", "bootFsType": "vfat", "stateVersion": "26.05"}
        )


def variant_trusted(ev: Evaluator, stages: Stages) -> None:
    # L125: a trusted link maps to the firewall option; port 22 opens on that link only.
    facts = ev.facts("trusted-facts", stages.make("trusted", "trusted"))
    if facts is not None:
        expect_equal(
            facts,
            "trusted",
            {
                "trustedInterfaces": ["enp1s0"],
                "openTcpByInterface": {"enp1s0": [22]},
                "openTcpGlobal": [],
                "sshd": True,
            },
        )


def variant_ownlink(ev: Evaluator, stages: Stages) -> None:
    # 010-D45: no trusted link, and `Port(22, interface=...)` opens SSH. Avahi serves those links, and UDP 5353 opens on them
    # only. The learner's own rule opens TCP 22 (once); `remote-login` adds none. Port 80 on `enp3s0` gets no mDNS.
    facts = ev.facts("ownlink-facts", stages.make("ownlink", "ownlink"))
    if facts is not None:
        expect_equal(
            facts,
            "ownlink",
            {
                "trustedInterfaces": [],
                "sshd": True,
                "avahi": {
                    "enable": True,
                    "allowInterfaces": ["eno1", "wlp2s0"],
                    "nssmdns4": True,
                    "publishAddresses": True,
                },
                "openTcpByInterface": {"eno1": [22], "wlp2s0": [22], "enp3s0": [80]},
                "openUdpByInterface": {"eno1": [5353], "wlp2s0": [5353], "enp3s0": []},
                "openTcpGlobal": [],
            },
        )


def variant_vmhost(ev: Evaluator, stages: Stages) -> None:
    # The Basix host of the minix VM check: `disk = None`, a root file system of its own, a serial console, no disk layout.
    facts_stage = stages.make("vmhost", "vmhost")
    drv_stage = stages.make("vmhost", "vmhost-drv")
    drv_job = go(lambda: ev.drv("vmhost-drv", drv_stage, "basix.config.system.build.toplevel"))
    facts = ev.facts("vmhost-facts", facts_stage, "basix")
    if facts is not None:
        expect_equal(
            facts,
            "vmhost",
            {
                "hostName": "basix",
                "diskNames": [],
                "rootFsType": "ext4",
                "linger": True,
                "sshd": False,
                "stateVersion": "26.05",
            },
        )
        report(
            facts["kernelConsole"] == ["console=ttyS0"],
            f"vmhost: the serial console is ttyS0 (got {facts['kernelConsole']!r})",
        )
    done = drv_job.result()
    report(
        done.returncode == 0,
        f"vmhost: the toplevel evaluates (drvPath {done.stdout.strip() or error_text(done)[-300:]})",
    )


def variant_live(ev: Evaluator, stages: Stages) -> None:
    # BSX-097: `liveOff` turns the service, the socket, and the timer off, and only in the ISO.
    live = stages.make("core", "live", module=MODULES["live-units"], stub=LIVE_STUB)
    off_job = go(lambda: ev.facts_run("live-off-facts", live, "trybox-off"))
    plain = ev.facts("live-plain-facts", live, "trybox")
    off = ev.facts_read("live-off-facts", off_job.result())
    if plain is not None and off is not None:
        units = ["basix-live-test.service", "basix-live-test.socket", "basix-live-test.timer"]
        report(
            sorted(plain["liveUnits"]) == units,
            f"liveOff fixture: the machine keeps its service, socket, and timer (got {plain['liveUnits']!r})",
        )
        report(off["liveUnits"] == [], f"liveOff fixture: the ISO policy turns them off (got {off['liveUnits']!r})")


def check_refusals(ev: Evaluator, stages: Stages) -> None:
    both = ["trybox.config.system.build.toplevel", "trybox.config.system.build.diskoScript"]
    jobs: list[tuple[str, Path, str, str]] = []
    for name, wanted in (
        ("extra-disk", 'basix: disk "extra" has no Basix guard'),
        ("forced-destroy", 'basix: disk "main" sets destroy = true'),
        ("hookless", 'basix: disk "main" has no Basix guard in preCreateHook'),
    ):
        stage = stages.make("core", name, module=MODULES[name])
        jobs += [(f"{name}-{attr.split('.')[-1]}", stage, attr, wanted) for attr in both]
    jobs.append(
        (
            "no-user",
            stages.make("core", "no-user", module=MODULES["no-user"]),
            both[0],
            "basix: the machine has no account",
        )
    )
    # SG31 F1 and F2: the fixed values give a Basix line, not the advice "use lib.mkForce".
    jobs.append(
        (
            "mutable-users",
            stages.make("core", "mutable-users", module=MODULES["mutable-users"]),
            both[0],
            "basix: users.mutableUsers must stay true",
        )
    )
    jobs.append(
        (
            "root-password",
            stages.make("core", "root-password", module=MODULES["root-password"]),
            both[0],
            "basix: the root account stays locked",
        )
    )
    # L128: a rival at the priority of `Force` conflicts. So the `Force` of the entry has priority 50, not normal.
    clash = stages.make("priority", "priority-clash", module=MODULES["priority-clash"])
    jobs.append(("priority-clash", clash, both[0], "conflicting definition values"))
    # SG31 F7: `disk = None` needs a root file system.
    no_root = stages.make("disk-none", "disk-none-no-root", stub=STUB % {"extra": ""}, drop="extra.nix")
    jobs.append(("disk-none-no-root", no_root, both[0], 'defines no root file system "/"'))
    results = [go(lambda job=job: refusal(ev, job[0], job[1], job[3], job[2])) for job in jobs]
    for (name, _stage, _attr, wanted), future in zip(jobs, results, strict=True):
        ok, text = future.result()
        report(ok, f"{name}: evaluation fails with: {wanted}")
        if not ok:
            emit(text[-1500:])


def append_line(name: str) -> Callable[[Path], None]:
    def edit(folder: Path) -> None:
        with (folder / name).open("a") as handle:
            handle.write("\n# edited after the compile\n")

    return edit


def edit_json(change: Callable[[dict], None]) -> Callable[[Path], None]:
    def edit(folder: Path) -> None:
        data = json.loads((folder / "system.json").read_text())
        change(data)
        (folder / "system.json").write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")

    return edit


def check_guards(ev: Evaluator, stages: Stages) -> None:
    """The guard lines of `lib.machine`: each stops with one plain line (009-D28, 009-D45, 009-D48, 009-D49)."""
    fake = WORK / "pynix-other-nixpkgs"
    (fake / "src" / "pynix_os").mkdir(parents=True, exist_ok=True)
    (fake / "src" / "pynix_os" / "_generation.py").write_text(
        'SOURCE: dict[str, str] = {"nixpkgs": "' + "b" * 40 + '"}\n'
    )

    def no_reads(data: dict) -> None:
        del data["reads"]

    def lacks_input(data: dict) -> None:
        data["inputs"][0]["name"] = "bar"

    def stale_package(data: dict) -> None:
        data["input_packages"]["foo"] = "sha256-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB="

    cases = [
        (
            "stale-source",
            stages.make("core", "stale-source", edit=append_line("system.py")),
            "system.py changed: run `basix apply`",
            None,
        ),
        (
            "stale-no-reads",
            stages.make("core", "stale-no-reads", edit=edit_json(no_reads)),
            "system.py changed: run `basix apply`",
            None,
        ),
        (
            "stale-read",
            stages.make("reads", "stale-read", edit=append_line("note.txt")),
            "note.txt changed: run `basix apply`",
            None,
        ),
        (
            "missing-read",
            stages.make("reads", "missing-read", drop="note.txt"),
            "note.txt is not in the flake. Run `git add note.txt`, then `basix apply`",
            None,
        ),
        (
            "pair-nixpkgs",
            stages.make("core", "pair-nixpkgs"),
            f"basix: pynix_os was generated from nixpkgs {'b' * 40}, but flake.lock holds nixpkgs",
            fake,
        ),
        (
            "input-missing",
            stages.make("input", "input-missing", edit=edit_json(lacks_input)),
            "the input `bar` is not in flake.nix: run `basix apply`",
            None,
        ),
        (
            "input-package-stale",
            stages.make("input", "input-package-stale", edit=edit_json(stale_package)),
            "the package `pynix_foo` was made from another version of `foo` than flake.lock holds; run `basix apply` to make it again",
            None,
        ),
    ]
    results = [go(lambda c=c: refusal(ev, c[0], c[1], c[2], pynix=c[3])) for c in cases]
    # The pair that matches evaluates.
    input_ok = stages.make("input", "input-ok")
    ok_job = go(lambda: ev.drv("input-ok", input_ok))
    for (name, _stage, wanted, _pynix), future in zip(cases, results, strict=True):
        ok, text = future.result()
        report(ok, f"{name}: evaluation fails with: {wanted}")
        if not ok:
            emit(text[-1500:])
    done = ok_job.result()
    report(done.returncode == 0, "input-ok: an input package from the locked input evaluates")
    if done.returncode != 0:
        emit(error_text(done)[-1500:])


def check_anchor(ev: Evaluator, stages: Stages) -> None:
    """T3: the anchor machine builds the recorded derivation through `lib.machine` on flake-parts (BSX-363, 010-D16)."""

    def disable(modules: list[str], extra: str = "") -> Callable[[Path], None]:
        """Edit a copy of T3: a module `no-linger.nix` that disables modules of Basix (and sets `extra`)."""

        def edit(folder: Path) -> None:
            names = "".join(f'    "${{inputs.basix}}/modules/{name}"\n' for name in modules)
            (folder / "no-linger.nix").write_text(
                "{ lib, inputs, ... }: {\n" + extra + "  disabledModules = [\n" + names + "  ];\n}\n"
            )
            flake = folder / "flake.nix"
            flake.write_text(flake.read_text().replace("[ ./vm.nix ]", "[ ./vm.nix ./no-linger.nix ]"))

        return edit

    # The anchor leaves out `basix-command.nix` (see `T3_HASH`). The only changes since ACC1 are then `linger` and the
    # script `minix`: with the option unset and the module of the script disabled, the old hash returns.
    anchor = stages.make("t3", "t3", edit=disable(["basix-command.nix"]))
    plain_job = go(lambda: ev.drv("t3", anchor))
    other = stages.make(
        "t3",
        "t3-no-linger",
        edit=disable(["minix.nix", "basix-command.nix"], "  users.users.learner.linger = lib.mkForce null;\n"),
    )
    other_job = go(lambda: ev.drv("t3-no-linger", other))
    done = plain_job.result()
    out = done.stdout.strip()
    ok = done.returncode == 0 and f"/{T3_HASH}-" in out
    report(ok, f"T3 anchor: the drvPath hash is {T3_HASH} (got {out or error_text(done)[-300:]!r})")
    done = other_job.result()
    out = done.stdout.strip()
    ok = done.returncode == 0 and f"/{T3_NO_LINGER_HASH}-" in out
    report(
        ok,
        f"T3 with linger unset and no launcher: the drvPath hash is {T3_NO_LINGER_HASH}, the value of ACC1 (got {out or error_text(done)[-300:]!r})",
    )


def check_command(ev: Evaluator, stages: Stages) -> None:
    """The full T3 machine holds the `basix` command and the compiler of its own Basix flake (010-D37)."""
    full = stages.make("t3", "t3-full")
    held_job = go(
        lambda: run(
            "command-held",
            [
                "nix",
                "eval",
                "--json",
                *nix_args(ev.flake, full, "nixosConfigurations.trybox.config.environment.systemPackages"),
                "--apply",
                "packages: map (p: p.drvPath or null) packages",
            ],
            ev.root,
        )
    )
    wanted_jobs = {
        name: go(
            lambda name=name: run(
                f"command-{name}",
                ["nix", "eval", "--raw", f"path:{ev.flake}#packages.x86_64-linux.{name}.drvPath"],
                ev.root,
            )
        )
        for name in ("basix", "compiler")
    }
    held = held_job.result()
    if held.returncode != 0:
        report(False, "command: the system packages of the full T3 machine evaluate")
        emit(error_text(held)[-1500:])
        return
    paths = json.loads(held.stdout)
    for name, job in wanted_jobs.items():
        done = job.result()
        want = done.stdout.strip()
        ok = done.returncode == 0 and bool(want) and want in paths
        report(
            ok,
            f"command: the system packages hold the `{name}` package of the same flake ({want or error_text(done)[-300:]})"
            if ok
            else f"command: the system packages of the full T3 machine lack the `{name}` package of the same flake"
            f" (wanted {want or error_text(done)[-300:]!r})",
        )


#: The text that the stub writer makes for an entry with no `Input`: CONCEPT Appendix C, first block, with `root = ./.;`
#: and the compiler package line of the managed file (009-D3).
PLAIN_STUB = """\
# Managed by Basix. Do not edit.
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }: {
    packages.x86_64-linux.compiler = basix.packages.x86_64-linux.compiler;
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
    };
  };
}
"""
STUB_CODE = """\
import sys
from pathlib import Path

from pynix_native import discover_source

from basix_config.stub import make_stub

source = Path(sys.argv[1])
sys.stdout.write(make_stub(discover_source(source.read_text(), source.name)).text)
"""
#: An entry with a local `Input`. `%(url)s` is the folder of the input. The folder is a plain source folder
#: (`flake=False`): an input with a typed package needs the packages that `basix apply` makes (P3).
INPUT_ENTRY = """\
from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    inputs=[Input("notes", "%(url)s", flake=False)],
)
"""


def compiler_python(compiler: Compiler) -> tuple[Path, Path] | None:
    """The Python and the library folder of the compiler package, read from its launcher script."""
    if compiler.mode != "compile" or len(compiler.command) != 1:
        return None
    launcher = Path(compiler.command[0])
    try:
        found = re.search(r"^exec (\S+) -P -m basix_config\.compile", launcher.read_text(), re.MULTILINE)
    except (OSError, UnicodeDecodeError):
        return None
    return (Path(found.group(1)), launcher.parent.parent / "lib") if found else None


def write_stub(folder: Path, python: Path, library: Path) -> str:
    """Run the stub writer of the compiler package on `system.py` in `folder`."""
    env = {**clean_env(), "PYTHONPATH": str(library)}
    done = child([str(python), "-P", "-c", STUB_CODE, "system.py"], folder, env)
    (LOGS / f"stub-{folder.name}.err").write_text(done.stderr)
    if done.returncode != 0:
        raise RuntimeError(f"the stub writer failed in {folder.name}: {done.stderr[-500:]}")
    return done.stdout


def check_stub(ev: Evaluator, stages: Stages, compiler: Compiler) -> None:
    """The stub writer (L129): the managed `flake.nix` of an entry with no `Input` and of one with a local `Input`
    evaluates through `lib.machine`, with the input `basix` replaced by the staged flake."""
    tools = compiler_python(compiler)
    if tools is None:
        emit("SKIP stub: the compiler is not the package of the flake, so the stub writer is not in reach")
        return
    python, library = tools
    parts(
        lambda: stub_plain(ev, stages, python, library), lambda: stub_local_input(ev, stages, compiler, python, library)
    )


def stub_plain(ev: Evaluator, stages: Stages, python: Path, library: Path) -> None:
    # No `Input`: the text is the first block of Appendix C, and the machine is the one of the hand-written stub.
    plain = stages.make("core", "stub-plain")
    reference = stages.make("core", "stub-plain-ref")
    same_job = go(lambda: ev.drv("stub-plain-ref", reference))
    text = write_stub(plain, python, library)
    report(text == PLAIN_STUB, "stub, no input: the text of the writer is Appendix C with `root = ./.;`")
    (plain / "flake.nix").write_text(text)
    done = ev.drv("stub-plain", plain)
    same = same_job.result()
    ok = done.returncode == 0 and done.stdout.strip() == same.stdout.strip() != ""
    report(
        ok,
        f"stub, no input: the machine evaluates, and equals the machine of the hand-written stub (got {done.stdout.strip() or error_text(done)[-300:]!r})",
    )


def stub_local_input(ev: Evaluator, stages: Stages, compiler: Compiler, python: Path, library: Path) -> None:
    # A local `Input`: the writer adds the input, and `lib.machine` reads it as `extraInputs`.
    source = WORK / "stub-input-source"
    source.mkdir(parents=True, exist_ok=True)
    (source / "note.txt").write_text("a plain source folder\n")
    folder = stages.make("core", "stub-input")
    (folder / "system.py").write_text(INPUT_ENTRY % {"url": f"path:{source}"})
    problem = compiler.compile(folder, "stub-input", enabled=True)
    if problem:
        report(False, f"stub, local input: the entry compiles ({problem})")
        return
    text = write_stub(folder, python, library)
    (folder / "flake.nix").write_text(text)
    wanted = ["inputs.notes.flake = false;", "extraInputs = removeAttrs inputs"]
    report(all(line in text for line in wanted), "stub, local input: the text holds the input and `extraInputs`")
    done = ev.drv("stub-input", folder)
    report(
        done.returncode == 0,
        f"stub, local input: the machine evaluates ({done.stdout.strip() or error_text(done)[-300:]!r})",
    )


#: An entry with a typed `Input` on a NixOS module (`fixtures/demo-input`). `%(url)s` is the folder of the input. The
#: class `Demo` exists only after `basix apply` has built the input packages (010-D13).
TYPED_INPUT_ENTRY = """\
import pynix_demo as demo

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    inputs=[Input("demo", "%(url)s", modules=["demo"])],
    options=[demo.Demo(enable=True, greeting="hi", count=2)],
)
"""


def check_typed_input(ev: Evaluator, stages: Stages, compiler: Compiler) -> None:
    """One typed `Input(flake=True)` end to end, as `basix apply` runs it: the stub, the lock, the input package, the
    compile, and the toplevel (BSX-397). The stub writer and the compiler are the ones of the flake."""
    tools = compiler_python(compiler)
    if tools is None:
        emit("SKIP typed input: the compiler is not the package of the flake, so the stub writer is not in reach")
        return
    python, library = tools
    source = WORK / "typed-input-source"
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(ev.root / "fixtures" / "demo-input", source)
    folder = stages.make("core", "typed-input")
    (folder / "system.py").write_text(TYPED_INPUT_ENTRY % {"url": f"path:{source}"})
    (folder / "system.json").unlink()
    # 1. The stub, from the syntax tree of the entry (nothing runs yet: the class `Demo` does not exist).
    env = {**clean_env(), "PYTHONPATH": str(library)}
    done = child([str(python), "-P", "-c", STUB_CODE, "system.py"], folder, env)
    (LOGS / "typed-input-stub.err").write_text(done.stderr)
    if done.returncode != 0:
        report(False, f"typed input: the stub writer runs ({done.stderr[-500:]})")
        return
    stub = done.stdout
    (folder / "flake.nix").write_text(stub)
    wanted = [
        "packages.x86_64-linux.input-packages = basix.lib.inputPackages {",
        'modules = { demo = [ "demo" ]; };',
        'inputs.demo.inputs.nixpkgs.follows = "nixpkgs";',
    ]
    report(
        all(line in stub for line in wanted),
        "typed input: the stub holds the input, its follows line, and `input-packages` with the module",
    )
    # 2. The lock (`basix apply` locks when the stub changed), then the input package: the typed classes of the options.
    locked = run(
        "typed-input-lock",
        ["nix", "flake", "lock", "--no-warn-dirty", "--override-input", "basix", f"path:{ev.flake}"],
        folder,
    )
    report(
        locked.returncode == 0,
        f"typed input: the lock holds the input ({error_text(locked)[-300:] if locked.returncode else 'ok'})",
    )
    if locked.returncode != 0:
        return
    link = folder / ".basix" / "pynix"
    link.parent.mkdir(exist_ok=True)
    built = run(
        "typed-input-package",
        [
            "nix",
            "build",
            "--out-link",
            str(link),
            "--print-out-paths",
            "--no-warn-dirty",
            "--override-input",
            "basix",
            f"path:{ev.flake}",
            f"path:{folder}#input-packages",
        ],
        ev.root,
    )
    report(
        built.returncode == 0,
        f"typed input: the input package builds ({built.stdout.strip() or error_text(built)[-400:]!r})",
    )
    if built.returncode != 0:
        return
    names = sorted(p.name for p in link.iterdir())
    report(names == ["pynix_demo"], f"typed input: the package holds pynix_demo (got {names!r})")
    # 3. The compile reads `.basix/pynix`, so the entry can import the typed class.
    problem = compiler.compile(folder, "typed-input", enabled=True)
    report(problem is None, f"typed input: the entry compiles with the typed value ({problem})")
    if problem:
        return
    data = json.loads((folder / "system.json").read_text())
    report(
        list(data.get("input_packages", {})) == ["demo"],
        f"typed input: system.json records the narHash of the input package (got {list(data.get('input_packages', {}))!r})",
    )
    # 4. The toplevel: the module of the input is in the machine, and it wrote the typed value.
    etc_job = go(
        lambda: run(
            "typed-input-etc",
            [
                "nix",
                "eval",
                "--raw",
                *nix_args(ev.flake, folder, 'nixosConfigurations.trybox.config.environment.etc."demo/greeting".text'),
            ],
            ev.root,
        )
    )
    done = ev.drv("typed-input-drv", folder)
    report(
        done.returncode == 0,
        f"typed input: the toplevel evaluates ({done.stdout.strip() or error_text(done)[-400:]!r})",
    )
    text = etc_job.result()
    report(
        text.returncode == 0 and text.stdout == "hi x2\n",
        f"typed input: the module wrote the typed value (got {text.stdout!r})",
    )


#: An entry with an `Input(python="src")` on `fixtures/demo-python`. `%(url)s` is the folder of the input and
#: `%(flake)s` is empty or `, flake=False`. The time zone comes from the code of the input.
PYTHON_INPUT_ENTRY = """\
from comps import zone
from pynix_os.time.__options__ import Time

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    inputs=[Input("comps", "%(url)s", python="src"%(flake)s)],
    options=[Time(timeZone=zone())],
)
"""
PYTHON_INPUT_ERROR_ENTRY = PYTHON_INPUT_ENTRY.replace("zone()", "broken()").replace("import zone", "import broken")


def failure(done: subprocess.CompletedProcess[str]) -> str:
    """The end of the output of a failed command, for a FAIL line. Empty when the command passed."""
    return f" ({error_text(done)[-400:]})" if done.returncode else ""


def check_python_input(ev: Evaluator, stages: Stages, compiler: Compiler) -> None:
    """`Input(python="src")` end to end, as `basix apply` runs it. The input is a flake, then a plain source folder."""
    tools = compiler_python(compiler)
    if tools is None:
        emit("SKIP python input: the compiler is not the package of the flake, so the stub writer is not in reach")
        return
    parts(
        lambda: python_input_case(ev, stages, compiler, tools, flake=True),
        lambda: python_input_case(ev, stages, compiler, tools, flake=False),
    )


def python_input_case(
    ev: Evaluator, stages: Stages, compiler: Compiler, tools: tuple[Path, Path], *, flake: bool
) -> None:
    python, library = tools
    label = "python-input" if flake else "python-input-plain"
    shown = f"python input ({'flake' if flake else 'flake=False'})"
    source = WORK / f"{label}-source"
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(ev.root / "fixtures" / "demo-python", source)
    folder = stages.make("core", label)
    entry = {"url": f"path:{source}", "flake": "" if flake else ", flake=False"}
    (folder / "system.py").write_text(PYTHON_INPUT_ENTRY % entry)
    (folder / "system.json").unlink()
    # 1. The stub, from the syntax tree of the entry.
    done = child([str(python), "-P", "-c", STUB_CODE, "system.py"], folder, {**clean_env(), "PYTHONPATH": str(library)})
    (LOGS / f"{label}-stub.err").write_text(done.stderr)
    if done.returncode != 0:
        report(False, f"{shown}: the stub writer runs ({done.stderr[-500:]})")
        return
    (folder / "flake.nix").write_text(done.stdout)
    report('pythonDirs = { comps = "src"; };' in done.stdout, f"{shown}: the stub holds pythonDirs")
    # 2. The lock, then the input package: the code of the input is linked next to the package `pynix_comps`.
    nix_basix = ["--no-warn-dirty", "--override-input", "basix", f"path:{ev.flake}"]
    locked = run(f"{label}-lock", ["nix", "flake", "lock", *nix_basix], folder)
    report(locked.returncode == 0, f"{shown}: the lock holds the input" + failure(locked))
    if locked.returncode != 0:
        return
    link = folder / ".basix" / "pynix"
    link.parent.mkdir(exist_ok=True)
    built = run(
        f"{label}-package",
        ["nix", "build", "--out-link", str(link), "--print-out-paths", *nix_basix, f"path:{folder}#input-packages"],
        ev.root,
    )
    report(built.returncode == 0, f"{shown}: the input package builds" + failure(built))
    if built.returncode != 0:
        return
    names = sorted(p.name for p in link.iterdir())
    report(names == ["comps", "pynix_comps"], f"{shown}: the package holds comps and pynix_comps (got {names!r})")
    # 3. The compile imports the code. The code is in the store, so `reads` holds no file for it.
    problem = compiler.compile(folder, label, enabled=True)
    report(
        problem is None,
        f"{shown}: the entry compiles with a value from the code of the input" + (f" ({problem})" if problem else ""),
    )
    if problem:
        return
    data = json.loads((folder / "system.json").read_text())
    report(
        data["reads"]["files"] == {}
        and [i.get("python") for i in data["inputs"]] == ["src"]
        and list(data.get("input_packages", {})) == ["comps"],
        f"{shown}: system.json records the python folder and the package, and reads no file",
    )
    # 4. An error in the code names its file and the line of the entry.
    failing = WORK / "cases" / f"{label}-error"
    if failing.exists():
        shutil.rmtree(failing)
    shutil.copytree(folder, failing, symlinks=True)
    (failing / "system.py").write_text(PYTHON_INPUT_ERROR_ENTRY % entry)
    wrong = compiler.compile(failing, f"{label}-error", enabled=True)
    ok_error = wrong is not None and wrong.strip().endswith(
        "comps/__init__.py:11: ValueError: comps broke (from system.py:11)"
    )
    report(
        ok_error,
        f"{shown}: an error in the code names comps/__init__.py" + ("" if ok_error else f" (got {wrong!r})"),
    )
    # 5. The toplevel holds the value from the code.
    zone = run(
        f"{label}-zone",
        ["nix", "eval", "--raw", *nix_args(ev.flake, folder, "nixosConfigurations.trybox.config.time.timeZone")],
        ev.root,
    )
    report(
        zone.returncode == 0 and zone.stdout == "Europe/Oslo",
        f"{shown}: the machine has the value from the code" + ("" if zone.returncode == 0 else failure(zone)),
    )
    # 6. A moved input: the lock moves, the compiled file does not, so the evaluation refuses.
    with (source / "src" / "comps" / "__init__.py").open("a") as handle:
        handle.write("# a new line moves the narHash of the input\n")
    moved = run(f"{label}-update", ["nix", "flake", "update", "comps", *nix_basix], folder)
    if moved.returncode != 0:
        report(False, f"{shown}: the lock moves" + failure(moved))
        return
    done = ev.drv(f"{label}-moved", folder)
    wanted = "the package `pynix_comps` was made from another version of `comps` than flake.lock holds"
    report(done.returncode != 0 and wanted in error_text(done), f"{shown}: a moved input fails the evaluation")
    if wanted not in error_text(done):
        emit(error_text(done)[-1000:])


#: A nixpkgs commit that is not the one of Basix: its `hello` differs from the `hello` of the Basix nixpkgs. The sources
#: are in the Nix store of a machine that ran the P8 examples. The commit and hash are those of the SG44 spike.
OLD_COMMIT = "d233902339c02a9c334e7e593de68855ad26c4cb"
OLD_HASH = "sha256-30sZNZoA1cqF5JNO9fVX+wgiQYjB7HJqqJ4ztCDeBZE="

#: An entry with a second, named nixpkgs. `old.pkgs.hello` is the `hello` of that commit (009-D53, 009-D54, PF-2).
NAMED_NIXPKGS_ENTRY = """\
from basix_config import *

old = NixpkgsPin("old", "%(commit)s")

machine = Machine(
    name="trybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-vd-target",
    programs=[Program(old.pkgs.hello)],
)
"""


def named_nixpkgs_pynix(ev: Evaluator) -> tuple[bool, Path | None]:
    """Whether a pynix can serve a named nixpkgs, and the folder that replaces the pin of the flake (or None).

    `BASIX_PYNIX_PATH=FOLDER` names a pynix source folder for the check (a pynix that no release holds yet). With no
    variable, the pin of the flake must hold `nixpkgsInputs` in `nix/input-packages.nix` and the class `NixpkgsPin`.
    """
    chosen = os.environ.get("BASIX_PYNIX_PATH")
    if chosen:
        return True, Path(chosen)
    got = run(
        "named-nixpkgs-pynix",
        [
            "nix",
            "eval",
            "--impure",
            "--raw",
            "--no-write-lock-file",
            "--expr",
            f'(builtins.getFlake "path:{ev.flake}").inputs.pynix.outPath',
        ],
        ev.root,
    )
    if got.returncode != 0:
        return False, None
    source = Path(got.stdout.strip())
    text = (source / "nix" / "input-packages.nix").read_text()
    return "nixpkgsInputs" in text and "class NixpkgsPin" in (source / "src/pynix_native/nixpkgs.py").read_text(), None


def check_named_nixpkgs(ev: Evaluator, stages: Stages, compiler: Compiler) -> None:
    """A second, named nixpkgs end to end, as `basix apply` runs it: the stub, the lock, the package list, the compile,
    and the toplevel with a package of the pinned commit (PF-2)."""
    tools = compiler_python(compiler)
    if tools is None:
        emit("SKIP named nixpkgs: the compiler is not the package of the flake, so the stub writer is not in reach")
        return
    ready, override = named_nixpkgs_pynix(ev)
    if not ready:
        emit(
            "SKIP named nixpkgs: the pynix pin of the flake predates `NixpkgsPin`; re-pin pynix, or set BASIX_PYNIX_PATH"
        )
        return
    python, library = tools
    pin = ["--override-input", "basix/pynix", f"path:{override}"] if override else []
    folder = stages.make("core", "named-nixpkgs")
    (folder / "system.py").write_text(NAMED_NIXPKGS_ENTRY % {"commit": OLD_COMMIT})
    (folder / "system.json").unlink()
    env = {**clean_env(), "PYTHONPATH": str(library)}
    done = child([str(python), "-P", "-c", STUB_CODE, "system.py"], folder, env)
    (LOGS / "named-nixpkgs-stub.err").write_text(done.stderr)
    if done.returncode != 0:
        report(False, f"named nixpkgs: the stub writer runs ({done.stderr[-500:]})")
        return
    (folder / "flake.nix").write_text(done.stdout)
    report(
        f'inputs.nixpkgs-old.url = "github:NixOS/nixpkgs/{OLD_COMMIT}";' in done.stdout
        and "input-packages" in done.stdout,
        "named nixpkgs: the stub holds the input `nixpkgs-old` and `input-packages`",
    )
    locked = run(
        "named-nixpkgs-lock",
        ["nix", "flake", "lock", "--no-warn-dirty", "--override-input", "basix", f"path:{ev.flake}"],
        folder,
    )
    report(
        locked.returncode == 0,
        f"named nixpkgs: the lock holds the pin ({error_text(locked)[-300:] if locked.returncode else 'ok'})",
    )
    if locked.returncode != 0:
        return
    link = folder / ".basix" / "pynix"
    link.parent.mkdir(exist_ok=True)
    built = run(
        "named-nixpkgs-package",
        [
            "nix",
            "build",
            "--out-link",
            str(link),
            "--print-out-paths",
            "--no-warn-dirty",
            "--override-input",
            "basix",
            f"path:{ev.flake}",
            *pin,
            f"path:{folder}#input-packages",
        ],
        ev.root,
    )
    report(
        built.returncode == 0,
        f"named nixpkgs: the package list builds ({built.stdout.strip() or error_text(built)[-400:]!r})",
    )
    if built.returncode != 0:
        return
    listing = link / "pynix_nixpkgs_old" / "names.json"
    names = json.loads(listing.read_text()) if listing.is_file() else {}
    report(
        sorted(p.name for p in link.iterdir()) == ["pynix_nixpkgs_old"] and names.get("hello") == ["hello", 1],
        f"named nixpkgs: the package holds pynix_nixpkgs_old with the name `hello` (got {sorted(p.name for p in link.iterdir())!r})",
    )
    problem = compiler.compile(folder, "named-nixpkgs", enabled=True)
    report(problem is None, f"named nixpkgs: the entry compiles with `old.pkgs.hello` ({problem})")
    if problem:
        return
    attr = "nixosConfigurations.trybox.config.environment.systemPackages"
    apply = (
        '(ps: builtins.concatStringsSep " " (map (p: p.drvPath) (builtins.filter (p: (p.pname or "") == "hello") ps)))'
    )
    got = run(
        "named-nixpkgs-hello",
        [
            "nix",
            "eval",
            "--raw",
            *nix_args(ev.flake, folder, attr)[:-1],
            *pin,
            f"path:{folder}#{attr}",
            "--apply",
            apply,
        ],
        ev.root,
    )
    want = run(
        "named-nixpkgs-hello-old",
        [
            "nix",
            "eval",
            "--raw",
            "--impure",
            "--expr",
            f'(import (builtins.fetchTree {{ type = "github"; owner = "NixOS"; repo = "nixpkgs"; rev = "{OLD_COMMIT}"; '
            f'narHash = "{OLD_HASH}"; }}) {{ system = "x86_64-linux"; }}).hello.drvPath',
        ],
        ev.root,
    )
    report(
        got.returncode == 0 and want.returncode == 0 and got.stdout.strip() == want.stdout.strip() != "",
        f"named nixpkgs: the machine holds the `hello` of the pinned commit (got {got.stdout.strip() or error_text(got)[-300:]!r},"
        f" want {want.stdout.strip() or error_text(want)[-300:]!r})",
    )


#: The driver of `basix explain` for the check. It runs the command on the fixture folder, with the input `basix`
#: replaced by the staged flake (`--override-input`), as `nix_args` does for the other cases.
EXPLAIN_CODE = """\
import sys
from pathlib import Path

from pynix_engine import SubprocessRunner

from basix.explain import explain

flake, folder, name = sys.argv[1:4]


class Runner(SubprocessRunner):
    def run(self, argv, *, cwd=None, on_line=None):
        argv = list(argv)
        at = argv.index("eval") + 1
        argv[at:at] = ["--no-write-lock-file", "--override-input", "basix", "path:" + flake]
        argv = [("path:" + folder + "#" + a[2:]) if a.startswith(".#") else a for a in argv]
        return super().run(argv, cwd=cwd)


sys.exit(explain(Path(folder), name, runner=Runner()))
"""


def check_explain(ev: Evaluator, stages: Stages, compiler: Compiler) -> None:
    """`basix explain` on a real evaluation (009-D14): the value, and the learner line that sets it."""
    tools = compiler_python(compiler)
    if tools is None:
        emit("SKIP explain: the compiler is not the package of the flake, so the command is not in reach")
        return
    python, library = tools
    got = run(
        "pynix-source",
        [
            "nix",
            "eval",
            "--impure",
            "--raw",
            "--no-write-lock-file",
            "--expr",
            f'(builtins.getFlake "path:{ev.flake}").inputs.pynix.outPath',
        ],
        ev.root,
    )
    if got.returncode != 0:
        report(False, f"explain: the pynix source of the flake is found ({error_text(got)[-300:]})")
        return
    # The compiled library comes first. `pynix_engine` comes from the `pynix` source, and `basix` from the flake.
    path = os.pathsep.join([str(library), f"{got.stdout.strip()}/src", str(ev.flake / "src")])
    folder = stages.make("trusted", "explain")
    entry = (folder / "system.py").read_text().splitlines()
    site = next(i for i, line in enumerate(entry, 1) if line.startswith("machine = Machine("))
    cases = [
        (
            "trusted_interfaces",
            0,
            [
                'networking.firewall.trustedInterfaces = ["enp1s0", "lo"]',
                f"set by trusted_interfaces at system.py:{site}",
            ],
        ),
        ("user", 0, ['basix.user = "learner"', f"set by user at system.py:{site}"]),
        (
            "RemoteLogin",
            0,
            [
                "services.openssh.enable = true",
                "set by RemoteLogin at system.py:",
                "networking.firewall.interfaces.enp1s0.allowedTCPPorts = [22]",
            ],
        ),
        ("services.openssh.ports", 0, ["services.openssh.ports = [22]", "set by NixOS"]),
        ("services.openssh.enabel", 1, ["is not an option of this machine. Did you mean `services.openssh.enable`?"]),
        ("trusted_interface", 1, ["Did you mean `trusted_interfaces`?"]),
    ]

    def one(case: tuple[str, int, list[str]]) -> subprocess.CompletedProcess[str]:
        env = {**clean_env(), "PYTHONPATH": path}
        argv = [str(python), "-P", "-c", EXPLAIN_CODE, str(ev.flake), str(folder), case[0]]
        done = child(argv, folder, env, 900)
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"explain-{case[0]}")
        (LOGS / f"{stem}.out").write_text(done.stdout)
        (LOGS / f"{stem}.err").write_text(done.stderr)
        return done

    futures = [go(lambda case=case: one(case)) for case in cases]
    results = [future.result() for future in futures]
    for (name, code, wanted), done in zip(cases, results, strict=True):
        text = ANSI.sub("", done.stdout + done.stderr)
        ok = done.returncode == code and all(line in text for line in wanted)
        report(ok, f"explain {name}: exit {code}, and the output holds {wanted!r}")
        if not ok:
            emit(f"exit {done.returncode}\n{text[-1500:]}")
    text = ANSI.sub("", results[2].stdout)
    for word in ("priority", "mkDefault", "mkForce", "override"):
        report(word not in text.lower(), f"explain RemoteLogin: the output has no priority word ({word})")


def check_conflict(ev: Evaluator, stages: Stages) -> None:
    """Two items of `options=[...]` give one option two values. NixOS names both lines of `system.py` (BSX-385)."""
    stage = stages.make("conflict", "conflict")
    lines = (stage / "system.py").read_text().splitlines()
    first = next(i for i, line in enumerate(lines, 1) if "America/New_York" in line)
    done = ev.drv("conflict-drv", stage)
    text = error_text(done)
    wanted = [f"system.py:{first} (Machine)", f"system.py:{first + 1} (Machine)"]
    ok = done.returncode != 0 and "time.timeZone" in text and all(w in text for w in wanted)
    report(ok, f"conflict: evaluation fails, and the error names the lines of both items ({', '.join(wanted)})")
    if not ok:
        emit(text[-1500:])


def native_desktop(values: dict[str, str]) -> str:
    text = DESKTOP_NATIVE
    for marker, value in values.items():
        text = text.replace(marker, value)
    return text


def check_desktop(ev: Evaluator, stages: Stages, compiler: Compiler) -> None:
    """The profile `Desktop()` (L66, L67, L42, 009-D30, S18 equality, P6)."""
    parts(
        lambda: desktop_plain(ev, stages),
        lambda: desktop_full(ev, stages),
        lambda: desktop_other(ev, stages, compiler),
    )


def desktop_plain(ev: Evaluator, stages: Stages) -> None:
    plain = stages.make("desktop", "desktop")
    drv_job = go(lambda: ev.drv("desktop-drv", plain))
    facts_job = go(lambda: ev.facts_run("desktop-facts", plain))
    # S18 equality: the component builds what the hand-written Nix builds. The base is the same machine.
    native = stages.make("core", "desktop-native", module=native_desktop(DESKTOP_PLAIN), stub=DESKTOP_STUB)
    native_job = go(lambda: ev.drv("desktop-native-drv", native))
    done = drv_job.result()
    report(
        done.returncode == 0,
        f"desktop: the machine evaluates (drvPath {done.stdout.strip() or error_text(done)[-300:]})",
    )
    facts = ev.facts_read("desktop-facts", facts_job.result())
    if facts is not None:
        packages = facts["packages"]
        expect_equal(
            facts,
            "desktop",
            {
                "hyprland": True,
                "noctalia": True,
                "greetd": True,
                "greeterAutologin": False,
                "defaultLocale": "C.UTF-8",
                "imperativeLocale": True,  # 010-D24: with neither field set, the core keeps it
                "consoleKeyMap": "us",
                "linger": True,
                "sshd": False,
            },
        )
        for program in ("foot", "firefox", "basix-session", "git", "networkmanager"):
            report(program in packages, f"desktop: {program} is in the system packages")
        report("htop" not in packages, "desktop: no program that the machine does not list")
        report(
            facts["etcHyprland"] is not None
            and facts["etcHyprland"].startswith("-- The default Hyprland config of Basix."),
            "desktop: /etc/xdg/hypr/hyprland.lua is the Basix default (009-D30)",
        )
        report(
            facts["etcNoctalia"] is not None and facts["etcNoctalia"].startswith("# Basix defaults."),
            "desktop: /etc/xdg/noctalia/00-basix.toml is the Basix default",
        )
        report(
            facts["userTmpfiles"]
            == [
                "d %h/.config/noctalia 0755 - - -",
                "L %h/.config/noctalia/00-basix.toml - - - - /etc/xdg/noctalia/00-basix.toml",
            ],
            "desktop: a user rule links the Noctalia default into the home",
        )
        command = facts["greeterCommand"] or ""
        _program, _, session = command.partition(" --cmd ")
        report(
            session == "basix-session" and "/nix/store" not in session,
            f"desktop: the login screen runs the wrapper `basix-session` and shows no store path (L67) (got {session!r})",
        )
        report(
            command.count("/nix/store/") == 1
            and command.startswith("/nix/store/")
            and "/bin/tuigreet --time --remember" in command,
            "desktop: the only store path of the greeter command is the program tuigreet itself",
        )
    done_native = native_job.result()
    same = done.returncode == 0 and done_native.returncode == 0 and done.stdout.strip() == done_native.stdout.strip()
    report(
        same,
        f"desktop equality: Desktop() equals the hand-written module (python {done.stdout.strip()}, nix {done_native.stdout.strip() or error_text(done_native)[-300:]})",
    )


def desktop_full(ev: Evaluator, stages: Stages) -> None:
    full = stages.make("desktop-full", "desktop-full")
    drv_job = go(lambda: ev.drv("desktop-full-drv", full))
    facts_job = go(lambda: ev.facts_run("desktop-full-facts", full))
    native = stages.make("core", "desktop-full-native", module=native_desktop(DESKTOP_FULL), stub=DESKTOP_STUB)
    native_job = go(lambda: ev.drv("desktop-full-native-drv", native))
    done_full = drv_job.result()
    report(
        done_full.returncode == 0,
        f"desktop-full: the machine evaluates (drvPath {done_full.stdout.strip() or error_text(done_full)[-300:]})",
    )
    facts = ev.facts_read("desktop-full-facts", facts_job.result())
    if facts is not None:
        expect_equal(
            facts, "desktop-full", {"defaultLocale": "en_US.UTF-8", "consoleKeyMap": "de", "imperativeLocale": False}
        )  # 010-D24
        report(
            "htop" in facts["packages"] and "firefox" in facts["packages"],
            "desktop-full: the extra program joins the system packages",
        )
        report(
            (facts["greeterCommand"] or "").endswith(
                "--remember --greeting 'Welcome to Basix, Ada!' --cmd basix-session"
            ),
            f"desktop-full: the greeting is on the login command (got {facts['greeterCommand']!r})",
        )
    done_native = native_job.result()
    same = (
        done_full.returncode == 0
        and done_native.returncode == 0
        and done_full.stdout.strip() == done_native.stdout.strip()
    )
    report(
        same,
        f"desktop-full equality: Desktop(...) equals the hand-written module (python {done_full.stdout.strip()}, nix {done_native.stdout.strip() or error_text(done_native)[-300:]})",
    )


def desktop_other(ev: Evaluator, stages: Stages, compiler: Compiler) -> None:
    # L42: a native module at normal priority wins over each `Default(...)` value and over the default files.
    over = stages.make("desktop-full", "desktop-override", module=DESKTOP_OVERRIDE)
    over_job = go(lambda: ev.facts_run("desktop-override-facts", over))

    # `browser=False` leaves Firefox out.
    def no_browser(folder: Path) -> None:
        (folder / "system.py").write_text(
            (folder / "system.py").read_text().replace("Desktop()", "Desktop(browser=False)")
        )
        problem = compiler.compile(folder, "desktop-nobrowser", enabled=True)
        if problem:
            raise RuntimeError(f"compile of desktop-nobrowser failed: {problem}")

    browser_job = None
    if compiler.mode == "compile":
        browser_job = go(
            lambda: ev.facts_run(
                "desktop-nobrowser-facts", stages.make("desktop", "desktop-nobrowser", edit=no_browser)
            )
        )

    # The machine of the VM check (`scripts/desktop_vm.py`) evaluates.
    def vm_files(folder: Path) -> None:
        shutil.copy2(ev.root / "fixtures" / "vmdesk.nix", folder / "vmdesk.nix")

    vm_job = go(
        lambda: ev.drv(
            "desktop-vm-drv",
            stages.make("desktop", "desktop-vm", stub=VM_STUB, edit=vm_files),
            "trybox.config.system.build.vm",
        )
    )
    facts = ev.facts_read("desktop-override-facts", over_job.result())
    if facts is not None:
        expect_equal(
            facts,
            "desktop-override",
            {
                "defaultLocale": "de_DE.UTF-8",
                "consoleKeyMap": "us",
                "greeterCommand": "agreety --cmd bash",
                "etcHyprland": "-- my own default",
                "etcNoctalia": "# my own default",
            },
        )
    if browser_job is not None:
        facts = ev.facts_read("desktop-nobrowser-facts", browser_job.result())
        if facts is not None:
            report(
                "firefox" not in facts["packages"] and "foot" in facts["packages"],
                "desktop: browser=False leaves Firefox out",
            )
    else:
        emit("SKIP desktop browser=False: the stored system.json does not compile")
    done = vm_job.result()
    report(
        done.returncode == 0,
        f"desktop: the VM of the desktop check evaluates (drvPath {done.stdout.strip() or error_text(done)[-300:]})",
    )


def check_script_order(ev: Evaluator, stages: Stages) -> None:
    stage = stages.make("core", "two-disks", module=TWO_DISKS)
    argv = [
        "nix",
        "build",
        "--no-link",
        "--print-out-paths",
        *nix_args(ev.flake, stage, "nixosConfigurations.trybox.config.system.build.diskoScript"),
    ]
    done = run("two-disks-build", argv, ev.root, timeout=1800)
    if done.returncode != 0:
        report(False, "two-disks: the disk script builds")
        emit(error_text(done)[-1500:])
        return
    script = Path(done.stdout.strip().splitlines()[-1])
    lines = script.read_text().splitlines()
    guard = next((i for i, line in enumerate(lines, 1) if "basix-disk-guard all" in line), None)
    write = next((i for i, line in enumerate(lines, 1) if "sgdisk --clear" in line), None)
    report(
        guard is not None and write is not None and guard < write,
        f"two-disks: the all-disk guard call (line {guard}) comes before the first sgdisk --clear (line {write})",
    )
    call = lines[guard - 1] if guard else ""
    report(
        "virtio-vd-extra" in call and "virtio-vd-target" in call,
        "two-disks: the all-disk guard call names both disks",
    )
    loops = [line for line in lines if line.startswith("for dev in")]
    report(
        bool(loops) and all(re.fullmatch(r"for dev in\s*;", line.rstrip()) for line in loops),
        "two-disks: the disk-deactivate loop has no target, so no disk is deactivated before the guard",
    )
    hooks = [i for i, line in enumerate(lines, 1) if "basix-disk-guard all" in line]
    report(len(hooks) >= 2, f"two-disks: every disk repeats the guard in its hook (lines {hooks})")


def copy_source_files(root: Path, target: Path) -> None:
    """Copy the files that the source trees read, and no stray file that the checkout holds."""
    ignore = shutil.ignore_patterns(
        "__pycache__", "*.pyc", ".devenv", ".direnv", ".venv", ".pytest_cache", ".ruff_cache"
    )
    for name in SOURCE_TREE_FILES:
        source = root / name
        if source.is_dir():
            shutil.copytree(source, target / name, symlinks=True, ignore=ignore)
        elif source.is_file():
            shutil.copy2(source, target / name)


def source_trees(name: str, folder: Path) -> dict[str, str]:
    """Return the store path of each source tree of the flake in `folder`."""
    done = run(
        name,
        ["nix", "eval", "--json", "--no-write-lock-file", f"path:{folder}#lib.sourceTrees"],
        folder,
    )
    if done.returncode != 0:
        raise RuntimeError(f"source trees ({name}): nix eval failed: {error_text(done)[-500:]}")
    return json.loads(done.stdout)


def check_source_trees(root: Path) -> None:
    """A checkout with stray files gives the same source trees as a clean one, and the trees hold no stray file."""
    base = WORK / "source-trees"
    if base.exists():
        shutil.rmtree(base)
    clean, stray = base / "clean", base / "stray"
    for folder in (clean, stray):
        folder.mkdir(parents=True)
        copy_source_files(root, folder)
    for relative, text in STRAY_FILES.items():
        path = stray / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if text is None:
            path.symlink_to("/nix/store/does-not-exist")
        else:
            path.write_text(text)
    planted = sum(1 for relative in STRAY_FILES if (stray / relative).is_symlink() or (stray / relative).is_file())
    want = {"iso", "library", "project"}
    first = go(lambda: source_trees("source-trees-clean", clean))
    second = go(lambda: source_trees("source-trees-stray", stray))
    clean_trees, stray_trees = first.result(), second.result()
    report(set(clean_trees) == want == set(stray_trees), f"source trees: the flake gives the trees {sorted(want)}")
    for tree in sorted(want & set(clean_trees) & set(stray_trees)):
        report(
            clean_trees[tree] == stray_trees[tree],
            f"source trees: {tree}: a checkout with {planted} stray files gives the store path of the clean checkout "
            f"({stray_trees[tree]})",
        )
        folder = Path(stray_trees[tree])
        found = [
            str(path.relative_to(folder))
            for path in folder.rglob("*")
            if path.name in ("__pycache__", ".pytest_cache", ".ruff_cache", ".venv", "result", ".DS_Store")
            or path.suffix in (".pyc", ".swp")
            or path.name.endswith("~")
        ]
        report(not found, f"source trees: {tree}: no stray file in the tree ({found[:3]})")
        report((folder / "src/basix/cli.py").is_file(), f"source trees: {tree}: the tree holds src/basix/cli.py")


def check_owner_workspaces(ev: Evaluator) -> None:
    """Optional: workspaces of the owner evaluate as they are.

    `BASIX_OWNER_WORKSPACES` is a list of `FOLDER=MACHINE` entries separated by `:`. The machines EX1 and EX2 of SG31 are
    private, so the repository does not hold them. Each folder holds a stub that calls `basix.lib.machine`. The check
    evaluates the drvPath of `nixosConfigurations.MACHINE` and expects it to succeed with no line removed.
    """
    for entry in filter(None, os.environ.get("BASIX_OWNER_WORKSPACES", "").split(":")):
        folder, _, machine = entry.partition("=")
        stage = Path(folder)
        done = ev.drv(f"owner-{stage.name}", stage, f"{machine}.config.system.build.toplevel")
        report(
            done.returncode == 0,
            f"owner workspace {stage.name}: evaluates with no line removed ({done.stdout.strip() or error_text(done)[-300:]})",
        )


def run_step(step: Callable[[], None]) -> tuple[list[str], BaseException | None]:
    """Run a step in this thread. Return its lines, and the error that stopped it."""
    STEP.lines = []
    error = None
    try:
        step()
    except RuntimeError as caught:  # a compile that failed
        report(False, str(caught))
    except BaseException as caught:
        error = caught
    lines, STEP.lines = STEP.lines, None
    return lines, error


def job_count() -> int:
    """The number of jobs: `BASIX_EVAL_JOBS`, or the CPU count."""
    named = os.environ.get("BASIX_EVAL_JOBS", "")
    return int(named) if named else max(1, os.cpu_count() or 1)


def main(argv: list[str]) -> int:
    global LOGS, WORK, SLOTS
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    try:
        jobs = job_count()
        if jobs < 1:
            raise ValueError(jobs)
    except ValueError:
        print(f"BASIX_EVAL_JOBS must be a whole number of 1 or more, not {os.environ['BASIX_EVAL_JOBS']!r}")
        return 2
    SLOTS = threading.BoundedSemaphore(jobs)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    default_logs = Path.home() / ".local/state/basix/p2/logs/check_eval" / stamp
    LOGS = Path(os.environ.get("BASIX_CHECK_LOGS", str(default_logs)))
    LOGS.mkdir(parents=True, exist_ok=True)
    WORK = Path(os.environ.get("BASIX_CHECK_WORK") or workdir.default_work("p2/check_eval"))
    WORK.mkdir(parents=True, exist_ok=True)
    print(f"raw logs: {LOGS}")
    print(f"work: {WORK}")

    flake = stage_flake(root)
    compiler = Compiler(root, flake)
    print(f"compile: {compiler.mode} ({compiler.note})")
    stages = Stages(root, flake, compiler)
    ev = Evaluator(root, flake)

    check_guard_file(root)
    steps: dict[str, Callable[[], None]] = {
        "core": lambda: check_core(ev, stages),
        "net": lambda: check_net(ev, stages),
        "variants": lambda: check_variants(ev, stages),
        "refusals": lambda: check_refusals(ev, stages),
        "conflict": lambda: check_conflict(ev, stages),
        "guards": lambda: check_guards(ev, stages),
        "anchor": lambda: check_anchor(ev, stages),
        "command": lambda: check_command(ev, stages),
        "stub": lambda: check_stub(ev, stages, compiler),
        "typed-input": lambda: check_typed_input(ev, stages, compiler),
        "python-input": lambda: check_python_input(ev, stages, compiler),
        "named-nixpkgs": lambda: check_named_nixpkgs(ev, stages, compiler),
        "explain": lambda: check_explain(ev, stages, compiler),
        "desktop": lambda: check_desktop(ev, stages, compiler),
        "script-order": lambda: check_script_order(ev, stages),
        "source-trees": lambda: check_source_trees(root),
        "owner": lambda: check_owner_workspaces(ev),
    }
    # `BASIX_CHECK_STEPS=typed-input,stub` runs only these steps (a quick loop while you work on one of them).
    only = [name for name in os.environ.get("BASIX_CHECK_STEPS", "").split(",") if name]
    for name in only:
        if name not in steps:
            print(f"unknown step {name!r}; the steps are: {', '.join(steps)}")
            return 2
    chosen = {name: step for name, step in steps.items() if not only or name in only}
    if not only:
        stages.prefetch(FIXTURES)
    # Start the slow steps first. Print the lines in the order of `steps`, so the output stays the same.
    order = sorted(chosen, key=lambda name: SLOW_FIRST.index(name) if name in SLOW_FIRST else len(SLOW_FIRST))
    with ThreadPoolExecutor(max_workers=len(chosen) or 1, thread_name_prefix="step") as pool:
        futures = {name: pool.submit(run_step, chosen[name]) for name in order}
        for name in chosen:
            lines, error = futures[name].result()
            for line in lines:
                print(line, flush=True)
            if error is not None:
                raise error
    failed = [text for ok, text in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
