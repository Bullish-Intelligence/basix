"""Boot Basix machines from `lib.machine` and run the profile and capability checks (`opt:basix-capabilities`).

Usage: python3 -I scripts/capabilities_vm.py [ROOT] capabilities|programs
       (run by `testee verify opt:basix-capabilities`)

Each target is a fixture workspace in `$BASIX_WORK`: a `system.py`, a stub `flake.nix` (the form of CONCEPT
Appendix C), the compiled `system.json`, and the test VM module `fixtures/vmcheck.nix` (a serial console, an
automatic root login, and the virtio drivers). The machine is `vmcheck`. The check compiles `system.py` (see
`BASIX_COMPILER` below), builds `nixosConfigurations.vmcheck.config.system.build.vm` with the input `basix` replaced
by a clean copy of this repository, and boots it. No disk image of the host and no block device reaches QEMU; the VM
makes its own qcow2 file under `$BASIX_WORK`.

`capabilities`: for each profile with a `check.nix`, boot the machine with that profile and no capability and run the
`cases`. For each directory in `capabilities/`, boot the machine with the capability (the class is the CamelCase
form of the directory name: `remote-login` is `RemoteLogin()`) and run the `cases` of its `check.nix`; then boot it
without the capability and run the `gone` cases. The second boot stands in for "remove the name and apply". It is
not a proof of `basix undo`.

The field `together` of a `check.nix` (BSX-316, BSX-320) is a map from the name of another capability to a list of
cases (`together.editor = [ { name = "..."; run = "..."; } ];`). For each pair, the check boots one VM with both
capabilities and runs the cases of both fields. Two files that name each other make one pair, not two. A name in
`together` that is not a shipped capability, or that is the capability itself, is a FAIL. So is a pair with no
case. A file with no `together` field adds no boot.

`programs` (BSX-152): boot a machine with `programs=[Program(pkgs.ripgrep), Program(pkgs.hello)]` and check that
the programs are in the system path.

The compile: `BASIX_COMPILER=PATH` names a compiler that the check runs as `PATH system.py system.json` in the
workspace. By default the check builds `.#compiler` and runs its `bin/basix-compile`.

Each case is a script in the shared directory `xchg` of the VM. The guest runs it with `bash`. The shell
variable `BASIX_USER` holds the account name. A case passes when the script exits 0.

The check stops before a build when metadata is above 90 % and unallocated space is under 15 GiB
(`vmlib.disk_guard`). On a host whose `/` is not btrfs, it stops when `/` has under 15 GiB free.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vmlib  # noqa: E402

RESULTS: list[tuple[bool, str]] = []

VM_MACHINE = "vmcheck"
VM_USER = "learner"
VM_DISK = "/dev/disk/by-id/virtio-vd-target"
#: The files of the flake. The VM build and the compiler build read them.
FLAKE_FILES = ["flake.nix", "flake.lock", "pyproject.toml", "uv.lock", "README.md", "lib", "modules", "profiles", "capabilities", "nix", "src"]

STUB = """\
# A fixture stub in the form of CONCEPT Appendix C, with the test VM module and the `nixos` parts of the checks.
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }:
    let
      pkgs = basix.inputs.nixpkgs.legacyPackages.x86_64-linux;
      check = path: import "${basix}/${path}/check.nix" { inherit pkgs; };
    in {
      nixosConfigurations = basix.lib.machine {
        compiled = builtins.fromJSON (builtins.readFile ./system.json);
        source = ./system.py;
        root = ./.;
        extraModules = [ ./vmcheck.nix ] ++ map (path: (check path).nixos or { }) [ %(checks)s ];
      };
    };
}
"""


def report(ok: bool, text: str) -> bool:
    RESULTS.append((ok, text))
    print(("PASS " if ok else "FAIL ") + text, flush=True)
    return ok


def class_name(directory: str) -> str:
    """`remote-login` is `RemoteLogin`."""
    return "".join(part.capitalize() for part in directory.split("-"))


def stage_flake(logs: vmlib.Logs, repo: Path) -> Path:
    """Copy the files of the flake to a clean folder. The VM build and the check data read this copy."""
    target = vmlib.work_dir("capabilities/basix")
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


def system_py(capabilities: list[str], trusted: list[str], programs: list[str], profile: str) -> str:
    caps = ", ".join(f"{class_name(name)}()" for name in capabilities)
    progs = ", ".join(f"Program(pkgs.{name})" for name in programs)
    lines = [
        "from basix_config import *",
        "",
        "machine = Machine(",
        f'    name="{VM_MACHINE}",',
        f'    user="{VM_USER}",',
        f'    disk="{VM_DISK}",',
        f"    profile={class_name(profile)}(),",
        f"    capabilities=[{caps}],",
        f"    programs=[{progs}],",
        f"    trusted_interfaces={json.dumps(trusted)},",
        ")",
        "",
    ]
    return "\n".join(lines)


class Context:
    def __init__(self, logs: vmlib.Logs, repo: Path, env: dict[str, str]) -> None:
        self.logs = logs
        self.repo = repo
        self.env = env
        self.flake = stage_flake(logs, repo)
        self.compiler = compiler_command(logs, self.flake, env)


def prepare(
    ctx: Context,
    name: str,
    capabilities: list[str],
    checks: list[str],
    trusted: list[str] | None = None,
    programs: list[str] | None = None,
    profile: str = "console",
) -> tuple[Path, Path]:
    """Make the fixture workspace. Return (workspace, folder for the VM files).

    `checks` lists the directories (`capabilities/NAME` or `profiles/NAME`) whose `check.nix` adds a `nixos` part.
    """
    folder = vmlib.work_dir(f"capabilities/{name}")
    ws = folder / "ws"
    ws.mkdir()
    (ws / "system.py").write_text(system_py(capabilities, trusted or [], programs or [], profile))
    (ws / "flake.nix").write_text(STUB % {"checks": " ".join(f'"{c}"' for c in checks)})
    shutil.copy2(ctx.repo / "fixtures" / "vmcheck.nix", ws / "vmcheck.nix")
    done = ctx.logs.run(f"compile-{name}", [*ctx.compiler, "system.py", "system.json"], cwd=ws, env=ctx.env, timeout=900)
    if done.returncode != 0:
        raise vmlib.Stop(f"the compile failed ({name}):\n{done.stderr[-2500:]}")
    return ws, folder


def check_data(ctx: Context, path: str) -> dict:
    """The data of a `check.nix`: cases, gone, trustedInterfaces, together."""
    expression = (
        f'let f = builtins.getFlake "path:{ctx.flake}"; pkgs = f.inputs.nixpkgs.legacyPackages.x86_64-linux; '
        f"c = import {ctx.flake}/{path}/check.nix {{ inherit pkgs; }}; in "
        "{ cases = c.cases or [ ]; gone = c.gone or [ ]; trustedInterfaces = c.trustedInterfaces or [ ]; together = c.together or { }; }"
    )
    done = ctx.logs.run(f"check-data-{path.replace('/', '-')}", ["nix", "eval", "--impure", "--json", "--expr", expression], env=ctx.env, timeout=900)
    if done.returncode != 0:
        raise vmlib.Stop(f"cannot read the check of {path}:\n{done.stderr[-1500:]}")
    return json.loads(done.stdout)


def build_runner(ctx: Context, ws: Path, name: str) -> Path:
    attr = f"nixosConfigurations.{VM_MACHINE}.config.system.build.vm"
    vmlib.disk_guard(ctx.logs, f"nix build {attr} ({name})")
    before = vmlib.root_usage(ctx.logs)
    argv = [
        "nix", "build", "--no-link", "--print-out-paths", "--no-write-lock-file", "--no-warn-dirty",
        "--override-input", "basix", f"path:{ctx.flake}", f"path:{ws}#{attr}",
    ]
    done = ctx.logs.run(f"build-vm-{name}", argv, env=ctx.env, timeout=6000)
    after = vmlib.root_usage(ctx.logs)
    ctx.logs.note(f"build vm {name}: rc {done.returncode}; unallocated {before[0]:.2f} -> {after[0]:.2f} GiB")
    if done.returncode != 0:
        raise vmlib.Stop(f"the VM build failed ({name}):\n{done.stderr[-2500:]}")
    out = Path(done.stdout.strip().splitlines()[-1])
    vmlib.refuse_cycles(out / "system", f"the VM build ({name})")
    return next((out / "bin").glob("run-*-vm"))


def run_cases(logs: vmlib.Logs, label: str, runner: Path, folder: Path, env: dict[str, str], cases: list[dict]) -> None:
    """Boot the VM, run each case, and power off."""
    xchg = folder / "tmp" / "xchg"
    xchg.mkdir(parents=True, exist_ok=True)
    for index, case in enumerate(cases):
        (xchg / f"case-{index}.sh").write_text(f"export BASIX_USER={VM_USER}\n{case['run']}\n")
    run_env = dict(env)
    run_env["NIX_DISK_IMAGE"] = str(folder / "root.qcow2")
    # The runner uses `$TMPDIR/xchg` as the shared directory `/tmp/xchg` only when `USE_TMPDIR` is set.
    run_env["TMPDIR"] = str(folder / "tmp")
    run_env["USE_TMPDIR"] = "1"
    proc = console = None
    try:
        proc = subprocess.Popen(
            [str(runner)],
            env=run_env,
            cwd=folder,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            preexec_fn=vmlib.limit_memlock,
        )
        console = vmlib.Console(logs, label, proc=proc)
        console.wait_for(rf"\[root@{VM_MACHINE}:[^\]]*\]# ", 420)
        console.command("set +H")
        for index, case in enumerate(cases):
            rc, out = console.command(f"bash /tmp/xchg/case-{index}.sh 2>&1", 180)
            ok = report(rc == 0, f"{label}: {case['name']}")
            if not ok:
                logs.note(f"{label}: case `{case['run']}` exited {rc}; output:\n{out[-1500:]}")
        console.write(b"poweroff\r")
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            logs.note(f"{label}: the guest did not stop in 60 s; the check kills QEMU")
    finally:
        if console is not None:
            console.close()
        vmlib.stop_process(proc, logs, label)


#: BSX-152: a learner adds system programs with `programs=[Program(...)]` of `Machine`.
PROGRAMS = ["ripgrep", "hello"]
PROGRAMS_CASES = [
    {"name": "ripgrep is in the system path", "run": "test -x /run/current-system/sw/bin/rg && rg --version | grep -q '^ripgrep '"},
    {"name": "hello is in the system path and runs", "run": 'test -x /run/current-system/sw/bin/hello && test "$(hello)" = "Hello, world!"'},
    {"name": "the programs come from the system profile", "run": "readlink -f /run/current-system/sw/bin/rg | grep -q '^/nix/store/'"},
    {"name": "the learner account finds the programs", "run": 'su - "$BASIX_USER" -c "command -v rg && command -v hello"'},
    {"name": "a program that the machine does not list is absent", "run": "! command -v cowsay"},
    {"name": "the machine has no failed unit", "run": 'test "$(systemctl --failed --no-legend | wc -l)" = 0'},
]


def target_programs(ctx: Context) -> None:
    ws, folder = prepare(ctx, "programs", [], [], programs=PROGRAMS)
    runner = build_runner(ctx, ws, "programs")
    run_cases(ctx.logs, "programs", runner, folder, ctx.env, PROGRAMS_CASES)


def target_profile(ctx: Context, name: str) -> None:
    """Boot the machine with a profile and no capability, and run the `cases` of the profile's `check.nix`."""
    data = check_data(ctx, f"profiles/{name}")
    if not data["cases"]:
        report(False, f"profile {name}: check.nix lists no case")
        return
    ws, folder = prepare(ctx, f"profile-{name}", [], [f"profiles/{name}"], profile=name)
    runner = build_runner(ctx, ws, f"profile-{name}")
    run_cases(ctx.logs, f"profile-{name}", runner, folder, ctx.env, data["cases"])


