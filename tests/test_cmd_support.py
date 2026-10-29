"""Shared set-up for the command tests: a fake machine and a workspace with real git.

Git runs for real in a temporary folder, with its own identity. Nix, the compiler, `sudo`, and `systemctl` are fakes:
the `World` answers them and keeps the state that a real machine would keep (the running system, the system profile,
the sudo ticket).
"""

import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pynix_engine import Result
from pynix_engine.testing import FakeClock, FakeRunner

from basix.commands import Context
from basix.layer import HEALTH_COMMAND, NIX_FEATURES, SUDO, Host
from basix_config.stub import DEFAULT_BASIX_URL, make_stub

NIX = ("nix", *NIX_FEATURES)
GOOD_UNITS = "web.service loaded active running Web\n"
BAD_UNITS = GOOD_UNITS + "api.service loaded failed failed Api\n"
LOOPING_UNITS = "api.service loaded activating auto-restart Api\n"
COMPILE_ERROR = "system.py:6:40: Machine.user must be an account name, found 42."
NIXPKGS_REV = "e7439b6b14ad3cc35d05608ebca9bce01a25f5f8"
ENTRY = 'from basix_config import *\n\nmachine = Machine(name="pybox", user="learner")\n'
CACHE = ("https://example.cachix.org", "example.cachix.org-1:/pLx1UaHxGgO260wU3pivrrIWjQAtqFbGINHFhfKg3Y=")
DRV = "/nix/store/0123456789abcdfghijklmnpqrsvwxyz-nixos-system-pybox.drv"
NOW = 1_792_000_000.0
DAY = 86400


def git_env(config: Path | None = None) -> dict[str, str]:
    """The environment of every real git call: no outer setting (a persona, a global file) reaches the test."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=str(config or os.devnull), GIT_CONFIG_NOSYSTEM="1")
    return env


class HybridRunner:
    """Send `git` to a real process and every other command to the fake runner."""

    def __init__(self, fake: FakeRunner, env: dict[str, str]) -> None:
        self.fake = fake
        self.env = env

    def run(self, argv: Sequence[str], *, cwd: Path | None = None, on_line: Callable[[str], None] | None = None):
        if tuple(argv[:1]) != ("git",):
            return self.fake.run(argv, cwd=cwd, on_line=on_line)
        done = subprocess.run(list(argv), cwd=cwd, env=self.env, capture_output=True, text=True)
        out = done.stdout + done.stderr
        if on_line:
            for line in out.splitlines():
                on_line(line)
        return Result(done.returncode, out)


def locked_of(expr: str) -> dict:
    """The `locked` attributes inside the expression that `basix update` gives to `nix eval` (`release.source_expr`)."""
    found = re.search(r"fromJSON (\".*\")\)\)\.outPath$", expr)
    assert found, expr
    return json.loads(json.loads(found[1]))


def lock_json(
    nar_hash: str = "sha256-AAAA",
    rev: str = "a" * 40,
    ref: str = "release",
    inputs=(),
    pynix_tag: str = "pynix-v0.1.0-rc.2",
    pynix_rev: str = "p" * 40,
    modified: int = int(NOW) - 13 * DAY,
) -> str:
    """A `flake.lock` of a Basix workspace: `basix` with its `nixpkgs` and `pynix`, and the named extra inputs."""
    nodes: dict[str, object] = {
        "root": {"inputs": {"basix": "basix", **{name: name for name in inputs}}},
        "basix": {
            "inputs": {"nixpkgs": "nixpkgs", "pynix": "pynix"},
            "locked": {"type": "git", "rev": rev, "narHash": nar_hash},
            "original": {"type": "git", "url": DEFAULT_BASIX_URL, "ref": ref},
        },
        "nixpkgs": {
            "locked": {"type": "github", "rev": NIXPKGS_REV, "narHash": "sha256-NIXPKGS", "lastModified": modified}
        },
        "pynix": {
            "locked": {"type": "git", "rev": pynix_rev, "narHash": "sha256-PYNIX", "ref": f"refs/tags/{pynix_tag}"},
            "original": {"type": "git", "ref": f"refs/tags/{pynix_tag}"},
        },
    }
    for name in inputs:
        nodes[name] = {
            "inputs": {"nixpkgs": ["basix", "nixpkgs"]},
            "locked": {"type": "github", "rev": "b" * 40, "narHash": f"sha256-{name}"},
        }
    return json.dumps({"nodes": nodes, "root": "root", "version": 7})


CHANGELOG = """\
# Changelog

## pynix-v0.1.0-rc.3

