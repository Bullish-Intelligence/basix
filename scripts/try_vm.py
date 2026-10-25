#!/usr/bin/env python3
"""Build and boot a throwaway VM from a Basix workspace."""

from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import tty
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import workdir  # noqa: E402
from pynix_engine import SubprocessRunner  # noqa: E402
from pynix_native import DiscoveryError, discover  # noqa: E402

from basix.units import ordering_cycles, summary
from basix_config.stub import INPUTS_ARGS, Stub, extra_inputs_expr, make_stub

ROOT = Path(__file__).resolve().parents[1]
STORE = "/nix/store/"  # Every output path of `nix build` starts with this.
STOP_WAIT_S = 30  # Seconds that QEMU gets to stop after SIGTERM. Then the script sends SIGKILL.


def work_root() -> Path:
    """Find the folder for VM files: `BASIX_TRY_WORK`, else the cache folder of the user."""
    return Path(os.environ.get("BASIX_TRY_WORK") or workdir.default_work("try"))


def logs_root() -> Path:
    """Find the folder for raw logs: `BASIX_TRY_LOGS`, else the state folder of the user."""
    return Path(os.environ.get("BASIX_TRY_LOGS", str(Path.home() / ".local/state/basix/try")))


def new_run_id() -> str:
    """Name one run. The time and the process number keep two runs apart."""
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"


def vm_module(user: str, screen: bool = False) -> str:
    """Make the throwaway VM module with the workspace account."""
    return f"""
{{ lib, modulesPath, pkgs, ... }}:
{{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
  users.users."{user}" = {{
    isNormalUser = true;
    initialPassword = "basix-try";
    extraGroups = [ "wheel" ];
  }};
  security.sudo.wheelNeedsPassword = lib.mkForce false;
  services.getty.autologinUser = "{user}";
  boot.kernelParams = [ "console=ttyS0" ];
  boot.initrd.availableKernelModules = [ "virtio_blk" "virtio_pci" "virtio_net" ];
  virtualisation.vmVariant.virtualisation = {{
    graphics = {str(screen).lower()};
    memorySize = 2048;
    cores = 2;
  }};
}}
"""


def workspace_for(path: Path) -> Path:
    """Find the workspace at `path` or above it."""
    path = path.resolve()
    for folder in (path, *path.parents):
        if (folder / "system.py").is_file():
            return folder
    raise ValueError(f"no system.py in {path} or its parents")


def machine_user(compiled: dict) -> str:
    """Read the account from the compiled Basix fragment."""
    for fragment in compiled.get("fragments", []):
        if fragment.get("path") == ["basix"]:
            user = fragment.get("value", {}).get("user")
            if isinstance(user, str) and user:
                return user
    raise ValueError("compiled system has no basix.user account")


def flake_text(repo: Path, home: Path | None = None, home_user: str | None = None, stub: Stub | None = None) -> str:
    """Make a temporary flake with separate Basix and optional minix workspace inputs.

    `stub` is the stub that `make_stub` made from the calls of `system.py`. Its input lines and its `extraInputs` make
    the flake read an `Input(...)` the same way as the stub of `basix apply`. None means a machine with no input.
    """
    repo_path = str(repo).replace('"', '\\"')
    stub_inputs = stub.inputs if stub else ()
    home_input = f'  inputs.home.url = "path:{home}";\n' if home is not None else ""
    input_lines = "".join(f"  {line}\n" for line in stub_inputs)
    names = ", ".join(["self", "basix", *(["home"] if home is not None else [])])
    inputs = INPUTS_ARGS.replace("self, basix", names) if stub_inputs else "{ " + names + " }"
    package_lines = "".join(f"    {line}\n" for line in stub.packages) if stub else ""
    skipped = ["home"] if home is not None else []
    extra_inputs = f"      extraInputs = {extra_inputs_expr(*skipped)};\n" if stub_inputs else ""
    home_binding = f'    homeActivation = home.homeConfigurations."{home_user}".activationPackage;\n' if home else ""
    bindings = f"  let\n{home_binding}  in\n" if home else ""
    home_module = ""
    if home is not None and home_user is not None:
        home_module = """
    ({ ... }: {
      systemd.user.services.minix-try-home = {
        wantedBy = [ "default.target" ];
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          Environment = "PATH=/run/current-system/sw/bin";
          ExecStart = "${homeActivation}/activate";
        };
      };
    })
"""
    return f"""{{
  inputs.basix.url = "path:{repo_path}";
{input_lines}{home_input}  outputs = {inputs}:\n{bindings}  {{
{package_lines}    nixosConfigurations = basix.lib.machine {{
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
{extra_inputs}      extraModules = [ ./vm.nix {home_module} ];
    }};
  }};
}}
"""


