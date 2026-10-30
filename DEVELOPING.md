# Developing Basix

This file holds the Basix product rules, the learner commands, the checks, the fast loop, the release order, and the
repository layout. [`AGENTS.md`](AGENTS.md) holds the general rules. When the folder `.loci` exists, `.loci/CURRENT.md`
names the active project.

## What Basix is

Basix is a distribution for people who learn computing on their own machines. It is not a service that the owner runs.

Basix v2 has two layers. Each layer has its own workspace, command, and lock.

| Layer | Repository | Entry file | Result |
| --- | --- | --- | --- |
| Basix | this repository | `system.py` | A NixOS machine |
| minix | `https://github.com/Bullish-Intelligence/minix` | `home.py` | A standalone Home Manager home for one account |

Both entry files are Python. They use Pydantic types that the repository `pynix` generates from the options of NixOS
and Home Manager. The learner path holds no Vendomat and no devenv. devenv is the shell of the developer only.

## Product rules

- Basix is minimal, functional, and usable first. User text is a short overview of where a change goes, not a lesson.
  Nix stays hidden from the learner. An error is one plain line with the file, the line, the column, and the value.
- Never hard-wire a personal host, network name, binary cache, path, or secret into a shipped file.
- A change goes to the layer by one rule: a thing that needs root, or a thing of the whole machine, goes in
  `system.py`. Everything else goes in `home.py`. No capability spans both layers.
- Basix emits NixOS only. minix emits Home Manager only. They do not depend on each other.
- Python runs at compile time only, never inside Nix evaluation. `basix apply` compiles `system.py` to `system.json`.
  A staleness guard stops an evaluation when `system.py` or a file that it read has changed since the compile.
- The learner word for a named function is "capability" (`RemoteLogin()`). The learner writes `pkgs.NAME` for a package.
- Python code from outside the workspace enters only through `Input(name, url, python="FOLDER")` (021-D1). The lock pins
  the code. It may import the standard library, `pydantic`, `nixdantic`, `pynix_pkgs`, `pynix_native`, `basix_config`,
  and `pynix_os`. No other third-party package is supported. A package that both layers use must not read the other
  layer: its shared part imports neither `basix_config` nor `minix_config`.
- Basix publishes the pins of its `python=` inputs in `/etc/basix/python-pins.json` (021-D3, 021-D7). The file always
  exists: with no `python=` input, it holds an empty list. It is world-readable and holds the input URLs with
  credentials removed (021-D9). minix reads the file from the activated system and warns when a pin differs. Basix
  reads nothing of minix and prints no pin warning.
- A hatch for a setting that has no field: first a field, then `options=[...]` with a generated class, then
  `native_modules=[NativeModule("file.nix")]`.
- Basix owns `install` and the disk guard. Every disko disk keeps `destroy = false` and the guard hook. Evaluation fails
  when a disk lacks them. Do not weaken `modules/disk.nix` or `modules/disk-guard.sh` to pass a fixture. Change the
  guard file only with a new fixture run, and record the new hash in the evidence.
- The core ships Bluetooth and the `minix` launcher. It ships no cache server.
- A failed health check after `basix apply` leaves the earlier system as the boot default.
- After the build and before the switch, `basix apply` runs `systemd-analyze verify` on `default.target` of the built
  system (`src/basix/units.py`). A systemd ordering cycle makes systemd delete jobs at random at boot, and Nix builds
  such a system without an error. On a cycle, `apply` prints a `WARNING` that names the units, then goes on (010-D56,
  BSX-411). It has no override, so it does not refuse. `basix-try` and the VM checks refuse on a cycle. The check adds
  about 0.2 s to 0.6 s.
- No public binary cache exists, and none is planned.

The full design is in the project records (`.loci/projects/009-v2-concept/CONCEPT.md`, when the folder `.loci` exists).
The rules are in 009 DECISIONS and 010 DECISIONS in the same records. The repository holds no link into the records.

## Learner commands

