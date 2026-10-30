#!/usr/bin/env python3
"""The VM check `opt:basix-apply`: the real `basix` command on a Basix v2 machine (P3, 010 PLAN).

Run it from the repository root, through the devenv: `devenv shell -- uv run --no-sync python scripts/apply_vm.py`.
The check needs `/dev/kvm`. It never counts for green. It boots a NixOS VM with one learner account, and runs the
cases below as that account over a test SSH channel, on a terminal, so that `sudo` asks for the password.

The VM has no network and no substituter. The host prepares each state of the machine first, with the same steps that
`basix apply` takes (stub, lock, input packages, compiler, compile, build), and copies the store paths into the VM.
Then the command in the VM finds every build output in its store. The local sources (the Basix source, the demo
input) have one path on the host and in the VM, so a lock written on the host is also the lock of the VM.

The machine is the workspace `fixtures/vmhost` plus the module `nix/vm.nix` (the VM parts, the test password, the test
cache, and a stand-in for the boot loader). The states are the `system.py` texts in `STATES`.

Cases (each prints `CHECK ok` or `CHECK FAIL` lines):

- `env`: the facts of the VM: no network, the machine is the prepared state, the evaluation in the VM equals the host.
- `apply`: a program. The lines "checked", "built", "applied", "saved"; the password line before the one `sudo` prompt;
  the new system is the running system, the profile, and the boot default; one commit; no store path and no git
  command in the output. A second apply prints "nothing to change".
- `fail` (G7, L123): a unit that fails. The apply prints the failure; the earlier system runs again; the boot default
  did not change; the edit stays; nothing is saved. After a reboot the earlier system boots. `BASIX_ON_FAIL=keep` keeps
  the new system running, with no boot default and no commit.
- `undo` (dirty tree): `basix undo` drops the edit and applies the last save.
- `guards` (G11, 007 S4): a secret, a nested repository, and a link out of the tree stop with one line; a public key
  passes (010-D11).
- `cycles` (BSX-411): the host builds two small NixOS systems (`fixtures/cycle/`). The check finds the ordering cycle of
  one and none in the other. It needs no VM. The run also refuses to go on when a prepared system has a cycle.
- `cache` (L134, BSX-400): a new `Cache` in `system.py`. Root builds the machine with the new cache, and the new
  `nix.conf` holds it.
- `input` (BSX-397): one `Input` from a local flake. One apply writes the stub, locks, builds the input package,
  compiles a typed value, and switches.
- `undo2`: the clean tree, the toggle, and the one-save refusal.
- `bootfail` (L123): the boot loader fails after the new system runs. The earlier system is back, the profile points to it
  again, and the failed generation is gone.
- `explain`: `basix explain trusted_interfaces` and `basix explain RemoteLogin`.
- `minix`: the launcher of the core holds the public URL. Its run needs the network, so the check does not run it.
- `update`: `basix update` with a second Basix commit in a local repository, then `basix apply`.
- `reboot`: the system of the last apply is the boot default: it runs after a reboot.

Groups (the argument `--groups`, or `--cases`; a comma list):

- The default is every group, in the order above. The slice gate runs the full set.
- A development run names only the groups that the change touched, for example
  `scripts/apply_vm.py --groups apply,fail`. The groups run in the order of the list above, not the order you type.
- A group may need an earlier group, because it reads the state that the earlier group left. The run adds the needed
  groups and prints a line for each: `undo` needs `fail`; `undo2` and `bootfail` need `cache` and `input`;
  `update` needs `input`.
- The run always prepares all states and stages the VM. A short list of groups saves only the run of the other groups.
- An unknown group name stops the run before any work, with the list of known names.

Environment (all optional):

- `BASIX_WORK`: the folder of the VM files, the sources, and the prepared states (default `~/.cache/basix/v2/apply`).
- `BASIX_LOGS`: the raw logs and `results.json` (default `~/.local/state/basix/v2/apply/logs`).
- `BASIX_VM_PORT`: the host port of the test channel (default 22560).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import resource
import select
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The version of the checked tree and the next patch version (the scratch release that `basix update` finds).
VERSION = re.search(r'^version = "(\d+)\.(\d+)\.(\d+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
assert VERSION, "no plain version in pyproject.toml"
OLD_VERSION = ".".join(VERSION.groups())
NEW_VERSION = f"{VERSION[1]}.{VERSION[2]}.{int(VERSION[3]) + 1}"
sys.path.insert(0, str(ROOT / "src"))

import vmlib  # noqa: E402
from basix.units import ordering_cycles  # noqa: E402
from pynix_native import discover_source  # noqa: E402

from basix_config.stub import make_stub  # noqa: E402

FEATURES = ["--extra-experimental-features", "nix-command flakes"]
USER = "learner"
PASSWORD = "basix-test"
WORK = Path(os.environ.get("BASIX_WORK") or vmlib.workdir.default_work("v2/apply"))
LOGS = Path(os.environ.get("BASIX_LOGS", Path.home() / ".local/state/basix/v2/apply/logs"))
PORT = os.environ.get("BASIX_VM_PORT", "22560")
#: The sources have this path on the host and in the VM.
MIRROR = WORK / "mirror"
SOURCE = MIRROR / "basix-src"
DEMO = MIRROR / "demo-input"
BASIX_URL = f"git+file://{SOURCE}"
PREP = WORK / "prep"
CACHE = WORK / "cache"
KEY = WORK / "key"
FLAKE_FILES = ["flake.nix", "flake.lock", "pyproject.toml", "uv.lock", "README.md", "lib", "modules", "profiles", "capabilities", "nix", "src"]
GROUPS = ["cycles", "env", "apply", "fail", "undo", "guards", "cache", "input", "undo2", "bootfail", "explain", "minix", "update", "reboot"]
#: The groups that a group needs to run first (they leave the state or the context that it reads). The order of
#: `GROUPS` is the order of the run. `undo` reads `ctx["fail_before"]`; `undo2` restores the save of `cache` and `input`;
#: `bootfail` builds the state of `cache` and restores the save of `input`; `update` needs the store paths that
#: `input` copies. Read from the code of the cases; not run alone yet.
NEEDS = {"undo": ["fail"], "undo2": ["cache", "input"], "bootfail": ["cache", "input"], "update": ["input"]}
MACHINE = "basix"
TOPLEVEL = f"nixosConfigurations.{MACHINE}.config.system.build.toplevel"
CACHE_PORT = "8765"
CACHE_URL = f"http://127.0.0.1:{CACHE_PORT}"
BOOT_DIR = "/var/lib/basix-test"
FAIL_NIX = """\
# TEST ONLY: a unit that starts and then fails after two seconds (G7).
{ pkgs, ... }: {
  systemd.services.basix-fails = {
    wantedBy = [ "multi-user.target" ];
    serviceConfig.ExecStart = "${pkgs.runtimeShell} -c 'sleep 2; exit 1'";
  };
}
"""
FAKE_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGJ/Lg5EFlUJN6ems0FaIIrtr40l5sGLnWYjSNwRA55G learner@laptop\n"

RESULTS: dict = {"checks": [], "times": {}, "notes": {}}
RAW = None
SSHOPT: list[str] = []
SEEN: set[str] = set()  # the store paths that the VM holds (the image and the copies)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


# --- the states ------------------------------------------------------------------------------------------------
def system_py(*, programs: str = "", natives: tuple[str, ...] = (), extra: str = "", imports: str = "", caps: str = "", trusted: str = "") -> str:
    native = ", ".join(f'NativeModule("{name}")' for name in ("vm-root.nix", "vm.nix", *natives))
    lines = [
        *([imports, ""] if imports else []),
        "from basix_config import *",
        "",
        "machine = Machine(",
        '    name="basix",',
        f'    user="{USER}",',
        "    disk=None,",
        '    state_version="26.05",',
        '    serial_console="ttyS0",',
        *([f"    programs=[{programs}],"] if programs else []),
        *([f"    capabilities=[{caps}],"] if caps else []),
        *([f"    trusted_interfaces=[{trusted}],"] if trusted else []),
        f"    native_modules=[{native}],",
        *([extra] if extra else []),
        ")",
    ]
    return "\n".join(lines) + "\n"


def states(cache_key: str) -> dict[str, str]:
    """The `system.py` text of each state. `S0` is the VM that boots."""
    s1 = {"programs": "Program(pkgs.hello)"}
    s4 = {**s1, "extra": f'    caches=[Cache("{CACHE_URL}", "{cache_key}")],'}
    s5 = {
        **s4,
        "imports": "import pynix_demo as demo",
        "extra": s4["extra"]
        + f'\n    inputs=[Input("demo", "path:{DEMO}", modules=["demo"])],\n    options=[demo.Demo(enable=True, greeting="hi", count=2)],',
    }
    return {
        "S0": system_py(),
        "S1": system_py(**s1),
        "S2": system_py(**s1, natives=("fail.nix",)),
        "S4": system_py(**s4),
        "S5": system_py(**s5),
        "explain": system_py(caps="RemoteLogin()", trusted='"eth0"'),
    }


# --- logging ---------------------------------------------------------------------------------------------------
def log(text: str = "") -> None:
    print(text, flush=True)
    if RAW:
        RAW.write(text + "\n")
        RAW.flush()


def check(case: str, what: str, expected: object, actual: object, ok: bool | None = None) -> bool:
    ok = (expected == actual) if ok is None else ok
    RESULTS["checks"].append({"case": case, "check": what, "expected": expected, "actual": actual, "ok": bool(ok)})
    log(f"   CHECK {'ok  ' if ok else 'FAIL'} [{case}] {what}: expected {expected!r}, actual {str(actual)[:500]!r}")
    return bool(ok)


def info(case: str, what: str, value: object) -> None:
    RESULTS["notes"][f"{case}: {what}"] = value
    log(f"   INFO [{case}] {what}: {str(value)[:600]!r}")


# --- the host --------------------------------------------------------------------------------------------------
def git_env() -> dict[str, str]:
    """A fixed identity and date: the scratch commit of the same tree is the same commit."""
    date = "2026-01-01T00:00:00+00:00"
    return {
        "GIT_AUTHOR_NAME": "scratch",
        "GIT_AUTHOR_EMAIL": "scratch@example.org",
        "GIT_COMMITTER_NAME": "scratch",
        "GIT_COMMITTER_EMAIL": "scratch@example.org",
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def host(command: list[str], cwd: Path | None = None, env: dict[str, str] | None = None, check_rc: bool = True, timeout: int = 3600):
    """Run a command on the host. The output goes to the raw log. A `git` command gets the fixed identity."""
    identity = git_env() if command[:1] == ["git"] else {}
    done = subprocess.run(command, cwd=cwd, env={**os.environ, **identity, **(env or {})}, capture_output=True, text=True, timeout=timeout)
    if RAW:
        RAW.write(f"$ {' '.join(command)}  (exit {done.returncode})\n{done.stdout[-4000:]}{done.stderr[-4000:]}\n")
        RAW.flush()
    if check_rc and done.returncode != 0:
        raise SystemExit(f"{' '.join(command)} failed ({done.returncode}):\n{(done.stdout + done.stderr)[-3000:]}")
    return done


def nix(*args: str, cwd: Path | None = None, check_rc: bool = True):
    return host(["nix", *FEATURES, *args], cwd=cwd, check_rc=check_rc)


def closure(path: str | Path) -> set[str]:
    return set(host(["nix-store", "-qR", str(Path(path).resolve())]).stdout.split())


def tree_text(path: Path) -> str:
    return path.read_text()


# --- the sources and the states on the host --------------------------------------------------------------------
def prepare_source() -> None:
    """The Basix source as a git repository: the commit that the machines lock, and the branch `next` (a new release)."""
    shutil.rmtree(SOURCE, ignore_errors=True)
    SOURCE.mkdir(parents=True)
    for name in FLAKE_FILES:
        source = ROOT / name
        if source.is_dir():
            shutil.copytree(source, SOURCE / name, ignore=shutil.ignore_patterns("__pycache__", ".devenv", ".direnv"))
        else:
            shutil.copy2(source, SOURCE / name)
    host(["git", "init", "-q", "-b", "main"], cwd=SOURCE)
    host(["git", "add", "-A"], cwd=SOURCE)
    host(["git", "commit", "-q", "-m", "Basix, the commit that the machines lock"], cwd=SOURCE)
    host(["git", "checkout", "-q", "-b", "next"], cwd=SOURCE)
    for name in ("pyproject.toml", "uv.lock"):  # a new release changes the version only: the compiler does not change
        text = (SOURCE / name).read_text()
        (SOURCE / name).write_text(text.replace(f'version = "{OLD_VERSION}"', f'version = "{NEW_VERSION}"', 1))
    host(["git", "commit", "-q", "-am", f"Basix v{NEW_VERSION}"], cwd=SOURCE)
    host(["git", "checkout", "-q", "main"], cwd=SOURCE)


def prepare_demo() -> None:
    shutil.rmtree(DEMO, ignore_errors=True)
    shutil.copytree(ROOT / "fixtures" / "demo-input", DEMO)


def make_key() -> tuple[str, str]:
    """The test cache key: the public key goes into `system.py`, the secret key signs the cache."""
    secret, public = WORK / "cache.sec", WORK / "cache.pub"
    if not secret.exists():
        host(["sh", "-c", f"nix {' '.join(shlex.quote(f) for f in FEATURES)} key generate-secret --key-name test-cache-1 > {secret}"])
        host(["sh", "-c", f"nix {' '.join(shlex.quote(f) for f in FEATURES)} key convert-secret-to-public < {secret} > {public}"])
    return str(secret), public.read_text().strip()


def workspace_files(name: str, text: str, pub: str, extra_files: dict[str, str]) -> dict[str, str]:
    files = {
        "system.py": text,
        "vm.nix": (ROOT / "nix" / "vm.nix").read_text(),
        "vm-root.nix": (ROOT / "fixtures" / "vmhost" / "vm-root.nix").read_text(),
        "vm.pub": pub,
        ".gitignore": ".basix/\n",
    }
    return {**files, **extra_files}


def refuse_cycles(top: str, label: str) -> None:
    """Stop the run when a prepared system has a systemd ordering cycle (BSX-411, 010-D56)."""
    try:
        vmlib.refuse_cycles(Path(top), label)
    except vmlib.Stop as stop:
        raise SystemExit(str(stop)) from None


def build_state(name: str, text: str, pub: str, extra_files: dict[str, str] | None = None, build: bool = True) -> dict:
    """Make a workspace with the steps of `basix apply`: stub, lock, input packages, compiler, compile, build."""
    ws = PREP / name
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    for file, content in workspace_files(name, text, pub, extra_files or {}).items():
        (ws / file).write_text(content)
    found = discover_source(text, "system.py")
    (ws / "flake.nix").write_text(make_stub(found, BASIX_URL).text)
    host(["git", "init", "-q", "-b", "main"], cwd=ws)
    host(["git", "add", "-A"], cwd=ws)
    host(["git", "commit", "-q", "-m", "seed"], cwd=ws)
    nix("flake", "lock", "--no-warn-dirty", cwd=ws)
    host(["git", "add", "-A"], cwd=ws)
    out: dict = {"ws": ws, "closure": set()}
    if found.inputs:
        (ws / ".basix").mkdir()
        pkgs = nix("build", "--no-warn-dirty", "--print-out-paths", "--out-link", str(ws / ".basix" / "pynix"), ".#input-packages", cwd=ws).stdout.split()[-1]
        out["inputs"] = pkgs
        out["closure"] |= closure(pkgs)
    compiler = nix("build", "--no-warn-dirty", "--print-out-paths", "--out-link", str(PREP / "compiler"), ".#compiler", cwd=ws).stdout.split()[-1]
    out["compiler"] = compiler
    out["closure"] |= closure(compiler)
    host([f"{compiler}/bin/basix-compile", "system.py", "system.json"], cwd=ws)
    host(["git", "add", "-A"], cwd=ws)
    host(["git", "commit", "-q", "-m", "state", "--allow-empty"], cwd=ws)
    if build:
        top = nix("build", "--no-warn-dirty", "--print-out-paths", "--out-link", str(PREP / f"{name}-system"), f".#{TOPLEVEL}", cwd=ws).stdout.split()[-1]
        refuse_cycles(top, f"state {name}")
        out["toplevel"] = top
        out["closure"] |= closure(top)
    return out


def prepare_states(pub: str, cache_key: str) -> dict[str, dict]:
    texts = states(cache_key)
    shutil.rmtree(PREP, ignore_errors=True)
    PREP.mkdir(parents=True)
    prepared: dict[str, dict] = {}
    fail = {"fail.nix": FAIL_NIX}
    prepared["S0"] = build_state("S0", texts["S0"], pub)
    prepared["S1"] = build_state("S1", texts["S1"], pub)
    prepared["S2"] = build_state("S2", texts["S2"], pub, fail)
    prepared["S4"] = build_state("S4", texts["S4"], pub)
    prepared["S5"] = build_state("S5", texts["S5"], pub)
    prepared["explain"] = build_state("explain", texts["explain"], pub, build=False)
    # The state after `basix update`: the files of S5, with the lock of the new Basix commit.
    ws = PREP / "S9"
    shutil.rmtree(ws, ignore_errors=True)
    shutil.copytree(PREP / "S5", ws, symlinks=True)
    host(["git", "checkout", "-q", "next"], cwd=SOURCE)
    try:
        nix("flake", "update", "basix", "--no-warn-dirty", cwd=ws)
        pkgs = nix("build", "--no-warn-dirty", "--print-out-paths", "--out-link", str(ws / ".basix" / "pynix"), ".#input-packages", cwd=ws).stdout.split()[-1]
        host(["git", "add", "-A"], cwd=ws)
        top = nix("build", "--no-warn-dirty", "--print-out-paths", "--out-link", str(PREP / "S9-system"), f".#{TOPLEVEL}", cwd=ws).stdout.split()[-1]
    finally:
        host(["git", "checkout", "-q", "main"], cwd=SOURCE)
    refuse_cycles(top, "state S9")
    prepared["S9"] = {"ws": ws, "toplevel": top, "inputs": pkgs, "closure": closure(top) | closure(pkgs)}
    for key, value in prepared.items():
        if "toplevel" in value:
            log(f"   prepared {key}: {value['toplevel']}")
    return prepared


# --- the VM ----------------------------------------------------------------------------------------------------
class Remote:
    """One remote command: its exit code, its output, its wall time, and how often `sudo` asked for the password."""

    def __init__(self, rc: int, out: str, wall: float, prompts: int = 0) -> None:
        self.rc, self.out, self.wall, self.prompts = rc, out, wall, prompts

    @property
    def lines(self) -> list[str]:
        return [line.strip() for line in self.out.splitlines() if line.strip()]

    def fields(self) -> dict[str, str]:
        return dict(line.split("=", 1) for line in self.out.splitlines() if "=" in line)


def show(title: str, result: Remote, user: str = USER) -> None:
    log(f"$ [{user}] {title}   (rc={result.rc}, {result.wall:.1f} s, password prompts: {result.prompts})")
    for line in result.out.rstrip().splitlines()[-40:]:
        log("    " + line)


def sh(command: str, user: str = USER, timeout: int = 1800, title: str | None = None, shown: bool = True, stdin: str | None = None) -> Remote:
    """Run a command in the VM over the test channel, with no terminal, in a login shell of `user`."""
    started = time.time()
    done = subprocess.run(
        ["ssh", *SSHOPT, f"{user}@127.0.0.1", "bash -l -c " + shlex.quote(command)],
        capture_output=True, text=True, timeout=timeout, input=stdin or "",
    )  # fmt: skip
    result = Remote(done.returncode, done.stdout + done.stderr, time.time() - started)
    if shown:
        show(title or command, result, user)
    return result


def put(path: str, text: str) -> None:
    done = subprocess.run(
        ["ssh", *SSHOPT, f"{USER}@127.0.0.1", f'mkdir -p "$(dirname ~/{path})" && cat > ~/{path}'], input=text, text=True, capture_output=True
    )  # fmt: skip
    assert done.returncode == 0, done.stderr


def tar_to(source: Path, target: str, user: str = USER, exclude: tuple[str, ...] = ()) -> None:
    """Copy a folder into the VM, with its times and its `.git`."""
    pack = subprocess.run(["tar", "-C", str(source), *[f"--exclude={e}" for e in exclude], "-cf", "-", "."], capture_output=True, check=True)
    done = subprocess.run(
        ["ssh", *SSHOPT, f"{user}@127.0.0.1", f"mkdir -p {target} && tar -C {target} -xf -"], input=pack.stdout, capture_output=True
    )  # fmt: skip
    assert done.returncode == 0, done.stderr.decode()


def tty(command: str, timeout: int = 3000, title: str | None = None, password: str | None = PASSWORD) -> Remote:
    """Run a command of the learner on a terminal. When `sudo` asks for the password, type it. Count the prompts.

    The output of `ssh -tt` is the output of the terminal: the lines of the command, and the prompt of `sudo`.
    The prompt leaves the result, but `prompts` counts it.
    """
    started = time.time()
    proc = subprocess.Popen(
        ["ssh", "-tt", *SSHOPT, f"{USER}@127.0.0.1", "bash -l -c " + shlex.quote(command)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip
    assert proc.stdout is not None and proc.stdin is not None
    fd, buf, answered, end = proc.stdout.fileno(), b"", 0, time.time() + timeout
    while True:
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf += chunk
            text = ANSI.sub("", buf.decode(errors="replace"))
            count = len(re.findall(r"\[sudo\] password for", text))
            if count > answered and password is not None and text.rstrip().endswith(":"):
                proc.stdin.write((password + "\n").encode())
                proc.stdin.flush()
                answered = count
        elif proc.poll() is not None:
            break
        if time.time() > end:
            proc.kill()
            break
    proc.wait()
    raw = ANSI.sub("", buf.decode(errors="replace")).replace("\r\n", "\n").replace("\r", "")
    prompts = len(re.findall(r"\[sudo\] password for \w+: ?", raw))
    out = re.sub(r"\[sudo\] password for \w+: ?\n?", "", raw)
    # The first `sudo` of an account prints its lecture (sudo, not Basix). It is no line of the command.
    lecture = re.search(r"We trust you have received.*?will not be visible\.\n", out, re.DOTALL)
    if lecture:
        out = out.replace(lecture[0], "")
        info("sudo", "the lecture that the first sudo of the account printed", lecture[0].strip().splitlines()[0])
    rc = re.search(r"^RC=(\d+)$", out, re.MULTILINE)
    out = re.sub(r"^RC=\d+\n?", "", out, flags=re.MULTILINE)
    result = Remote(int(rc[1]) if rc else 255, out, time.time() - started, prompts)
    result.raw = raw  # ty: ignore[unresolved-attribute]
    show(title or command, result)
    return result


def wait_ssh(timeout: int = 300) -> None:
    end = time.time() + timeout
    while time.time() < end:
        done = subprocess.run(["ssh", *SSHOPT, "root@127.0.0.1", "true"], capture_output=True, text=True)
        if done.returncode == 0:
            return
        time.sleep(3)
    raise SystemExit("ssh never came up")


def ssh_alive() -> bool:
    return subprocess.run(["ssh", *SSHOPT, "root@127.0.0.1", "true"], capture_output=True, text=True).returncode == 0


PROBE = r"""
cd {where} 2>/dev/null
echo cur=$(readlink /run/current-system)
echo prof=$(readlink -f /nix/var/nix/profiles/system)
echo gens=$(ls /nix/var/nix/profiles | grep -cE '^system-[0-9]+-link$')
echo boot=$(cat /var/lib/basix-test/boot-default 2>/dev/null)
echo boots=$(cat /var/lib/basix-test/boot-installs 2>/dev/null | wc -l)
echo commits=$(git rev-list --count HEAD 2>/dev/null || echo 0)
echo head=$(git log -1 --format=%s 2>/dev/null)
echo dirty=$(git status --short | tr '\n' ';')
echo failed=$(systemctl --failed --no-legend --plain | wc -l)
echo fails=$(systemctl is-active basix-fails.service 2>&1)
echo hello=$(command -v hello)
"""


def state(where: str = "~/basix", shown: bool = True) -> dict[str, str]:
    fields = sh(PROBE.format(where=where), shown=False).fields()
    if shown:
        log("   STATE: " + "; ".join(f"{k}={v[-45:] if k in {'cur', 'prof', 'boot'} else v}" for k, v in fields.items()))
    return fields


def basix(args: str = "apply", label: str = "", env: str = "", where: str = "~/basix", then: str = "") -> Remote:
    """Run `basix` as the learner, on a terminal, in `where`. `then` runs in the same session afterwards."""
    result = tty(f"cd {where} && {env}basix {args}; echo RC=$?" + (f"; {then}" if then else ""), title=f"basix {args}  [{label}] in {where}")
    RESULTS["times"][label or args] = round(result.wall, 1)
    return result


def no_leak(lines: list[str]) -> bool:
    """The plain lines name no store path and no git command (009-D15)."""
    return not any("/nix/store" in line or re.search(r"\bgit\b", line) for line in lines)


def top(prepared: dict[str, dict], name: str) -> str:
    return str(Path(prepared[name]["toplevel"]).resolve())


# --- the cases -------------------------------------------------------------------------------------------------
APPLIED = "applied: the system runs, and its units are healthy"
WATCHING = "checking the units for 8 s"
PASSWORD_LINE = "Basix needs your password to change the machine"
CHECKED = "checked: system.py"


def put_state(text: str, extra: dict[str, str] | None = None) -> None:
    put("basix/system.py", text)
    for name, content in (extra or {}).items():
        put(f"basix/{name}", content)


def case_cycles(p: dict[str, dict]) -> None:
    """BSX-411: the check finds the cycle of a real toplevel, and no cycle in a toplevel without one."""
    lock = json.loads((ROOT / "flake.lock").read_text())["nodes"]["nixpkgs"]["locked"]
    url = f"github:{lock['owner']}/{lock['repo']}/{lock['rev']}?narHash={lock['narHash'].replace('+', '%2B')}"
    tops: dict[str, Path] = {}
    for name in ("cycle", "ok"):
        expr = f'import {ROOT}/fixtures/cycle/system.nix {{ nixpkgs = builtins.getFlake "{url}"; module = {ROOT}/fixtures/cycle/{name}.nix; }}'
        started = time.time()
        built = nix("build", "--impure", "--no-link", "--print-out-paths", "--expr", expr).stdout.split()[-1]
        tops[name] = Path(built)
        info("cycles", f"{name}: toplevel built in", f"{time.time() - started:.1f} s")
    started = time.time()
    found = ordering_cycles(vmlib.HostRun(), tops["cycle"])
    info("cycles", "the check of one toplevel took", f"{time.time() - started:.2f} s")
    units = {unit for cycle in found for unit in cycle.units}
    check("cycles", "the cycle fixture has a cycle", True, bool(found))
    check("cycles", "the check names both units of the cycle", ["cycle-a.service", "cycle-b.service"], sorted(units & {"cycle-a.service", "cycle-b.service"}))
    check("cycles", "the fixture without a loop has no cycle", [], [cycle.line for cycle in ordering_cycles(vmlib.HostRun(), tops["ok"])])
    for name, state in p.items():
        if "toplevel" in state:
            check("cycles", f"the prepared system {name} has no cycle", [], [cycle.line for cycle in ordering_cycles(vmlib.HostRun(), Path(state["toplevel"]))])


def case_env(p: dict[str, dict]) -> None:
    who = sh("id -un; nix --version; nproc; free -m | sed -n 2p", title="facts")
    check("env", "the account is the learner", USER, who.lines[0])
    flags = sh("grep experimental-features /etc/nix/nix.conf; true", shown=False).out
    check("env", "Nix flakes are on (the core sets them)", True, "flakes" in flags)
    net = sh("curl -sS -m 5 https://cache.nixos.org/nix-cache-info", shown=False)
    check("env", "the VM has no network", True, net.rc != 0)
    check("env", "the running system is the prepared state S0", top(p, "S0"), sh("readlink /run/current-system", shown=False).out.strip())
    mine = sh(f"cd ~/basix && nix eval --raw .#{TOPLEVEL}.outPath", title="the toplevel that the VM evaluates for ~/basix", timeout=900)
    check("env", "the VM evaluates the same system as the host (the sources are the same)", top(p, "S0"), mine.out.strip().splitlines()[-1] if mine.out.strip() else "")
    cmd = sh("command -v basix; basix --version", shown=False)
    check("env", "the command runs and names its version", [f"basix {OLD_VERSION}"], cmd.lines[-1:])
    sudo = sh("stat -c %a /run/wrappers/bin/sudo", shown=False).out.strip()
    check("env", "sudo is the setuid copy", True, sudo.startswith("4"))
    s = state()
    check("env", "the system profile is the running system", s["cur"], s["prof"])
    check("env", "the tree is clean", "", s["dirty"])
    check("env", "one commit (the seed)", "1", s["commits"])


def case_apply(p: dict[str, dict]) -> dict[str, str]:
    before = state()
    put_state(states_cache["S1"])
    r = basix("apply", "first apply")
    check("apply", "the apply exits 0", 0, r.rc)
    check("apply", "the lines are checked, built, applied, saved", [CHECKED, "building the system", "built the system", PASSWORD_LINE, WATCHING, APPLIED, "saved: system.json, system.py"], r.lines)
    raw = getattr(r, "raw", "")
    check("apply", "the password line comes before the sudo prompt", True, 0 <= raw.find(PASSWORD_LINE) < raw.find("[sudo] password for"))
    check("apply", "sudo asks once", 1, r.prompts)
    check("apply", "the lines name no store path and no git command", True, no_leak(r.lines))
    s = state()
    check("apply", "the running system is the built one", top(p, "S1"), s["cur"])
    check("apply", "the system profile points to it (the boot default)", top(p, "S1"), s["prof"])
    check("apply", "the boot loader got the new system once", [top(p, "S1"), str(int(before["boots"]) + 1)], [s["boot"], s["boots"]])
    check("apply", "a new generation of the profile", str(int(before["gens"]) + 1), s["gens"])
    check("apply", "one more commit", str(int(before["commits"]) + 1), s["commits"])
    check("apply", "the commit is the apply", "apply: system.json, system.py", s["head"])
    check("apply", "the tree is clean", "", s["dirty"])
    check("apply", "the program is on the path", True, s["hello"].startswith("/run/current-system/sw/bin/"))
    log_text = sh("cat ~/basix/.git/basix/last.log", shown=False).out
    check("apply", "the log holds the commands", True, "$ sudo" in log_text.replace("/run/wrappers/bin/", "") and "switch-to-configuration test" in log_text and "--set" in log_text)
    r2 = basix("apply", "second apply")
    check("apply", "the second apply exits 0", 0, r2.rc)
    check("apply", "it prints nothing to change", [CHECKED, "building the system", "built the system", "nothing to change"], r2.lines)
    check("apply", "it asks for no password", 0, r2.prompts)
    s2 = state()
    for key in ("cur", "prof", "gens", "boots", "commits"):
        check("apply", f"{key} did not change", s[key], s2[key])
    return s2


def case_fail(p: dict[str, dict], ctx: dict) -> None:
    before = state()
    boot_id = sh("cat /proc/sys/kernel/random/boot_id", user="root", shown=False).out.strip()
    put_state(states_cache["S2"], {"fail.nix": FAIL_NIX})
    r = basix("apply", "failing unit")
    check("fail", "the apply exits 3", 3, r.rc)
    text = "\n".join(r.lines)
    check("fail", "the lines name the failed unit", True, "problem: failed units: basix-fails.service" in r.lines)
    check("fail", "the earlier system is back", True, "failed: the earlier system is back. Nothing was saved." in text)
    check("fail", "the lines tell what to do", True, "Your edited files are still in the folder. Fix them and run basix apply, or run basix undo to drop the edit." in text)
    check("fail", "the lines name no store path and no git command", True, no_leak(r.lines))
    check("fail", "sudo asked once", 1, r.prompts)
    s = state()
    check("fail", "the running system is the earlier one", before["cur"], s["cur"])
    check("fail", "the system profile (the boot default) did not change", before["prof"], s["prof"])
    check("fail", "the boot loader got no call", [before["boot"], before["boots"]], [s["boot"], s["boots"]])
    check("fail", "no new generation", before["gens"], s["gens"])
    check("fail", "no commit", before["commits"], s["commits"])
    check("fail", "the edit stays in the folder", True, "system.py" in s["dirty"] and "fail.nix" in s["dirty"])
    check("fail", "the failing unit is gone and no unit is failed", ["0", "inactive"], [s["failed"], s["fails"]])
    # Reboot: the earlier system boots (the stand-in boot loader follows the profile).
    reboot_vm()
    after_boot = sh("cat /proc/sys/kernel/random/boot_id", user="root", shown=False).out.strip()
    check("fail", "the VM rebooted", True, after_boot != boot_id)
    s = state()
    check("fail", "after the reboot the earlier system runs", before["cur"], s["cur"])
    check("fail", "after the reboot the edit still stays", True, "fail.nix" in s["dirty"])
    # BASIX_ON_FAIL=keep: the new system stays, still with no boot default and no commit.
    k = basix("apply", "failing unit, keep", env="BASIX_ON_FAIL=keep ")
    check("fail", "keep: the apply exits 3", 3, k.rc)
    check("fail", "keep: the lines say that the new system runs", True, "failed: the new system is running, but it did not start cleanly. Nothing was saved." in "\n".join(k.lines))
    ks = state()
    check("fail", "keep: the running system is the new one", top(p, "S2"), ks["cur"])
    check("fail", "keep: the profile did not change", before["prof"], ks["prof"])
    check("fail", "keep: the boot loader got no call", [before["boot"], before["boots"]], [ks["boot"], ks["boots"]])
    check("fail", "keep: no commit", before["commits"], ks["commits"])
    ctx["fail_before"] = before


def case_undo(p: dict[str, dict], ctx: dict) -> None:
    """The tree is dirty (the failing edit) and the failing system runs (keep). Undo drops the edit."""
    before = ctx["fail_before"]
    r = basix("undo", "undo, dirty tree")
    check("undo", "the undo exits 0", 0, r.rc)
    check("undo", "it says that it dropped the edit", True, any(line.startswith("undo: dropped the edit") for line in r.lines))
    check("undo", "it ends with nothing to change (the files equal the last save)", "nothing to change", r.lines[-1] if r.lines else "")
    s = state()
    check("undo", "the tree is clean", "", s["dirty"])
    check("undo", "no new commit", before["commits"], s["commits"])
    check("undo", "the system of the last save runs again", before["cur"], s["cur"])
    check("undo", "the failing unit is gone", ["0", "inactive"], [s["failed"], s["fails"]])
    dropped = sh("cd ~/basix && git for-each-ref refs/basix | wc -l", shown=False).out.strip()
    check("undo", "the dropped edit stays in the history of the folder", True, int(dropped) >= 1)
    check("undo", "fail.nix is gone", False, sh("test -e ~/basix/fail.nix", shown=False).rc == 0)
    check("undo", "system.py is the file of the last save", states_cache["S1"], sh("cat ~/basix/system.py", shown=False).out)
    ctx["clean"] = s


def case_guards(p: dict[str, dict], ctx: dict) -> None:
    folder = "~/basix"
    before = state()

    def refused(label: str, setup: str, wanted: str, cleanup: str) -> None:
        sh(setup, shown=False)
        r = basix("apply", label)
        check("guards", f"{label}: exit 2", 2, r.rc)
        check("guards", f"{label}: the line that names the file and the fix", wanted, r.lines[-1] if r.lines else "")
        check("guards", f"{label}: the output is exactly one line, the refusal", [wanted], r.lines)
        check("guards", f"{label}: sudo asked for nothing", 0, r.prompts)
        s = state()
        for key in ("cur", "prof", "gens", "boots", "commits", "head"):
            check("guards", f"{label}: {key} did not change", before[key], s[key])
        staged = sh("cd ~/basix && git diff --cached --name-only | tr '\\n' ' '", shown=False).out.strip()
        check("guards", f"{label}: the index is unchanged (no staged path)", "", staged)
        sh(cleanup, shown=False)

    refused("a secret", "cd ~/basix && echo not-a-key > key.pem", f"apply stopped: key.pem looks like a secret; move it out of {folder}", "rm -f ~/basix/key.pem")
    refused(
        "a nested repository",
        "cd ~/basix && mkdir sub && cd sub && git init -q && echo x > f && git add f && git commit -q -m x",
        f"apply stopped: sub holds its own repository, so its files stay hidden; move it out of {folder}",
        "rm -rf ~/basix/sub",
    )
    refused("a link out of the tree", "cd ~/basix && ln -s /etc/passwd link", f"apply stopped: link is a link that leaves {folder}; put the file inside {folder}", "rm -f ~/basix/link")
    s = state()
    check("guards", "the tree is clean again", "", s["dirty"])
    # A public key is not a secret (010-D11).
    put("basix/id_ed25519.pub", FAKE_PUBLIC_KEY)
    r = basix("apply", "a public key")
    check("guards", "a public key passes: exit 0", 0, r.rc)
    check("guards", "the lines say that only files changed", True, "the system is the same; only files changed" in r.lines and "saved: id_ed25519.pub" in r.lines)
    check("guards", "it asks for no password (nothing is switched)", 0, r.prompts)
    s = state()
    check("guards", "the system did not change", before["cur"], s["cur"])
    check("guards", "one more commit", str(int(before["commits"]) + 1), s["commits"])
    check("guards", "the commit names the file", "apply: id_ed25519.pub", s["head"])


def case_cache(p: dict[str, dict], ctx: dict) -> None:
    before = state()
    conf = sh("cat /run/current-system/etc/nix/nix.conf", shown=False).out
    check("cache", "the running nix.conf lacks the new cache", False, CACHE_URL in conf)
    served = sh(f"curl -s -m 5 {CACHE_URL}/nix-cache-info", shown=False)
    check("cache", "the test cache answers in the VM", True, "StoreDir: /nix/store" in served.out)
    put_state(states_cache["S4"])
    r = basix("apply", "new cache")
    check("cache", "the apply exits 0", 0, r.rc)
    if r.rc != 0:  # keep the whole log of the failed build: it tells what the build still missed
        (LOGS / "cache-first-attempt.log").write_text(sh("cat ~/basix/.git/basix/last.log", shown=False).out)
    check(
        "cache",
        "the lines: the build with the new cache, one password line, then the ordinary lines",
        ["building the system with the new caches", PASSWORD_LINE, CHECKED, "building the system", "built the system", WATCHING, APPLIED, "saved: system.json, system.py"],
        r.lines,
    )
    check("cache", "sudo asked once", 1, r.prompts)
    check("cache", "the lines name no store path and no git command", True, no_leak(r.lines))
    log_text = sh("cat ~/basix/.git/basix/last.log", shown=False).out
    root_build = [line for line in log_text.splitlines() if line.startswith("$ ") and "extra-substituters" in line]
    check("cache", "the log shows the root build with the new cache", True, bool(root_build) and CACHE_URL in root_build[0] and "extra-trusted-public-keys" in root_build[0] and "test-cache-1:" in root_build[0])
    info("cache", "the root build command", root_build[:1])
    at = next((i for i, line in enumerate(log_text.splitlines()) if line.startswith("$ ") and "extra-substituters" in line), 0)
    info("cache", "the log after the root build command", [line[:160] for line in log_text.splitlines()[at + 1 : at + 12]])
    check("cache", "the log shows paths copied from the new cache", True, f"from '{CACHE_URL}'" in log_text)
    served = sh("journalctl -u testcache --no-pager -o cat | grep -E 'GET /(nar/[^ ]+|[0-9a-z]{32}\\.narinfo) HTTP/[0-9.]+\" 200'", user="root", shown=False).out
    nars = [line for line in served.splitlines() if "GET /nar/" in line]
    infos = [line.split()[6].removeprefix("/").removesuffix(".narinfo") for line in served.splitlines() if ".narinfo" in line]
    wanted = sorted(Path(path).name.split("-", 1)[0] for path in RESULTS["notes"]["cache paths"])
    toplevel = Path(top(p, "S4")).name.split("-", 1)[0]
    fetched = sorted(set(infos))
    info("cache", "the paths that the VM fetched from the cache", fetched)
    check("cache", "the VM fetched paths from the cache, and only paths of the cache", True, bool(fetched) and set(fetched) <= set(wanted) - {toplevel})
    check("cache", "the VM fetched a NAR for each of them", len(fetched), len(nars))
    s = state()
    check("cache", "the running system is the built one", top(p, "S4"), s["cur"])
    check("cache", "the boot default is the built one", top(p, "S4"), s["prof"])
    check("cache", "one more commit", str(int(before["commits"]) + 1), s["commits"])
    conf = sh("cat /etc/nix/nix.conf", shown=False).out
    check("cache", "the new nix.conf holds the cache", True, f"extra-substituters = {CACHE_URL}" in conf and "test-cache-1:" in conf)
    again = basix("apply", "new cache, second apply")
    check("cache", "a second apply prints nothing to change, and it asks for no password", ["nothing to change", 0], [again.lines[-1], again.prompts])


def case_input(p: dict[str, dict], ctx: dict) -> None:
    before = state()
    put_state(states_cache["S5"])
    r = basix("apply", "one Input")
    check("input", "the apply exits 0", 0, r.rc)
    check("input", "the lines end with the save of the stub, the lock, the compiled file, and the entry", "saved: flake.lock, flake.nix, system.json, system.py", r.lines[-1] if r.lines else "")
    check("input", "the lines name no store path and no git command", True, no_leak(r.lines))
    log_text = sh("cat ~/basix/.git/basix/last.log", shown=False).out
    check("input", "apply built the input packages", True, ".#input-packages" in log_text)
    stub = sh("cat ~/basix/flake.nix", shown=False).out
    check("input", "the stub holds the input and its follows line", True, f'inputs.demo.url = "path:{DEMO}";' in stub and 'inputs.demo.inputs.nixpkgs.follows = "nixpkgs";' in stub)
    check("input", "the stub builds the input packages", True, "basix.lib.inputPackages {" in stub and 'modules = { demo = [ "demo" ]; };' in stub)
    lock = sh("cat ~/basix/flake.lock", shown=False).out
    check("input", "the lock holds the input", True, '"demo"' in lock and '"narHash"' in lock)
    classes = sh("ls ~/basix/.basix/pynix", shown=False).out.split()
    check("input", "the input package pynix_demo exists", ["pynix_demo"], classes)
    greeting = sh("cat /etc/demo/greeting", shown=False).out
    check("input", "the module of the input wrote its file with the typed values", "hi x2\n", greeting)
    s = state()
    check("input", "the running system is the built one", top(p, "S5"), s["cur"])
    check("input", "one more commit", str(int(before["commits"]) + 1), s["commits"])
    check("input", "the tree is clean and .basix stays out of the commit", "", s["dirty"])
    again = basix("apply", "one Input, second apply")
    check("input", "a second apply prints nothing to change", "nothing to change", again.lines[-1] if again.lines else "")
    log_again = sh("cat ~/basix/.git/basix/last.log", shown=False).out
    check("input", "a second apply builds the input packages again (010-D13)", True, ".#input-packages" in log_again)
    ctx["input_state"] = state()


def case_undo2(p: dict[str, dict], ctx: dict) -> None:
    start = state()
    r = basix("undo", "undo, clean tree")
    check("undo2", "the undo of a clean tree exits 0", 0, r.rc)
    check("undo2", "it says what it restores", True, "undo: restoring the files of the save before the last one" in r.lines)
    s = state()
    check("undo2", "one new commit", str(int(start["commits"]) + 1), s["commits"])
    check("undo2", "the commit is an undo", True, s["head"].startswith("undo to "))
    check("undo2", "system.py is the earlier entry", states_cache["S4"], sh("cat ~/basix/system.py", shown=False).out)
    check("undo2", "the earlier system runs", top(p, "S4"), s["cur"])
    check("undo2", "the input is gone from the machine", False, sh("test -e /etc/demo/greeting", shown=False).rc == 0)
    t = basix("undo", "undo, the toggle")
    check("undo2", "the toggle exits 0", 0, t.rc)
    s3 = state()
    check("undo2", "the toggle makes one more commit", str(int(s["commits"]) + 1), s3["commits"])
    check("undo2", "system.py is the input entry again", states_cache["S5"], sh("cat ~/basix/system.py", shown=False).out)
    check("undo2", "the input system runs again", top(p, "S5"), s3["cur"])
    check("undo2", "the file of the module is back", "hi x2\n", sh("cat /etc/demo/greeting", shown=False).out)
    # One save only.
    sh("rm -rf ~/solo && mkdir ~/solo && cd ~/solo && git init -q -b main && printf '.basix/\\n' > .gitignore && touch system.py && git add -A && git commit -q -m seed", shown=False)
    one = basix("undo", "undo, one save", where="~/solo")
    check("undo2", "one save: exit 1", 1, one.rc)
    check("undo2", "one save: the refusal line", ["There is only one save. There is nothing to undo."], one.lines)


def case_bootfail(p: dict[str, dict], ctx: dict) -> None:
    """The boot loader fails after the new system runs and the profile is set (the rollback of the finish step, L123)."""
    before = state()
    sh(f"touch {BOOT_DIR}/fail-boot", user="root", shown=False)
    try:
        put_state(states_cache["S4"])
        r = basix("apply", "the boot loader fails")
        check("bootfail", "the apply exits 3", 3, r.rc)
        text = "\n".join(r.lines)
        check("bootfail", "the lines say that the system did not switch", True, re.search(r"^problem: the system did not switch \(exit \d+\)$", text, re.MULTILINE) is not None)
        check("bootfail", "the earlier system is back, and nothing was saved", True, "failed: the earlier system is back. Nothing was saved." in text)
        check("bootfail", "the lines name no store path and no git command", True, no_leak(r.lines))
        s = state()
        check("bootfail", "the running system is the earlier one", before["cur"], s["cur"])
        check("bootfail", "the profile points to the earlier system again", before["prof"], s["prof"])
        check("bootfail", "the failed generation is gone", before["gens"], s["gens"])
        check("bootfail", "the boot default did not change", [before["boot"], before["boots"]], [s["boot"], s["boots"]])
        check("bootfail", "no commit", before["commits"], s["commits"])
        check("bootfail", "the edit stays in the folder", True, "system.py" in s["dirty"])
        check("bootfail", "no unit is failed", "0", s["failed"])
    finally:
        sh(f"rm -f {BOOT_DIR}/fail-boot", user="root", shown=False)
    u = basix("undo", "undo after the boot loader failed")
    check("bootfail", "the undo of the dirty tree exits 0", 0, u.rc)
    s = state()
    check("bootfail", "the tree is clean and the earlier system runs", ["", before["cur"]], [s["dirty"], s["cur"]])
    check("bootfail", "system.py is the input entry again", states_cache["S5"], sh("cat ~/basix/system.py", shown=False).out)


def case_reboot(p: dict[str, dict], ctx: dict) -> None:
    """After a successful apply, the new system is the boot default: it runs after a reboot."""
    before = state()
    boot_id = sh("cat /proc/sys/kernel/random/boot_id", user="root", shown=False).out.strip()
    reboot_vm()
    check("reboot", "the VM rebooted", True, sh("cat /proc/sys/kernel/random/boot_id", user="root", shown=False).out.strip() != boot_id)
    s = state()
    check("reboot", "the system of the last apply boots", before["cur"], s["cur"])
    check("reboot", "no unit is failed after the boot", "0", s["failed"])
    check("reboot", "the tree is clean and the commit is the last save", [before["dirty"], before["head"]], [s["dirty"], s["head"]])
    r = basix("apply", "apply after the reboot")
    check("reboot", "an apply after the reboot prints nothing to change", "nothing to change", r.lines[-1] if r.lines else "")


def case_explain(p: dict[str, dict], ctx: dict) -> None:
    r = basix("explain trusted_interfaces", "explain a field", where="~/explain")
    check("explain", "explain trusted_interfaces exits 0", 0, r.rc)
    text = "\n".join(r.lines)
    check("explain", "it prints the value", True, 'networking.firewall.trustedInterfaces = ["eth0", "lo"]' in text)
    check("explain", "it prints the learner line", True, re.search(r"set by trusted_interfaces at system\.py:\d+", text) is not None)
    q = basix("explain RemoteLogin", "explain a capability", where="~/explain")
    check("explain", "explain RemoteLogin exits 0", 0, q.rc)
    text = "\n".join(q.lines)
    check("explain", "it prints the value", True, "services.openssh.enable = true" in text)
    check("explain", "it prints the learner line", True, re.search(r"set by RemoteLogin at system\.py:\d+", text) is not None)
    check("explain", "no priority word", False, any(word in text.lower() for word in ("priority", "mkdefault", "mkforce", "override")))
    check("explain", "the lines name no store path", True, "/nix/store" not in text)
    bad = basix("explain trusted_interface", "explain a wrong name", where="~/explain")
    check("explain", "a wrong name exits 1 with a suggestion", True, bad.rc == 1 and "Did you mean `trusted_interfaces`?" in "\n".join(bad.lines))


def case_minix(p: dict[str, dict], ctx: dict) -> None:
    where = sh("command -v minix", title="command -v minix")
    check("minix", "command -v minix names the launcher of the system", "/run/current-system/sw/bin/minix", where.out.strip())
    body = sh("cat $(command -v minix)", shown=False).out
    check("minix", "the launcher holds the public URL", True, "git+https://github.com/Bullish-Intelligence/minix?ref=release" in body)
    check("minix", "the launcher holds no host of the owner", False, any(word in body for word in ("/home/", "/mnt/", "tailnet", "ts.net")))
    info("minix", "the run of the launcher", "not run: it needs the network (nix run of the public repository)")


def case_update(p: dict[str, dict], ctx: dict) -> None:
    before = state()
    sh(f"cd {SOURCE} && git checkout -q next && git log -1 --format=%s", title="a new Basix commit in the local repository")
    r = basix("update", "update")
    check("update", "the update exits 0", 0, r.rc)
    text = "\n".join(r.lines)
    check("update", "the lines name the old and the new release", True, f"Basix v{OLD_VERSION} -> v{NEW_VERSION}" in r.lines)
    check("update", "the lines give the age of the nixpkgs", True, re.search(r"^nixpkgs e7439b6b, \d+ days? old$", text, re.MULTILINE) is not None)
    check("update", "the lines say what to do next", "next: run basix apply to use it", r.lines[-1] if r.lines else "")
    check("update", "the lines name no store path and no git command", True, no_leak(r.lines))
    s = state()
    check("update", "the update did not switch", before["cur"], s["cur"])
    check("update", "the update changed the lock only", "M flake.lock", s["dirty"].strip(" ;"))
    a = basix("apply", "apply the update")
    check("update", "the apply exits 0", 0, a.rc)
    check("update", "the lines end with the save of the lock", "saved: flake.lock", a.lines[-1] if a.lines else "")
    s = state()
    check("update", "the machine moved to the new release", top(p, "S9"), s["cur"])
    check("update", "the boot default moved too", top(p, "S9"), s["prof"])
    command = sh("readlink -f /run/current-system/sw/bin/basix", shown=False).out.strip()
    check("update", "the command of the new system is the new release", True, command.rsplit("/", 3)[-3].endswith(f"basix-{NEW_VERSION}"))
    check("update", "one more commit", str(int(before["commits"]) + 1), s["commits"])


# --- the run ---------------------------------------------------------------------------------------------------
states_cache: dict[str, str] = {}
VM: subprocess.Popen | None = None
RUNNER: Path | None = None
RUN_ENV: dict[str, str] = {}


def start_vm() -> None:
    global VM
    assert RUNNER is not None
    console = open(LOGS / "qemu-console.log", "ab")
    VM = subprocess.Popen(
        [str(RUNNER)], env=RUN_ENV, cwd=WORK / "run", stdin=subprocess.DEVNULL, stdout=console, stderr=subprocess.STDOUT,
        start_new_session=True, preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_MEMLOCK, (65536, 65536)),
    )  # fmt: skip
    time.sleep(3)
    if VM.poll() is not None:
        raise SystemExit(f"the VM runner exited with {VM.returncode}; see {LOGS}/qemu-console.log")


def reboot_vm() -> None:
    """`systemctl reboot` in the VM. The VM may restart inside QEMU, or QEMU may exit; then start the runner again."""
    assert VM is not None
    sh("systemctl reboot", user="root", timeout=60, shown=False)
    end = time.time() + 120
    while ssh_alive() and time.time() < end:
        time.sleep(2)
    time.sleep(3)
    try:
        VM.wait(timeout=60)
    except subprocess.TimeoutExpired:
        raise SystemExit("the VM did not stop at the reboot") from None
    # The stand-in boot loader: the boot default is the system that the profile names.
    RUN_ENV["QEMU_KERNEL_PARAMS"] = "init=/nix/var/nix/profiles/system/init"
    log("   QEMU ended at the reboot; the runner starts again on the same disk image, with the boot default of the profile")
    start_vm()
    wait_ssh(300)
    log("   the VM is up again after the reboot")


def stage_vm(p: dict[str, dict], seed: Path, groups: list[str]) -> None:
    """Copy the sources, the lock inputs, and the store paths of the first states into the VM."""
    sh(f"mkdir -p {MIRROR} && chown {USER} {MIRROR}", user="root", shown=False)
    tar_to(SOURCE, str(SOURCE))
    tar_to(DEMO, str(DEMO))
    sh("git config --global user.name learner; git config --global user.email learner@basix; git config --global init.defaultBranch main", shown=False)
    tar_to(seed, "~/basix")
    tar_to(PREP / "explain", "~/explain", exclude=(".basix",))
    ssh_env = {**os.environ, "NIX_SSHOPTS": " ".join(SSHOPT)}
    t = time.time()
    # A source that the host fetched itself has no signature, so the VM takes it only with `--no-check-sigs` (as `copy_paths`).
    archive = ["flake", "archive", "--no-check-sigs", "--to", "ssh-ng://root@127.0.0.1", "--no-write-lock-file", str(PREP / "S0")]
    done = subprocess.run(["nix", *FEATURES, *archive], env=ssh_env, capture_output=True, text=True)
    log(f"== nix flake archive (the sources of the lock): rc={done.returncode}, {time.time() - t:.0f} s\n{done.stderr[-300:]}")
    if done.returncode != 0:
        raise SystemExit("nix flake archive failed: the VM has no sources of the lock, so every case would fail offline")
    copy_paths(sorted(p["S0"]["closure"] | p["S1"]["closure"] | p["S2"]["closure"]), "the compiler and the first states")
    # The system profile and the boot default of the running system: an installed machine has both.
    sh(f"nix-env -p /nix/var/nix/profiles/system --set $(readlink /run/current-system); mkdir -p {BOOT_DIR}; readlink /run/current-system > {BOOT_DIR}/boot-default", user="root", shown=False)


def copy_paths(paths: list[str], what: str) -> None:
    ssh_env = {**os.environ, "NIX_SSHOPTS": " ".join(SSHOPT)}
    t = time.time()
    done = subprocess.run(["nix", *FEATURES, "copy", "--no-check-sigs", "--to", "ssh-ng://root@127.0.0.1", *paths], env=ssh_env, capture_output=True, text=True)
    log(f"== nix copy of {len(paths)} paths ({what}): rc={done.returncode}, {time.time() - t:.0f} s\n{done.stderr[-300:]}")
    if done.returncode != 0:
        raise SystemExit("nix copy failed")
    SEEN.update(paths)


def drv_inputs(drv: str) -> set[str]:
    """The outputs of the input derivations of a derivation (the paths that a build of it needs)."""
    shown = json.loads(nix("derivation", "show", drv).stdout)["derivations"]
    wanted = next(iter(shown.values()))["inputs"]["drvs"]
    if not wanted:
        return set()
    inputs = json.loads(nix("derivation", "show", *[f"/nix/store/{name}" for name in wanted]).stdout)["derivations"]
    return {f"/nix/store/{inputs[name]['outputs'][output]['path']}" for name, spec in wanted.items() for output in spec["outputs"]}


def build_inputs(toplevel: str) -> set[str]:
    """The outputs of the input derivations of a system. The toplevel is never fetched from a cache
    (`allowSubstitutes = false`), so the machine that builds it needs these outputs in its store."""
    return drv_inputs(host(["nix-store", "-qd", toplevel]).stdout.strip())


def must_have(state_info: dict) -> set[str]:
    """The paths that a machine needs to build a system: its closure, and the closures of the inputs of the toplevel."""
    paths = set(state_info["closure"])
    for path in build_inputs(state_info["toplevel"]):
        paths |= closure(path)
    return paths


def fill_cache(p: dict[str, dict], secret: str) -> None:
    """The paths that only S4 needs go into a signed binary cache, which the unit `testcache` serves.

    The VM builds the toplevel itself (a NixOS toplevel is never fetched: `allowSubstitutes = false`), so it needs the
    outputs of the input derivations of S4. The ones that S1 needs too (the builder and its tools) are copied into the
    VM now. The ones that S4 adds (the new `etc`, the units, the activation script) are the paths that the new cache
    must give. Nix may also try to build those paths in the VM next to the fetch, so the VM holds the inputs of their
    derivations too: a build that starts then ends well, and the fetch is no race that the build can fail.
    `nix copy` writes a path only when the cache holds its references, so it copies the whole closure. Then the
    check drops every file but the ones for the new paths.
    """
    new = must_have(p["S4"]) - must_have(p["S1"]) - SEEN
    candidates = (must_have(p["S4"]) & must_have(p["S1"])) - SEEN
    for path in sorted(new):
        deriver = host(["nix-store", "-qd", path]).stdout.strip()
        if deriver.endswith(".drv"):
            for tool in drv_inputs(deriver):
                candidates |= closure(tool) - SEEN
    candidates -= new
    while moved := {tool for tool in candidates if closure(tool) & new}:  # a path that holds a new path is new too
        new |= moved
        candidates -= moved
    needed, tools = sorted(new), sorted(candidates)
    log(f"== the cache holds {len(needed)} paths that only S4 needs; the VM gets {len(tools)} build tools")
    copy_paths(tools, "the build tools of the S4 system")
    RESULTS["notes"]["cache build tools"] = len(tools)
    shutil.rmtree(CACHE, ignore_errors=True)
    nix("copy", "--to", f"file://{CACHE}?secret-key={secret}&compression=none", *needed)
    keep = {Path(path).name.split("-", 1)[0] for path in needed}
    for narinfo in CACHE.glob("*.narinfo"):
        if narinfo.stem in keep:
            continue
        found = re.search(r"^URL: (.+)$", narinfo.read_text(), re.MULTILINE)
        if found:
            (CACHE / found[1]).unlink(missing_ok=True)
        narinfo.unlink()
    left = sorted(path.stem for path in CACHE.glob("*.narinfo"))
    check("cache", "the host cache holds the files of the new paths only", sorted(keep), left)
    tar = subprocess.run(["tar", "-C", str(CACHE), "-cf", "-", "."], capture_output=True, check=True)
    done = subprocess.run(["ssh", *SSHOPT, "root@127.0.0.1", "tar -C /var/lib/testcache -xf -"], input=tar.stdout, capture_output=True)
    assert done.returncode == 0, done.stderr.decode()
    RESULTS["notes"]["cache paths"] = needed


def select_groups(text: str) -> list[str]:
    """The groups to run, from a comma list. Add the groups that a chosen group needs. Exit on an unknown name."""
    wanted = [name.strip() for name in text.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in GROUPS]
    if unknown or not wanted:
        raise SystemExit(f"unknown or empty group list {text!r}; known groups: {', '.join(GROUPS)}")
    chosen = set(wanted)
    for name in GROUPS:  # one pass in run order: the needs of a group come earlier in the list
        if name in chosen:
            for need in NEEDS.get(name, []):
                if need not in chosen:
                    print(f"group {name} needs {need}: added", flush=True)
                    chosen.add(need)
    return [name for name in GROUPS if name in chosen]


def main() -> int:
    global RAW, SSHOPT, RUNNER, RUN_ENV
    parser = argparse.ArgumentParser(
        description="The VM check opt:basix-apply. The default runs every group. A development run names only the groups "
        "that the change touched.",
        epilog="Groups run in the order shown, not the order given. The run adds a group that another needs: "
        + "; ".join(f"{name} needs {' and '.join(need)}" for name, need in NEEDS.items())
        + ".",
    )
    parser.add_argument(
        "--groups",
        "--cases",
        dest="cases",
        default=",".join(GROUPS),
        metavar="LIST",
        help="comma list of case groups to run (default: all). Groups: " + ", ".join(GROUPS),
    )
    parser.add_argument("--keep-vm", action="store_true", help="debug: leave the VM running at the end")
    parser.add_argument("--prepare-only", action="store_true", help="debug: prepare the sources, the states, and the VM only")
    args = parser.parse_args()
    groups = select_groups(args.cases)
    WORK.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    RAW = open(LOGS / "apply_vm.log", "w")
    if not KEY.exists():
        host(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(KEY), "-C", "basix-apply"])
    # `-F /dev/null`: read no ssh config file. Under Testee the store files show as owned by `nobody`, and ssh
    # refuses the system file that the host config includes ("Bad owner or permissions"). Every option is given here.
    SSHOPT = [
        "-F", "/dev/null", "-p", PORT, "-i", str(KEY), "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
    ]  # fmt: skip
    if ssh_alive():
        raise SystemExit(f"port {PORT} already answers: a VM from an earlier run is still alive")
    started = time.time()
    log(f"== groups {groups}; work {WORK}; logs {LOGS}")
    secret, cache_pub = make_key()
    states_cache.update(states(cache_pub))
    prepare_source()
    prepare_demo()
    pub = (WORK / "key.pub").read_text()
    prepared = prepare_states(pub, cache_pub)
    # The VM runner is the first state of the machine.
    runner_dir = WORK / "vm-runner"
    nix("build", "--no-warn-dirty", "--out-link", str(runner_dir), f"path:{PREP / 'S0'}#nixosConfigurations.{MACHINE}.config.system.build.vm")
    RUNNER = next((runner_dir / "bin").glob("run-*-vm"))
    SEEN.update(prepared["S0"]["closure"])
    seed = WORK / "seed"
    shutil.rmtree(seed, ignore_errors=True)
    shutil.copytree(PREP / "S0", seed, ignore=shutil.ignore_patterns(".git", ".basix"), symlinks=True)
    host(["git", "init", "-q", "-b", "main"], cwd=seed)
    host(["git", "add", "-A"], cwd=seed)
    host(["git", "commit", "-q", "-m", "seed"], cwd=seed)
    log(f"== prepared in {time.time() - started:.0f} s; runner {RUNNER}")
    if args.prepare_only:
        return 0
    run_dir = WORK / "run"
    shutil.rmtree(run_dir, ignore_errors=True)
    (run_dir / "tmp").mkdir(parents=True)
    RUN_ENV = {
        **os.environ,
        "NIX_DISK_IMAGE": str(run_dir / "vm.qcow2"),
        "TMPDIR": str(run_dir / "tmp"),
        "QEMU_NET_OPTS": f"hostfwd=tcp:127.0.0.1:{PORT}-:2222,restrict=on",
        "NO_COLOR": "1",
        "QEMU_OPTS": "-no-reboot",  # a reboot ends QEMU, so the next boot can name the boot default
    }
    ctx: dict = {}
    try:
        start_vm()
        wait_ssh()
        log(f"== VM up after {time.time() - started:.0f} s")
        stage_vm(prepared, seed, groups)
        RESULTS["times"]["prepare and stage"] = round(time.time() - started, 1)
        cases = {
            "cycles": lambda: case_cycles(prepared),
            "env": lambda: case_env(prepared),
            "apply": lambda: case_apply(prepared),
            "fail": lambda: case_fail(prepared, ctx),
            "undo": lambda: case_undo(prepared, ctx),
            "guards": lambda: case_guards(prepared, ctx),
            "cache": lambda: (fill_cache(prepared, secret), case_cache(prepared, ctx)),
            "input": lambda: (copy_paths(sorted(prepared["S5"]["closure"] | prepared["S9"]["closure"]), "the input state and the update"), case_input(prepared, ctx)),
            "undo2": lambda: case_undo2(prepared, ctx),
            "bootfail": lambda: case_bootfail(prepared, ctx),
            "explain": lambda: case_explain(prepared, ctx),
            "minix": lambda: case_minix(prepared, ctx),
            "update": lambda: case_update(prepared, ctx),
            "reboot": lambda: case_reboot(prepared, ctx),
        }
        for group in GROUPS:
            if group not in groups:
                continue
            log(f"\n== case {group}")
            try:
                cases[group]()
            except Exception as error:  # a crashed case must not stop the session
                import traceback

                check(group, f"the case crashed: {type(error).__name__}", "no crash", str(error)[:300], ok=False)
                log(traceback.format_exc())
    finally:
        if VM is not None and not args.keep_vm:
            subprocess.run(["ssh", *SSHOPT, "root@127.0.0.1", "poweroff"], capture_output=True)
            try:
                VM.wait(timeout=60)
            except subprocess.TimeoutExpired:
                os.killpg(VM.pid, signal.SIGTERM)
        host(["git", "checkout", "-q", "main"], cwd=SOURCE, check_rc=False)
    failed = [c for c in RESULTS["checks"] if not c["ok"]]
    RESULTS["total_s"] = round(time.time() - started, 1)
    RESULTS["toplevels"] = {k: v["toplevel"] for k, v in prepared.items() if "toplevel" in v}
    (LOGS / "results.json").write_text(json.dumps(RESULTS, indent=2) + "\n")
    log(f"\n{len(RESULTS['checks']) - len(failed)} of {len(RESULTS['checks'])} checks ok, {len(failed)} failed, {RESULTS['total_s']} s")
    for item in failed:
        log(f"FAIL [{item['case']}] {item['check']}: expected {item['expected']!r}, actual {str(item['actual'])[:300]!r}")
    log("PASS" if not failed else "FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