def plan_pairs(data: dict[str, dict], known: list[str]) -> tuple[list[tuple[str, str, list[dict]]], list[str]]:
    """Read the `together` fields. Return (pairs, problems).

    `data` maps a capability name to its check data. `known` lists the shipped capability names.
    A pair is (first, second, cases) with the names in sorted order. The cases of both fields join.
    """
    cases: dict[tuple[str, str], list[dict]] = {}
    problems: list[str] = []
    for name in sorted(data):
        for other, entry in sorted(data[name].get("together", {}).items()):  # entry: the list of cases
            if other == name:
                problems.append(f"{name}: together names the capability itself")
            elif other not in known:
                problems.append(f'{name}: together names "{other}", which is not a shipped capability (known: {", ".join(known) or "none"})')
            else:
                cases.setdefault((min(name, other), max(name, other)), []).extend(entry)
    pairs = []
    for (first, second), joined in sorted(cases.items()):
        if joined:
            pairs.append((first, second, joined))
        else:
            problems.append(f"{first} and {second}: together lists no case")
    return pairs, problems


def target_pair(ctx: Context, first: str, second: str, cases: list[dict], data: dict[str, dict]) -> None:
    """Boot one machine with both capabilities and run the `together` cases of both files."""
    trusted = sorted({i for n in (first, second) for i in data[n]["trustedInterfaces"]})
    ws, folder = prepare(ctx, f"pair-{first}-{second}", [first, second], [f"capabilities/{first}", f"capabilities/{second}"], trusted=trusted)
    runner = build_runner(ctx, ws, f"pair-{first}-{second}")
    run_cases(ctx.logs, f"{first}+{second}", runner, folder, ctx.env, cases)