The `basix` command (`src/basix/cli.py`) has five subcommands. Each takes `--dir` to name the workspace
(default: the current folder, else the folder in `BASIX_WORKSPACE`, else `~/basix`). A person who keeps the workspace
elsewhere sets `BASIX_WORKSPACE`. If the variable names a folder with no `system.py`, the command stops with a plain line.
Basix has no `init` command. Only `basix install` makes a workspace, and its folder is fixed (see the table). `minix init` does
read `MINIX_WORKSPACE`: it makes that folder and its missing parents, and else `~/minix`.

| Command | What it does |
| --- | --- |
| `basix install` | Runs from the live ISO as root. Asks for the machine name, account, password, time zone, and disk. Writes the workspace `~/basix` of the new account and installs. It never reads `BASIX_WORKSPACE`: the installer runs as root on the live ISO, and the variable of the new account does not exist yet. Refuses BIOS boot, no network, a small disk, and a used disk until the learner agrees to erase it. |
| `basix apply` | Compiles `system.py`, builds the system, asks for the password once, switches, checks the services, then saves the files in Git. It prints "nothing to change" when the system is current. |
| `basix undo` | Drops an edit that was not applied. Else it returns to the save before the last one. Then it applies. |
| `basix update [NAME]` | Moves the lock of Basix, or of one input of `system.py`. A stub that follows the `path:` copy of the installer moves to the release channel first, with one line that says so. It prints the old and new release and the changelog lines that touch the settings of the workspace. Run `basix apply` after it. |
| `basix explain NAME` | Prints the value of a field, a capability, or an option path, and the lines of `system.py` that set it. |

A gitman workspace is a Git linked worktree. `apply`, `undo`, and `update` work in it. Each worktree keeps its own log
and build links in its Git folder. All worktrees of one repository share one lock, because they change one machine. A
second command in any worktree prints the "busy" line until the first command ends.

`minix` has `init`, `apply`, `undo`, and `update`. The Basix core ships a `minix` launcher
(`modules/minix.nix`) that runs the public minix release, so `minix init` works before the home exists. It needs a
network. After the first `minix apply`, the command of the home takes over.

`basix-try` is not a learner command. See "Fast loop".

## Verification

```bash
testee verify
```

Run it from the repository root. Do not wrap it in `devenv shell`. It runs `check:lint`, `check:format`,
`check:types`, and `test:full`. Do not run two `testee verify` in this repository at the same time.

The `testee` program is private for now and gets a public release later. Until then, a contributor has no documented
gate.

| Task | Runs | Rough time |
| --- | --- | --- |
| `setup:sync` | `uv sync --locked`, and the link to the locked `pynix` source | once per slot |
| `check:lint` | `uv lock --check`, `ruff check src tests scripts`, `scripts/check_docs.py`, `scripts/check_pins.py` (self-test and layout), ShellCheck | 2 s |
| `check:format` | `ruff format --check` | under 1 s |
| `check:types` | `ty check src tests` | 2 s |
| `test:quick` | `pytest -n 4` (profile `quick` only) | 7 s |
| `test:full` | `pytest -n 4`, then `scripts/check_eval.py` (compiles the fixtures and evaluates them through `lib.machine`; includes the T3 anchor) | 60 s |

A whole `testee verify` takes about 75 to 90 s with cold verdicts. Testee reuses a verdict when a commit changes none of
the inputs of a task. The project records are outside the repository (`.loci`, ignored by Git), so a commit never changes them. Every task declares its inputs
(`input.testee`). Keep it so: a task without inputs cannot reuse a verdict.

`scripts/check_docs.py` fails on a broken relative Markdown link, a repeated `BSX-` row, or a table row with another
prefix. `scripts/check_pins.py` compares `flake.nix` with `flake.lock`, and the `nixpkgs` pin of `devenv.yaml` and
`devenv.lock` with them. It requires `pynix` as a tag. `scripts/check_public.py` scans the tracked files for owner facts (BSX-415): an e-mail address, a home folder, a path
under the vendor folder or a mount folder, a disk id with a serial, a private IP address, a tailnet name, a Git-protocol
URL, and a link into the records. The reviewed exceptions are in `scripts/public_allow.toml`. A private pattern file adds the owner
words: set `PUBLIC_LEAK_PATTERNS` to its path.

