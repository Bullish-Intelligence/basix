# basix — the development project: the Python venv, the gate tasks, and the opt-in capability check.
#
# Run the gate from the repository root with the host command: `testee verify`.
# Run the opt-in check the same way: `testee verify opt:basix-capabilities`.
# Run other in-repo commands through here: `devenv shell -- <command>`.
#
# A learner workspace does not read this file. It reads the managed `flake.nix` (CONCEPT Appendix C).
{ pkgs, lib, ... }:

let
  python = pkgs.python313;
  # `-B`: write no `__pycache__` folder. `-I` ignores `PYTHONDONTWRITEBYTECODE`, so the flag is needed.
  py = "${python}/bin/python3 -I -B";

  # The `pynix` source, from the one locked node of `flake.lock` (010-D8). The Nix packages read the same node.
  # The venv lists no `pynix` dependency. A `.pth` file in the venv names `STORE/src`, so pytest, ty, and ruff see the
  # same `pynix` as the Nix packages. A lock without the node (the v1 lock) leaves the source out; the shell says so.
  lock = builtins.fromJSON (builtins.readFile ./flake.lock);
  pynixNode = lock.nodes.pynix or null;
  pynixSrc = if pynixNode == null then null else "${builtins.fetchTree pynixNode.locked}/src";
  # The venv variable `VIRTUAL_ENV` is set after `enterShell` runs, so the path comes from the devenv state folder.
  pynixPth = lib.optionalString (pynixSrc != null) ''
    venv="''${VIRTUAL_ENV:-''${DEVENV_STATE:-$DEVENV_ROOT/.devenv/state}/venv}"
    if [ -x "$venv/bin/python" ]; then
      site="$("$venv/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
      echo "${pynixSrc}" > "$site/pynix.pth"
    fi
  '';
  pynixMissing = lib.optionalString (pynixSrc == null) ''
    echo "basix: flake.lock holds no pynix node, so the venv cannot see pynix. Set PYTHONPATH to a pynix src folder." >&2
  '';

  # The Testee shell drops the host PATH and HOME. A task that runs Nix sets what it needs.
  # VM files and scratch go under ~/.cache/basix/ (`scripts/workdir.py`). BASIX_WORK overrides it.
  hostPath = ''
    export HOME="$(eval echo "~$(id -un)")"
    export PATH="/run/current-system/sw/bin:${pkgs.nix}/bin:${pkgs.git}/bin:$PATH:/etc/profiles/per-user/$(id -un)/bin"
  '';
  # An optional file `~/.config/basix/dev.env` (or under $XDG_CONFIG_HOME) sets machine facts for these tasks, for
  # example `export BASIX_WORK_BASE=/path/on/a/big/drive`. Testee passes no variable from the calling shell.
  hostEnv = slice: ''
    ${hostPath}
    if [ -f "''${XDG_CONFIG_HOME:-$HOME/.config}/basix/dev.env" ]; then
      . "''${XDG_CONFIG_HOME:-$HOME/.config}/basix/dev.env"
    fi
    if [ -z "''${BASIX_WORK:-}" ]; then
      BASIX_WORK="$(${py} "$DEVENV_ROOT/scripts/workdir.py" ${slice})"
    fi
    export BASIX_WORK
    mkdir -p "$BASIX_WORK/tmp"
    export TMPDIR="$BASIX_WORK/tmp"
    export BASIX_LOGS="''${BASIX_LOGS:-$HOME/.local/state/basix/${slice}/logs}"
    mkdir -p "$BASIX_LOGS"
  '';
  # A task that Testee runs. `timeout_s` is the limit in seconds. `ignores` lists the file patterns that
  # Testee leaves out of the inputs of the task, so a change to only those files reuses the verdict.
  # Every task declares its inputs, or Testee cannot reuse a verdict. An opt-in task never counts for green.
  # No opt-in task reads the project records (`.loci` is outside the tracked tree).
  optIn = { timeout_s, script, slice ? "v2", ignores ? [ ] }: {
    input.testee = { inherit timeout_s ignores; };
    exec = ''
      set -o pipefail
      cd "$DEVENV_ROOT"
      ${hostEnv slice}
      ${script}
    '';
  };
  # The documentation check. It reads the Markdown files and fails on a broken link or a bad ledger row.
  checkDocs = "${py} scripts/check_docs.py .";

  # Files that `check:format`, `check:types`, `test:quick`, and `test:full` never read. A task ignores them, so a
  # change to only these files reuses its verdict. Before you add a path, grep for it in the task commands and in
  # the files that they run. `check:lint` reads all of them: `check_docs.py` reads every Markdown file
  # and `check_pins.py` scans every file of `scripts/`. So `check:lint` ignores none of them.
  # Keep `scripts/try_vm.py` out of the list: a test loads it.
  notes = [ ];
  optScripts = [
    "scripts/iso_boot.py"
    "scripts/guard_vm.py"
    "scripts/install_vm.py"
    "scripts/capabilities_vm.py"
    "scripts/apply_vm.py"
    "scripts/desktop_vm.py"
    "scripts/release_vm.py"
    "scripts/editor_check.py"
    "scripts/vmlib.py"
  ];
  # `check:lint` alone runs these scripts. No test imports them. `check_public.py` reads the tracked files, so it reads its own
  # allow list `public_allow.toml` too.
  lintScripts = [ "scripts/check_docs.py" "scripts/check_pins.py" "scripts/check_public.py" "scripts/public_allow.toml" ];
  unread = notes ++ optScripts ++ lintScripts;