def target_capability(ctx: Context, name: str) -> dict:
    """Boot the machine with one capability, then without it. Return its check data (for the `together` fields)."""
    data = check_data(ctx, f"capabilities/{name}")
    if not data["cases"]:
        report(False, f"{name}: check.nix lists no case")
        return data
    trusted = data["trustedInterfaces"]  # the check names the trusted interfaces of its VM (Basix R13)
    ws, folder = prepare(ctx, name, [name], [f"capabilities/{name}"], trusted=trusted)
    runner = build_runner(ctx, ws, f"{name}-with")
    run_cases(ctx.logs, f"{name}-with", runner, folder, ctx.env, data["cases"])
    if data["gone"]:
        ws2, folder2 = prepare(ctx, f"{name}-without", [], [f"capabilities/{name}"], trusted=trusted)
        runner2 = build_runner(ctx, ws2, f"{name}-without")
        run_cases(ctx.logs, f"{name}-without", runner2, folder2, ctx.env, data["gone"])
    return data


def main(argv: list[str]) -> int:
    args = list(argv[1:])
    mode = args.pop() if args and args[-1] in {"capabilities", "programs"} else "capabilities"
    repo = Path(args[0] if args else ".").resolve()
    logs = vmlib.Logs.create(f"capabilities_vm_{mode}")
    env = vmlib.clean_env()
    env["TMPDIR"] = str(vmlib.work_dir("vmtmp"))
    try:
        logs.note(f"raw logs: {logs.root}")
        started = time.monotonic()
        ctx = Context(logs, repo, env)
        if mode == "programs":
            target_programs(ctx)
        else:
            names = sorted(p.name for p in (repo / "capabilities").iterdir() if (p / "check.nix").is_file())
            logs.note(f"capabilities: {names}")
            for profile in sorted(p.name for p in (repo / "profiles").iterdir() if (p / "check.nix").is_file()):
                target_profile(ctx, profile)
            data = {name: target_capability(ctx, name) for name in names}
            pairs, problems = plan_pairs(data, names)
            for problem in problems:
                report(False, problem)
            for first, second, cases in pairs:
                target_pair(ctx, first, second, cases, data)
            target_programs(ctx)
        logs.note(f"{mode}: {time.monotonic() - started:.0f} s")
    except vmlib.Stop as error:
        report(False, str(error))
    failed = [text for ok, text in RESULTS if not ok]
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed; logs {logs.root}")
    print("GREEN" if not failed else "RED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