### Opt-in checks

The opt-in checks need `/dev/kvm` and build images. They never count for green. Run one by name, after
`testee verify`:

```bash
testee verify opt:basix-iso            # boot the test ISO under UEFI with no disk (about 60 s)
testee verify opt:basix-guard-vm       # the disk guard in a VM: refused disks and a blank pair (about 45 to 60 s)
testee verify opt:basix-install        # basix install in VMs: install, login, refusals, first offline apply (about 730 to 1000 s)
testee verify opt:basix-apply          # the real basix command in a VM with no network: apply, undo, update, explain, and the cycle check on real toplevels (about 600 s)
testee verify opt:basix-capabilities   # boot the console machine, RemoteLogin, and programs; run the check.nix cases (no time recorded; limit 7200 s)
testee verify opt:basix-desktop        # boot the desktop machine with a screen, greetd, and login (about 50 s)
testee verify opt:basix-release        # basix update from one scratch release to the next on an installed machine (about 410 to 430 s)
testee verify opt:basix-editor         # editor help in a fresh workspace: ty completion, hover, and type errors on system.py (about 15 s, no VM)
```

- Run an opt-in check when the change touches what it proves. A change to the installed system (core, profile,
  capability, pins) needs at least `opt:basix-capabilities` and `opt:basix-apply`. A change to the installer
  (`src/basix/install.py`, `iso/`, `nix/iso.nix`, `modules/disk.nix`, `modules/disk-guard.sh`) needs `opt:basix-iso`,
  `opt:basix-guard-vm`, and `opt:basix-install`. A change to the release job needs `opt:basix-release`.
- A change to the editor launcher (`nix/editor.nix`) or to the field help of `Machine` needs `opt:basix-editor`.
- Do not run a required gate beside a VM check. Do not run two VM checks at once.
- The VM tasks boot the test ISO `packages.iso-test`. `BASIX_ISO_ATTR=iso` selects the shipped ISO.
- `opt:basix-install` runs two scenarios at a time (`BASIX_PARALLEL=2`; `1` runs them in turn). Name a subset by
  scenario: `devenv shell -- python3 -I scripts/install_vm.py . nvme`. The scenarios are `nvme`, `bios`,
  `no-network`, `small-disk`, `used-disk`, and `cut-install`.
- The installed-disk cache serves `opt:basix-release`. The `nvme` scenario installs and stores the disk when its login
  checks pass. `opt:basix-release` boots a qcow2 overlay of a stored disk when its key matches. The key holds the ISO
  store path, the disk shape, the install answers, QEMU and OVMF, and a hash of the driver code. The log says
  `installed disk REUSED` or `installed disk FRESH`. `BASIX_FRESH_INSTALL=1` forces an install. Raise `DISK_FORMAT` in
  `scripts/install_vm.py` when a change alters the installed disk in a way that the key does not show.
- `nix/source.nix` holds the file lists of the Basix source that the ISO, the compiler, the editor launcher, and the
  Python package carry. An edit to a script, a test, a fixture, or a note does not change the ISO path and does not
  miss the cache. The lists leave out `__pycache__`, `*.pyc`, tool caches, editor files, and `result` links, so a stray
  file in a `path:` checkout cannot change the ISO. `check_eval.py` (step `source-trees`) proves this. The devenv and
  the scripts also write no bytecode (`PYTHONDONTWRITEBYTECODE`, `python -B`).
- VM files and scratch go under `$BASIX_WORK` (default `${XDG_CACHE_HOME:-~/.cache}/basix/NAME`; `scripts/workdir.py` decides; `BASIX_WORK_BASE` replaces the part before `NAME`). Testee passes no variable from your shell to a task, so write `export BASIX_WORK_BASE=...` and other machine facts in `~/.config/basix/dev.env`; the opt-in tasks source it. Raw logs go
  under `$BASIX_LOGS` (default `~/.local/state/basix/NAME/logs`). Check `btrfs filesystem usage /` before an image
  build (`df -h /` when `/` is not btrfs). The disk guard of the VM checks reads the same numbers.