in
{
  # Python toolchain. The venv hosts the app and the dev group (pytest, ruff, ty).
  # Python 3.13: the interpreter of the Nix compiler package (008-Q7).
  languages.python = {
    enable = true;
    package = python;
    venv.enable = true;
    uv.enable = true;
  };

  # The venv holds Basix as an editable install, so every tool that imports it would write `__pycache__` folders into
  # `src/`. A `path:` flake copies them into the ISO and changes its hash. Write no bytecode.
  env.PYTHONDONTWRITEBYTECODE = "1";

  enterShell = ''
    ${pynixPth}
    ${pynixMissing}
  '';

  # The fast loop (proposed name). `devenv shell -- basix-try --help` lists the options. It builds a VM from
  # this working tree and starts it with the learner login. It is a tool of the Basix developer: no learner
  # workspace and no shipped file holds it. Each run keeps its VM files in a folder under $BASIX_TRY_WORK and
  # its raw logs in a folder under $BASIX_TRY_LOGS. The script reads the `Input(...)` calls of `system.py` with the
  # same library as `basix apply`, so it runs in the project venv (with the `pynix` link), not in the bare Python.
  scripts.basix-try.exec = ''
    ${hostPath}
    uv sync --quiet --locked --project "$DEVENV_ROOT"
    ${pynixPth}
    exec uv run --no-sync --project "$DEVENV_ROOT" python -I -B "$DEVENV_ROOT/scripts/try_vm.py" "$@"
  '';

  # The five standard tasks (project 22 ruling R10). Testee 0.8 runs them by name.
  # `testee verify` runs check:lint, check:format, check:types, and test:full.
  # `testee verify --profile quick` runs check:lint, check:format, and test:quick.
  # Every task declares `input.testee`, so Testee can reuse a verdict when a commit changes none of its inputs.
  tasks = {
    # Testee runs `setup:*` once before the selected checks. The venv needs the locked tools and the `pynix` link.
    # `uv sync` reads the lock, the project file, the readme and the `.gitignore` of the editable build (checked with
    # strace, 2026-10-21). The `pynix` link comes from `flake.lock`.
    "setup:sync" = {
      input.testee.reads = [ "uv.lock" "pyproject.toml" "README.md" ".gitignore" "flake.lock" ];
      exec = ''
        cd "$DEVENV_ROOT"
        uv sync --locked
        ${pynixPth}
      '';
    };

    # A stale `uv.lock` fails here, before the Nix package build does.
    # ShellCheck covers every shell script of the product (the disk guard, BSX-138, BSX-139).
    # The pin check scans every file of the repository, and the document check reads every Markdown file. So this task
    # ignores no note and no script. It ignores only the folder of the devenv state.
    "check:lint" = {
      input.testee.ignores = [ ".devenv/**" ];
      exec = ''
        cd "$DEVENV_ROOT"
        set -o pipefail
        status=0
        uv lock --check || status=1
        uv run --no-sync ruff check src tests || status=1
        ${checkDocs} || status=1
        ${py} scripts/check_pins.py --selftest || status=1
        ${py} scripts/check_pins.py . || status=1
        ${py} scripts/check_public.py --selftest || status=1
        ${py} scripts/check_public.py . || status=1
        for script in modules/*.sh scripts/*.sh install/*.sh; do
          [ -e "$script" ] || continue
          echo "shellcheck $script"
          ${pkgs.shellcheck}/bin/shellcheck "$script" || status=1
        done
        exit $status
      '';
    };

    "check:format" = {
      input.testee.ignores = unread;
      exec = ''
        cd "$DEVENV_ROOT"
        set -o pipefail
        uv run --no-sync ruff format --check src tests
      '';
    };

    "check:types" = {
      input.testee.ignores = unread;
      exec = ''
        cd "$DEVENV_ROOT"
        set -o pipefail
        uv run --no-sync ty check src tests
      '';
    };

    # The quick profile: the unit tests, four processes in parallel (about 7 s on 8 CPUs, measured 2026-10-21).
    # The profile runs `check:lint` beside it, and that task runs the documents and the pin checks. No Nix evaluation.
    "test:quick" = {
      input.testee.ignores = unread;
      exec = ''
        cd "$DEVENV_ROOT"
        set -e
        uv run --no-sync pytest -n 4 tests
      '';
    };

    # The full gate: the tests, then the Nix checks. The Nix checks run `nix`, which needs the host PATH.
    # `check:lint` runs `check_pins.py`. `check_eval.py` compiles the fixture workspaces and evaluates them through
    # `lib.machine`. Measured 2026-10-21 with a warm Nix store, for the whole task: 58 s (pytest `-n 4` 6 s,
    # `check_eval.py` 52 s with 8 jobs, 164 cases). The limit is 150 s: about twice the time, and room for a cold
    # evaluation.
    "test:full" = optIn {
      timeout_s = 150;
      # A change to only the project notes or to only a script of an opt-in task does not change what these checks read.
      ignores = unread;
      script = ''
        set -e
        uv run --no-sync pytest -n 4 tests
        ${py} scripts/check_eval.py .
      '';
    };

    # P4: boot the live installer ISO under UEFI with no disk. The VM tasks boot the test ISO `packages.iso-test`
    # (zstd level 3). `BASIX_ISO_ATTR=iso` selects the shipped ISO. `BASIX_STALL_S` sets the bound for a console with
    # no progress (default 120 s). Measured 2026-10-21 with a warm Nix store: 61 s to 63 s. The limit is 450 s: a cold
    # store builds the system toplevel too.
    "opt:basix-iso" = optIn {
      timeout_s = 450;
      slice = "010/P4b/vm";
      script = ''
        ${py} scripts/iso_boot.py .
      '';
    };

    # P4: run the installer scenarios, two at a time, and check first apply without a network device. It boots the test
    # ISO (see `opt:basix-iso`). Two light VMs in one check are allowed (owner rule). Measured 2026-10-22: 729 s with
    # two scenarios at a time, 1001 s one at a time. The limit is 1500 s.
    "opt:basix-install" = optIn {
      timeout_s = 1500;
      slice = "010/P4b/vm";
      script = ''
        BASIX_PARALLEL=2 ${py} scripts/install_vm.py .
      '';
    };

    # P4: run the product disk script against qcow2 images in a NixOS VM. Measured 2026-10-21 with a warm Nix store:
    # 42 s to 44 s. The limit is 150 s.
    "opt:basix-guard-vm" = optIn {
      timeout_s = 150;
      slice = "010/P4b/vm";
      script = ''
        ${py} scripts/guard_vm.py .
      '';
    };

    # Opt-in: boot the machine of each shipped capability and run its check.cases (BSX-152, BSX-194, BSX-270).
    # It builds a VM per boot and needs /dev/kvm. VM files go under $BASIX_WORK. Raw logs go under $BASIX_LOGS.
    "opt:basix-capabilities" = optIn {
      timeout_s = 7200;
      script = ''
        ${py} scripts/capabilities_vm.py . capabilities
      '';
    };

    # Opt-in (needs /dev/kvm; never counts for green): the real `basix` command in a NixOS VM with no network (P3).
    # The host prepares the sources and the states of the machine (about 1 to 2 minutes with a warm Nix store). The VM
    # boots in about a minute, and the cases take about seven more. Measured 2026-10-21: 567 s for the whole run (189 checks).
    # The limit is about twice that, with room for a cold store. VM files go under $BASIX_WORK.
    # The script takes `--groups a,b` and runs only those case groups (default: all). The task runs the full set.
    "opt:basix-apply" = optIn {
      timeout_s = 1200;
      slice = "v2/apply";
      script = ''
        uv run --no-sync python scripts/apply_vm.py
      '';
    };

    # Opt-in (needs /dev/kvm; never counts for green): `basix update` from one Basix release to the next on the installed
    # machine (P7b, G8 for Basix). It boots a qcow2 overlay of the installed disk that `opt:basix-install` stores (or
    # installs first), makes two scratch releases with `scripts/release.sh --skip-status` and a scratch channel
    # (`release`) in $BASIX_WORK, and runs `basix update` and `basix apply` in the VM, which has no network. The host
    # builds both systems and passes them through a 9p share. It never writes to a real repository or to ~/.cache/nix.
    # The disk cache is the one of `opt:basix-install` (slice 010/P4b/vm). The key of that cache holds the ISO path,
    # which changes with the tree, so a new tree installs first. Measured 2026-10-22: 407 s with that install (96
    # checks; a run that reuses the disk is shorter). The limit is about twice that.
    "opt:basix-release" = optIn {
      timeout_s = 900;
      slice = "010/P7/basix-vm";
      script = ''
        export BASIX_DISK_CACHE="''${BASIX_DISK_CACHE:-''${XDG_CACHE_HOME:-$HOME/.cache}/basix/installed-disks}"
        ${py} scripts/release_vm.py .
      '';
    };

    # Opt-in: boot the desktop machine with a screen, check that greetd shows the login screen, and log in as the learner
    # (L66, L67, P6). It builds the closure of Hyprland, Noctalia, and Firefox, and needs /dev/kvm. VM files go under
    # $BASIX_WORK. Raw logs and one PNG per screen go under $BASIX_LOGS. Do not run it beside another VM check.
    # Measured 2026-10-21 with a warm Nix store: 48 s to 55 s for the whole run (22 checks). The limit is 300 s: it
    # leaves room for the build of the system derivation after a change. A cold store needs a longer limit.
    "opt:basix-desktop" = optIn {
      timeout_s = 300;
      script = ''
        ${py} scripts/desktop_vm.py .
      '';
    };

    # Opt-in (needs no VM): editor help in a fresh workspace (BSX-371). It makes a workspace as `basix install` does, builds
    # the launcher of the workspace lock (`nix build --inputs-from . basix#editor`), starts `ty` through it, and asks over
    # the language server protocol for completion, hover, and diagnostics on `system.py`. Measured 2026-10-23: about 15 s
    # with a warm Nix store (the first run fetches `ty`). The limit is 300 s. Files go under $BASIX_WORK.
    "opt:basix-editor" = optIn {
      timeout_s = 300;
      slice = "v2/editor";
      script = ''
        uv run --no-sync python scripts/editor_check.py
      '';
    };

    # The devman contract names. They run the standard tasks.
    # They run no command and read no file, so they ignore only the folder of the devenv state.
    "base:check" = {
      after = [ "check:lint" "check:format" "check:types" ];
      input.testee.ignores = [ ".devenv/**" ];
    };
    "base:test" = {
      after = [ "test:full" ];
      input.testee.ignores = [ ".devenv/**" ];
    };
  };

  # No .env needed; silence the integration hint.
  dotenv.disableHint = true;
}