def run(argv: list[str], *, cwd: Path, log: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run one command and save its complete output."""
    log.parent.mkdir(parents=True, exist_ok=True)
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        log.write_text(f"$ {shlex.join(argv)}\nexit-code: timeout after {timeout}s\n{error.stderr or ''}\n")
        raise
    log.write_text(f"$ {shlex.join(argv)}\n--- stdout ---\n{done.stdout}--- stderr ---\n{done.stderr}")
    log.with_suffix(".cmd").write_text(
        json.dumps({"argv": argv, "cwd": str(cwd), "returncode": done.returncode, "timeout_s": timeout}, indent=1)
        + "\n"
    )
    return done


def lock_inputs(scratch: Path, stub: Stub, logs: Path) -> int:
    """Write the stub with the inputs, lock them, and build the input packages. Return 0, or the exit code to stop with.

    This is the order of `basix apply` (`basix.compile`). The lock may fetch an input, so it runs online.
    """
    (scratch / "flake.nix").write_text(stub.text)
    lock_log = logs / "inputs-lock.log"
    locked = run(["nix", "flake", "lock", "--no-warn-dirty"], cwd=scratch, log=lock_log, timeout=900)
    if locked.returncode:
        print(f"basix-try: input lock failed; see {lock_log}", file=sys.stderr)
        return locked.returncode
    packages_log = logs / "input-packages.log"
    link = scratch / ".basix" / "pynix"
    link.parent.mkdir(exist_ok=True)
    built = run(
        ["nix", "build", "--out-link", str(link), "--no-warn-dirty", f"path:{scratch}#input-packages"],
        cwd=scratch,
        log=packages_log,
        timeout=3600,
    )
    if built.returncode:
        print(f"basix-try: input packages build failed; see {packages_log}", file=sys.stderr)
    return built.returncode


def home_needs_packages(flake_text: str) -> bool:
    """Decide whether the home workspace needs its input packages (`.basix/pynix`) built again.

    The stub of minix defines the output `input-packages` only when `home.py` has an `Input`. `minix apply` builds the
    packages on every run. `basix-try` does the same: the copy of the home workspace holds the link from the last
    `minix apply`, and a lock that changed since then makes that package stale (011 bug 3). A build of packages that are
    still current costs a few seconds.
    """
    return "input-packages" in flake_text


def build_home_packages(home: Path, logs: Path) -> int:
    """Build the input packages of the home workspace into `.basix/pynix`. Return 0, or the exit code to stop with."""
    link = home / ".basix" / "pynix"
    link.parent.mkdir(exist_ok=True)
    log = logs / "home-input-packages.log"
    built = run(
        ["nix", "build", "--out-link", str(link), "--no-warn-dirty", f"path:{home}#input-packages"],
        cwd=home,
        log=log,
        timeout=3600,
    )
    if built.returncode:
        print(f"basix-try: home input packages build failed; see {log}", file=sys.stderr)
    return built.returncode


def stop_vm(vm: subprocess.Popen[bytes]) -> None:
    """Stop QEMU with SIGTERM. When it is still alive after `STOP_WAIT_S` seconds, send SIGKILL."""
    if vm.poll() is None:
        os.killpg(vm.pid, signal.SIGTERM)
    try:
        vm.wait(timeout=STOP_WAIT_S)
    except subprocess.TimeoutExpired:
        print(f"basix-try: QEMU still ran after {STOP_WAIT_S} s; sent SIGKILL", file=sys.stderr, flush=True)
        try:
            os.killpg(vm.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        vm.wait()


def boot(runner: Path, run_dir: Path, vm_log_path: Path) -> int:
    """Start the VM runner with the terminal as its serial line. Return the exit code.

    The VM gets a short `TMPDIR` in `/tmp`. QEMU makes a socket path under it, and a Unix socket path must be
    shorter than 108 bytes. A `TMPDIR` in `BASIX_TRY_WORK` can be too long. The folder goes away when the run ends.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    vm_tmp = Path(tempfile.mkdtemp(prefix="bt-", dir="/tmp"))
    try:
        return _boot(runner, run_dir, vm_log_path, vm_tmp)
    finally:
        shutil.rmtree(vm_tmp, ignore_errors=True)


def _boot(runner: Path, run_dir: Path, vm_log_path: Path, vm_tmp: Path) -> int:
    """Run `boot` with a ready `TMPDIR` folder."""
    env = {
        **os.environ,
        "NIX_DISK_IMAGE": str(run_dir / "vm.qcow2"),
        "TMPDIR": str(vm_tmp),
    }
    print(f"basix-try: booting {runner}; logs: {vm_log_path}", flush=True)
    master, slave = pty.openpty()
    vm = subprocess.Popen(
        [str(runner)],
        cwd=run_dir,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    input_fd = sys.stdin.fileno()
    old_tty = termios.tcgetattr(input_fd) if os.isatty(input_fd) else None
    stopped_by_user = False
    if old_tty is not None:
        tty.setraw(input_fd)
    try:
        with vm_log_path.open("wb") as output_log:
            while vm.poll() is None:
                readers = [master, input_fd] if old_tty is not None else [master]
                ready, _, _ = select.select(readers, [], [], 0.5)
                if master in ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError as error:
                        if error.errno == errno.EIO:
                            break
                        raise
                    if not data:
                        break
                    output_log.write(data)
                    output_log.flush()
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                if old_tty is not None and input_fd in ready:
                    data = os.read(input_fd, 4096)
                    if data == b"\x03":
                        os.killpg(vm.pid, signal.SIGTERM)
                        stopped_by_user = True
                        break
                    os.write(master, data)
    finally:
        if old_tty is not None:
            termios.tcsetattr(input_fd, termios.TCSADRAIN, old_tty)
        try:
            stop_vm(vm)
        finally:
            os.close(master)
    return 130 if stopped_by_user else (vm.returncode or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and boot a throwaway VM from a Basix workspace.",
        epilog="Paths: BASIX_TRY_WORK holds the VM files, BASIX_TRY_LOGS holds the raw logs. Each run gets its own folder.",
    )
    parser.add_argument("--dir", type=Path, default=Path.cwd(), help="The Basix workspace (default: current folder).")
    parser.add_argument("--home", type=Path, help="A minix workspace to include as the home input.")
    parser.add_argument("--screen", action="store_true", help="Show a graphical VM window.")
    args = parser.parse_args(argv)

    try:
        system_ws = workspace_for(args.dir)
        home_ws = args.home.resolve() if args.home else None
        if home_ws is not None:
            for name in ("home.py", "flake.nix", "flake.lock"):
                if not (home_ws / name).is_file():
                    raise ValueError(f"--home: {home_ws} has no {name}")
    except ValueError as error:
        print(f"basix-try: {error}", file=sys.stderr)
        return 2

    # One folder per run, in the work tree and in the log tree. A second run never deletes the files of the first.
    run_id = new_run_id()
    work = work_root() / run_id
    logs = logs_root() / run_id
    scratch = work / "workspace"
    home_scratch = work / "home"
    run_dir = work / "run"
    scratch.mkdir(parents=True)
    logs.mkdir(parents=True)
    print(f"basix-try: run {run_id}; files: {work}; logs: {logs}", flush=True)
    shutil.copytree(
        system_ws,
        scratch,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", ".jj", ".basix", ".devenv", ".direnv", "__pycache__"),
    )
    for generated in ("flake.nix", "flake.lock", "system.json"):
        (scratch / generated).unlink(missing_ok=True)

    # An `Input(...)` or `NixpkgsPin(...)` in `system.py` needs the same steps as `basix apply`: the stub with the inputs,
    # a lock, and the input packages in `.basix/pynix`. The compiler reads that folder.
    try:
        stub = make_stub(discover(scratch / "system.py"), f"path:{ROOT}")
    except (DiscoveryError, ValueError) as error:
        print(f"basix-try: {error}", file=sys.stderr)
        return 2
    for note in stub.notes:
        print(f"basix-try: {note}", flush=True)
    if stub.inputs:
        code = lock_inputs(scratch, stub, logs)
        if code:
            return code

    compiler_log = logs / "compiler.log"
    compiler_build = run(
        [
            "nix",
            "build",
            f"path:{ROOT}#packages.x86_64-linux.compiler",
            "--no-link",
            "--print-out-paths",
            "--no-warn-dirty",
        ],
        cwd=ROOT,
        log=compiler_log,
        timeout=3600,
    )
    if compiler_build.returncode:
        print(f"basix-try: compiler build failed; see {compiler_log}", file=sys.stderr)
        return compiler_build.returncode
    compiler_path = next((line for line in compiler_build.stdout.splitlines() if line.startswith(STORE)), None)
    if compiler_path is None:
        print(f"basix-try: no compiler path in {compiler_log}", file=sys.stderr)
        return 1
    compile_log = logs / "compile.log"
    compiled = run(
        [f"{compiler_path}/bin/basix-compile", "system.py", "system.json"], cwd=scratch, log=compile_log, timeout=900
    )
    if compiled.returncode:
        print(f"basix-try: compile failed; see {compile_log}", file=sys.stderr)
        return compiled.returncode

    # The account is known now, before the slow minix build. A machine with no user cannot take a home.
    compiled_json = json.loads((scratch / "system.json").read_text())
    try:
        account = machine_user(compiled_json)
    except ValueError as error:
        if home_ws is not None:
            print("basix-try: --home needs a machine with a user; system.py sets no user", file=sys.stderr)
        else:
            print(f"basix-try: {error}", file=sys.stderr)
        return 2

    home_user = None
    if home_ws is not None:
        shutil.copytree(
            home_ws,
            home_scratch,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".jj", ".devenv", ".direnv", "__pycache__"),
        )
        if home_needs_packages((home_scratch / "flake.nix").read_text()):
            code = build_home_packages(home_scratch, logs)
            if code:
                return code
        home_compiler_log = logs / "home-compiler.log"
        home_compiler_build = run(
            [
                "nix",
                "build",
                f"path:{home_scratch}#packages.x86_64-linux.compiler",
                "--no-link",
                "--print-out-paths",
                "--no-warn-dirty",
            ],
            cwd=home_scratch,
            log=home_compiler_log,
            timeout=3600,
        )
        if home_compiler_build.returncode:
            print(f"basix-try: minix compiler build failed; see {home_compiler_log}", file=sys.stderr)
            return home_compiler_build.returncode
        home_compiler_path = next(
            (line for line in home_compiler_build.stdout.splitlines() if line.startswith(STORE)), None
        )
        if home_compiler_path is None:
            print(f"basix-try: no minix compiler path in {home_compiler_log}", file=sys.stderr)
            return 1
        home_compile_log = logs / "home-compile.log"
        home_compiled = run(
            [f"{home_compiler_path}/bin/minix-compile", "home.py", "home.json"],
            cwd=home_scratch,
            log=home_compile_log,
            timeout=900,
        )
        if home_compiled.returncode:
            print(f"basix-try: minix compile failed; see {home_compile_log}", file=sys.stderr)
            return home_compiled.returncode
        home_user = json.loads((home_scratch / "home.json").read_text())["user"]
        if home_user != account:
            print(f"basix-try: --home names {home_user!r}, but system.py names {account!r}", file=sys.stderr)
            return 2
    (scratch / "vm.nix").write_text(vm_module(account, args.screen))
    (scratch / "flake.nix").write_text(flake_text(ROOT, home_scratch if home_user else None, home_user, stub))

    lock_log = logs / "flake-lock.log"
    locked = run(["nix", "flake", "lock", "--offline", "--no-warn-dirty"], cwd=scratch, log=lock_log, timeout=900)
    if locked.returncode:
        print(f"basix-try: flake lock failed; see {lock_log}", file=sys.stderr)
        return locked.returncode

    vm_log = logs / "build-vm.log"
    vm_attr = f"nixosConfigurations.{compiled_json['name']}.config.system.build.vm"
    drv_log = logs / "vm-drvpath.log"
    evaluated = run(
        ["nix", "eval", "--raw", f"path:{scratch}#{vm_attr}.drvPath"], cwd=scratch, log=drv_log, timeout=900
    )
    if evaluated.returncode:
        print(f"basix-try: VM evaluation failed; see {drv_log}", file=sys.stderr)
        return evaluated.returncode
    drv_path = evaluated.stdout.strip()
    print(f"basix-try: VM drvPath {drv_path}", flush=True)
    built = run(
        ["nix", "build", "--no-link", "--print-out-paths", "--no-warn-dirty", f"path:{scratch}#{vm_attr}"],
        cwd=scratch,
        log=vm_log,
        timeout=7200,
    )
    if built.returncode:
        print(f"basix-try: VM build failed; see {vm_log}", file=sys.stderr)
        return built.returncode
    output_lines = built.stdout.splitlines()
    output = next((Path(line) for line in output_lines if line.startswith(STORE)), None)
    if output is None:
        print(f"basix-try: no VM output path in {vm_log}", file=sys.stderr)
        return 1
    cycles = ordering_cycles(SubprocessRunner(), output / "system")
    if cycles:
        print(
            f"basix-try: the system has {len(cycles)} systemd ordering cycle(s), so the VM would boot in a random order.\n"
            f"{summary(cycles)}\nFix the order of these units. The VM did not start.",
            file=sys.stderr,
        )
        return 1
    runner = next((output / "bin").glob("run-*-vm"), None)
    if runner is None:
        print(f"basix-try: no VM runner in {output}", file=sys.stderr)
        return 1

    code = boot(runner, run_dir, logs / "qemu.log")
    # The VM disk is throwaway. Delete it, and keep the scratch workspace and the logs of this run.
    shutil.rmtree(run_dir, ignore_errors=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