## Fast loop

`basix-try` builds a Basix machine from a workspace and the working tree, and starts it in a throwaway VM. It uses no
disk and needs no reboot. It is a tool of the developer and ships to nobody.

```bash
devenv shell -- basix-try --dir fixtures/core            # compile that system.py with the compiler of this tree, then boot it
devenv shell -- basix-try --dir WS --home HOME_WS        # add a minix workspace as the home input
devenv shell -- basix-try --dir WS --screen              # open a graphical window
```

- It reads `system.py`, compiles it with the compiler of the working tree, and builds the VM. It handles an
  `Input(...)` the way `basix apply` does. It refuses to start the VM when the built system has a systemd ordering
  cycle (BSX-411).
- The VM logs in as the account of `system.py`. The console is the serial line in your terminal. Stop it with
  `poweroff`.
- Each run has its own folder under `$BASIX_TRY_WORK` (default `~/.cache/basix/try`) and `$BASIX_TRY_LOGS`
  (default `~/.local/state/basix/try`). The script deletes the VM image when the run ends. The scratch workspace and
  the logs stay.
- The login and the test password live in the scratch workspace only. A test checks that they do not reach a shipped
  file.

## Release order

The three repositories release in this order. Each release needs a GREEN `testee verify` in its repository.

1. Choose the `nixpkgs` revision of the next Basix release.
2. `pynix`: `scripts/release.sh` writes the tag `pynix-vX.Y.Z[-rc.N]`, generated at that revision. A `pynix` release
   needs no change since the previous tag (010-D53 removed that guard).
3. `minix`: pin that `pynix` tag, and release a tag `vX.Y.Z[-rc.N]` on the branch `release`.
4. Basix: pin the same `pynix` tag in `flake.nix` and `flake.lock`. Then run:

   ```bash
   devenv shell -- scripts/release.sh vX.Y.Z-rc.N
   ```

   The tag must match the version in `pyproject.toml`. The job checks `testee status`, the locks, and the pair of
   `pynix_os` with `nixpkgs`. It compares the pins with the newest minix tag (it warns only). It writes
   `provenance/inputs.json` and `CHANGELOG.md`, makes one release commit, and makes the annotated tag. It never
   pushes. It prints the push commands (`git push origin TAG`, then the branch).

Each repository checks only its own pair at release (Basix: `pynix_os` against `nixpkgs`; minix: `pynix_hm` against
Home Manager and `nixpkgs`). The cross-repository compare warns and blocks nothing.

The release channel is the remote `origin` on GitHub (`Bullish-Intelligence`). The release job pushes nothing. It prints
the push commands for the release tag and the release branch. The Basix release branch is `release`
(`BASIX_RELEASE_BRANCH` changes it). It follows v2 (010-D54). The managed stub of a learner workspace follows `release`
(`src/basix_config/stub.py`). The new repository has no alias branch (012-D5); `BASIX_RELEASE_ALIAS` names one if a
release needs it. Push no tag and no release branch without the owner's yes. A workspace of v1 that updates from
`release` meets v2 and needs the v1-to-v2 migration.

## Sister repositories

| Repository | Address | Holds |
| --- | --- | --- |
| `pynix` | `https://github.com/Bullish-Intelligence/pynix` | `nixdantic` (the runtime), the generated `pynix_os`, `pynix_hm`, `pynix_flake_parts`, and `pynix_pkgs`, `pynix_native`, `pynix_engine` (the stage, build, health check, commit, and undo steps that both commands use), the generator, and the release gate |
| `minix` | `https://github.com/Bullish-Intelligence/minix` | `minix_config`, the compiler package, `lib.home`, and the `minix` command (`init`, `apply`, `undo`, `update`) |