```text
pynix 0.1.0-rc.3 (nixpkgs aaaaaaa, 2026-10-15; Home Manager fae6e9e; flake-parts 024633c)
NixOS options: 21699 -> 21701. Added 2, removed 0, type changed 1, renamed 1.
NixOS:
 Renamed (also counted in added and removed):
  services.openssh.ports -> services.openssh.listenPorts
  programs.zsh.enable -> programs.zsh.enabled
 Type changed:
  services.openssh.settings.PermitRootLogin: bool -> enum
  boot.loader.timeout: int -> str
```

## pynix-v0.1.0-rc.2

```text
pynix 0.1.0-rc.2 (nixpkgs e7439b6, 2026-10-08)
NixOS options: 21699 -> 21699. Renamed 0.
 Renamed: services.openssh.oldname -> services.openssh.newname
```
"""


@dataclass
class World:
    """The fake machine: the system profile, the health answer, the compiler, sudo, and the answers of Nix."""

    tmp: Path
    ws: Path
    units: str = GOOD_UNITS
    analyze: str | None = None  # the output of `systemd-analyze verify`; None: the built systems hold no units
    ticket: bool = False  # a valid sudo ticket
    password_ok: bool = True
    compile_error: str | None = None
    caches: list[tuple[str, str]] = field(default_factory=list)  # the caches that the compiler writes
    conf: str = "substituters = https://cache.nixos.org/\n"  # the nix.conf of the running system
    build_error: str | None = None
    eval_error: str | None = None
    root_build_code: int = 0
    test_code: int = 0  # the exit code of `switch-to-configuration test` for the new system
    boot_code: int = 0
    health_code: int = 0
    lock_code: int = 0
    update_to: str | None = None  # the lock text that `nix flake update` writes
    source_version: str = "0.1.0"
    new_version: str = "0.1.1"
    next_system: str = "sys-1"
    input_packages_code: int = 0
    generations: int = 0
    switches: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unauthorized: list[tuple[str, ...]] = field(default_factory=list)
    updated: bool = False
    provenance: dict[str, list[str]] | None = None

    @property
    def host(self) -> Host:
        return Host(
            current=self.tmp / "run" / "current-system",
            profile=self.tmp / "profiles" / "system",
            nix_conf=self.tmp / "etc" / "nix.conf",
        )

    def store(self, name: str) -> Path:
        path = self.tmp / "store" / name
        (path / "bin").mkdir(parents=True, exist_ok=True)
        (path / "bin" / "switch-to-configuration").write_text("#!/bin/sh\n")
        if self.analyze is not None:
            target = path / "etc" / "systemd" / "system" / "default.target"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("[Unit]\n")
        return path

    def run_system(self, system: Path) -> None:
        """What `switch-to-configuration test` does: the system runs."""
        link = self.host.current
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(system)

    def set_profile(self, system: Path) -> None:
        """What `nix-env --set` does: a new generation, and the profile points at it."""
        profile = self.host.profile
        profile.parent.mkdir(parents=True, exist_ok=True)
        self.generations += 1
        (profile.parent / f"system-{self.generations}-link").symlink_to(system)
        profile.unlink(missing_ok=True)
        profile.symlink_to(f"system-{self.generations}-link")

    def install(self, name: str) -> Path:
        """Put a system in the state of an installed machine: it runs, and it is the boot default."""
        system = self.store(name)
        self.run_system(system)
        self.set_profile(system)
        return system

    def running(self) -> str | None:
        return os.path.realpath(self.host.current) if os.path.lexists(self.host.current) else None

    def boot_default(self) -> str | None:
        return os.path.realpath(self.host.profile) if os.path.lexists(self.host.profile) else None


@dataclass
class Setup:
    world: World
    fake: FakeRunner
    runner: HybridRunner
    clock: FakeClock
    lines: list[str]
    home: Path
    ctx: Context

    @property
    def ws(self) -> Path:
        return self.home / "basix"

    def git(self, *args: str) -> str:
        done = subprocess.run(["git", *args], cwd=self.ws, env=self.runner.env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        return done.stdout

    def commits(self) -> list[str]:
        return self.git("log", "--format=%s").splitlines()

    def nix_calls(self, *words: str) -> list[tuple[str, ...]]:
        return [call for call in self.fake.calls if call[: len(NIX)] == NIX and call[len(NIX) :][: len(words)] == words]

    def sudo_calls(self) -> list[tuple[str, ...]]:
        return [call for call in self.fake.calls if call[:1] == (SUDO,)]

    def log(self) -> str:
        return (self.ws / ".git" / "basix" / "last.log").read_text()

    def trace(self) -> list[str]:
        """The calls that matter, in order, as short labels."""
        labels: list[str] = []
        for call in self.fake.calls:
            label = _label(call)
            if label and (label != "health" or not labels or labels[-1] != "health"):
                labels.append(label)
        return labels


def _label(call: tuple[str, ...]) -> str | None:
    if call[:1] == (SUDO,):
        rest = call[1:]
        if rest[:2] == ("-n", "true"):
            return "ticket"
        if rest[:1] == ("-v",):
            return "password"
        if rest[:2] == ("systemctl", "reset-failed"):
            return "reset"
        if rest[:1] == ("nix-env",):
            return "profile:" + rest[3] + (":" + Path(rest[4]).name if rest[3] == "--set" else "")
        if rest[:1] == ("nix",):
            return "root-build"
        if rest[0].endswith("/bin/switch-to-configuration"):
            return f"{rest[1]}:{Path(rest[0]).parent.parent.name}"
    if call[: len(NIX)] == NIX:
        rest = call[len(NIX) :]
        if rest[:2] == ("flake", "lock"):
            return "lock"
        if rest[:2] == ("flake", "update"):
            return "update"
        if rest[:3] == ("eval", "--json", "--expr"):
            return "source"
        if rest[:1] == ("eval",):
            return "eval"
        if rest[:1] == ("build",):
            if rest[-1].endswith("#compiler"):
                return "compiler"
            return "packages" if rest[-1].endswith("#input-packages") else "build"
    if call[0].endswith("basix-compile"):
        return "compile"
    if call[: len(HEALTH_COMMAND)] == HEALTH_COMMAND:
        return "health"
    return None


def install_handlers(fake: FakeRunner, world: World) -> None:
    """Teach the fake runner the commands of a Basix apply."""

    def analyze(argv):
        if (
            len(argv) >= 4
            and argv[0] == "env"
            and argv[1].startswith("SYSTEMD_UNIT_PATH=")
            and argv[2:4] == ("systemd-analyze", "verify")
            and world.analyze is not None
        ):
            return Result(0, world.analyze)  # the real program exits 0 even when it finds a cycle
        return None

    fake.add(analyze)

    def build(argv):
        target = argv[-1]
        if target.endswith("#compiler"):
            if world.build_error and world.build_error.startswith("compiler"):
                return Result(1, "error: compiler build failed\n")
            path = world.store("basix-compiler")
            (path / "bin" / "basix-compile").write_text("#!/bin/sh\n")
            return Result(0, str(path) + "\n")
        if target.endswith("#input-packages"):
            if world.input_packages_code:
                return Result(world.input_packages_code, "error: attribute 'input-packages' missing\n")
            return Result(0, str(world.tmp / "store" / "input-packages") + "\n")
        if world.build_error:
            return Result(1, world.build_error)
        return Result(0, str(world.store(world.next_system)) + "\n")

    def compile_machine(argv):
        if world.compile_error:
            return Result(1, world.compile_error + "\n")
        data: dict[str, object] = {"name": "pybox", "profile": "console", "fragments": []}
        if world.caches:
            settings = {
                "extra-substituters": [url for url, _ in world.caches],
                "extra-trusted-public-keys": [key for _, key in world.caches],
            }
            data["fragments"] = [
                {"path": ["nix"], "source": "system.py:3 (Machine)", "value": {"settings": settings}, "version": 1},
                {
                    "path": ["services", "openssh"],
                    "source": "system.py:5 (RemoteLogin)",
                    "value": {"enable": True, "settings": {"PermitRootLogin": "no"}},
                    "version": 1,
                },
            ]
        if world.provenance is not None:
            data["provenance"] = world.provenance
        Path(argv[2]).write_text(json.dumps(data))
        return Result(0, "")

    def sudo(argv):
        rest = argv[1:]
        if rest == ("-n", "true"):
            return Result(0) if world.ticket else Result(1, "sudo: a password is required\n")
        if rest == ("-v",):
            if world.password_ok:
                world.ticket = True
                return Result(0)
            return Result(1, "sudo: 3 incorrect password attempts\n")
        if not world.ticket:
            world.unauthorized.append(tuple(argv))
            return Result(1, "sudo: a password is required\n")
        if rest[:2] == ("systemctl", "reset-failed"):
            return Result(0)
        if rest[0].endswith("/bin/switch-to-configuration"):
            system, action = Path(rest[0]).parent.parent, rest[1]
            world.switches.append((system.name, action))
            if action == "test":
                if world.test_code and system.name == world.next_system:
                    return Result(world.test_code, "switch failed\n")
                world.run_system(system)
                return Result(0, "activating the configuration...\n")
            if world.boot_code and system.name == world.next_system:
                return Result(world.boot_code, "boot entry failed\n")
            return Result(0)
        if rest[0] == "nix-env":
            profile = world.host.profile
            if rest[3] == "--set":
                world.set_profile(Path(rest[4]))
            elif rest[3] == "--switch-generation":
                profile.unlink(missing_ok=True)
                profile.symlink_to(f"system-{rest[4]}-link")
            elif rest[3] == "--delete-generations":
                world.deleted.append(rest[4])
                (profile.parent / f"system-{rest[4]}-link").unlink(missing_ok=True)
            return Result(0)
        if rest[0] == "nix":
            if world.root_build_code:
                return Result(world.root_build_code, "error: cannot build\n")
            return Result(0, str(world.store(world.next_system)) + "\n")
        return Result(1, "unknown sudo command\n")

    def evaluate(argv):
        if world.eval_error:
            return Result(1, world.eval_error)
        return Result(0, DRV + "\n")

    def fetch(argv):
        """`nix eval --json --expr`: the store path of a locked source. The fake knows the side by `world.updated`."""
        locked = locked_of(argv[-1])
        if locked.get("narHash") == "sha256-PYNIX":
            pynix = world.tmp / ("pynix-new" if world.updated else "pynix-old")
            pynix.mkdir(exist_ok=True)
            (pynix / "CHANGELOG.md").write_text(CHANGELOG)
            return Result(0, json.dumps(str(pynix)) + "\n")
        source = world.tmp / ("basix-source-new" if world.updated else "basix-source-old")
        source.mkdir(exist_ok=True)
        version = world.new_version if world.updated else world.source_version
        (source / "pyproject.toml").write_text(f'[project]\nname = "basix"\nversion = "{version}"\n')
        return Result(0, "warning: noise\n" + json.dumps(str(source)) + "\n")

    def flake_update(argv):
        if world.update_to is None:
            return Result(1, "error: input 'nope' does not exist\n")
        (world.ws / "flake.lock").write_text(world.update_to)
        world.updated = True
        return Result(0, "")

    def lock(argv):
        if world.lock_code:
            return Result(world.lock_code, "error: cannot lock\n")
        stub = (world.ws / "flake.nix").read_text()
        import re

        names = re.findall(r"^\s*inputs\.(?!basix\b|nixpkgs[.-])([\w-]+)\.url", stub, flags=re.MULTILINE)
        (world.ws / "flake.lock").write_text(lock_json(inputs=names))
        return Result(0, "")

    fake.on(NIX + ("build",), build)
    fake.on(NIX + ("eval", "--raw"), evaluate)
    fake.on(NIX + ("flake", "lock"), lock)
    fake.on(NIX + ("eval", "--json", "--expr"), fetch)
    fake.on(NIX + ("flake", "update"), flake_update)
    fake.add(lambda argv: compile_machine(argv) if argv[0].endswith("basix-compile") else None)
    fake.add(lambda argv: sudo(argv) if argv[0] == SUDO else None)
    fake.on(HEALTH_COMMAND, lambda argv: Result(world.health_code, world.units))


def make_setup(tmp_path: Path, **world_options: object) -> Setup:
    """A home folder with the workspace `~/basix` in the state that the install leaves, and a context."""
    tmp_path = tmp_path.resolve()
    home = tmp_path / "home"
    home.mkdir()
    world = World(tmp_path, home / "basix", **world_options)  # ty: ignore[invalid-argument-type]
    fake = FakeRunner()
    install_handlers(fake, world)
    runner = HybridRunner(fake, git_env())
    clock = FakeClock()
    lines: list[str] = []
    ctx = Context(
        home=home,
        cwd=home,
        env={"HOME": str(home)},
        runner=runner,
        clock=clock,
        say=lines.append,
        host=world.host,
        has_terminal=True,
        wall_time=lambda: NOW,
        read_inputs=lambda url: ["nixpkgs"],
    )
    return Setup(world, fake, runner, clock, lines, home, ctx)


def initialized(tmp_path: Path, **world_options: object) -> Setup:
    """A setup whose workspace exists and holds one commit, and whose machine runs the system `sys-0`."""
    setup = make_setup(tmp_path, **world_options)
    ws = setup.ws
    ws.mkdir()
    (ws / "system.py").write_text(ENTRY)
    (ws / "flake.nix").write_text(make_stub().text)
    (ws / "flake.lock").write_text(lock_json())
    (ws / ".gitignore").write_text(".basix/\n")
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.org"]
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], [*identity, "commit", "-q", "-m", "install"]):
        done = subprocess.run(["git", *args], cwd=ws, env=setup.runner.env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
    setup.world.install("sys-0")
    setup.ctx.cwd = ws
    return setup