Basix and minix each pin a `pynix` tag with `flake = false` and read `"${pynix}/src"`. The pin URL is the public
GitHub URL. A developer can map it to a local clone with a Git `insteadOf` rule, so no tracked file names a private host. The
development venv reads the locked `pynix` source through a `.pth` file that `devenv.nix` writes after `uv sync`.
minix has its own opt-in tasks, `opt:minix-apply` and `opt:minix-update`. Run them in the minix repository.

## Repository layout

| Path | Holds |
| --- | --- |
| `flake.nix`, `flake.lock` | The pins and the outputs: `lib.machine`, `lib.inputPackages`, `packages.compiler`, `packages.basix`, `packages.iso`, and `packages.iso-test`. |
| `devenv.nix`, `devenv.yaml`, `devenv.lock` | The development project: the venv, the Testee tasks, and the script `basix-try`. `devenv.yaml` repeats the `nixpkgs` pin. A learner never reads these files. |
| `pyproject.toml`, `uv.lock` | The Python dependencies and tools. Commit `uv.lock`. `check:lint` fails on a stale lock. |
| `src/basix/` | The `basix` command: `cli.py`, `commands.py`, `install.py`, `compile.py`, `layer.py`, `explain.py`, `release.py`, `caches.py`, `nix.py`, `errors.py`, and `workspace.py`. |
| `src/basix_config/` | The typed library that the learner imports: `Machine`, `Console`, `Desktop`, `Port`, `Ports`, `Cache`, the stub writer, and the compiler. A capability class sits in `capabilities/` as a link to its folder. |
| `capabilities/NAME/` | One shipped capability: `component.py`, an optional `module.nix`, `check.nix` (VM cases), and `lesson.md` (one screen). |
| `profiles/NAME/` | One profile: `module.nix`, `check.nix`, and for `desktop` the shipped Hyprland and Noctalia files. |
| `modules/` | NixOS modules for every Basix machine: `core.nix`, `disk.nix`, `disk-guard.sh`, `live-off.nix`, `minix.nix`, `basix-command.nix`. |
| `lib/` | `machine.nix` (builds the NixOS machine from `system.json` and the guards), `guard.nix` (the staleness guard), `input-packages.nix`. |
| `nix/` | `compiler.nix`, `editor.nix` (the `ty` launcher), `python.nix` (uv2nix packages), `source.nix` (the source trees), `iso.nix` (the ISO and the test ISO), `vm.nix`. |
| `iso/`, `install/` | The files of the live system and the installer: `system.py`, `system.json`, and the hardware-report filter `drop.jq`. |
| `template/` | The workspace overview (`README.md`) and `system.py`. |
| `provenance/inputs.json`, `CHANGELOG.md` | Written by the release job. Do not edit them. |
| `fixtures/` | Fixture workspaces for `check_eval.py` and the VM checks. They ship to nobody. |
| `tests/` | The `pytest` suite and its cases. |
| `scripts/` | `check_docs.py`, `check_pins.py`, `check_eval.py` (gate); `iso_boot.py`, `guard_vm.py`, `install_vm.py`, `apply_vm.py`, `capabilities_vm.py`, `desktop_vm.py`, `release_vm.py`, `vmlib.py` (opt-in VM checks), `editor_check.py` (opt-in editor check); `try_vm.py` (`basix-try`); `release.sh`. |
| `.loci/` | Not tracked. A link to the project records of the owner (`CURRENT.md`, `AGENTS.md`, and `projects/NNN-name/`). linkman makes it. |

## Rules for the code

- The root `devenv.nix` is the development project, not an entry module. A learner workspace reads the managed
  `flake.nix` only. The learner never edits it.
- `modules/core.nix` takes its answers as options (`basix.user`, `basix.disk.device`, `basix.console`). The compiled
  `system.json` sets them. The core sets every value that a machine may change with `mkDefault`.
- The core has no SSH key and no password in the store.
- A pin lives in `flake.nix` and `flake.lock`. The `nixpkgs` pin repeats in `devenv.yaml` and `devenv.lock`. Change
  them in one commit. Every input follows the one `nixpkgs`.
- Keep the shell scripts ShellCheck-clean. Write the other code in Python 3.13, with `ruff` and `ty`, and test it.
