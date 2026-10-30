# Changelog

## v0.5.1

```text
Basix 0.5.1 (pynix pynix-v0.7.0; nixpkgs e7439b6, 2026-10-08)
Previous: v0.5.0. Commits since then: 4.
- Version 0.5.1
- Publish the python input pins in /etc/basix/python-pins.json
- Pin pynix-v0.7.0
- Compile: use the minix text for a NameError or ImportError in a helper
```

## v0.5.0

```text
Basix 0.5.0 (pynix pynix-v0.6.0; nixpkgs e7439b6, 2026-10-08)
Previous: v0.4.4. Commits since then: 7.
- Version 0.5.0
- Document the python field of Input
- Build the Python code of an input into the input packages
- Pin pynix-v0.6.0
- Compile: name the file of an error in a helper module
- Compile: refuse an Input that the file does not show
- Compile: the child writes no bytecode
```

## v0.4.4

```text
Basix 0.4.4 (pynix pynix-v0.5.0; nixpkgs e7439b6, 2026-10-08)
Previous: v0.4.3. Commits since then: 3.
- Version 0.4.4
- Let basix update, apply, and undo run in a git worktree
- Pin pynix-v0.5.0
```

## v0.4.3

```text
Basix 0.4.3 (pynix pynix-v0.4.0; nixpkgs e7439b6, 2026-10-08)
Previous: v0.4.2. Commits since then: 7.
- Version 0.4.3
- Pin pynix-v0.4.0
- Copy the lock sources into the apply VM without a signature check
- Let the VM disk checks work when / is not btrfs
- Merge v0.4.2 into main
- explain: list the names below a prefix of options
- Read the version from the package metadata
```

## v0.4.2

```text
Basix 0.4.2 (pynix pynix-v0.3.0; nixpkgs e7439b6, 2026-10-08)
Previous: v0.4.1. Commits since then: 4.
- Version 0.4.2
- Merge fix-candidate-unit-isolation-v0.4.0: isolate systemd verification to candidate units
- Format candidate unit verification command
- Isolate systemd verification to candidate units
```

## v0.4.1

```text
Basix 0.4.1 (pynix pynix-v0.3.0; nixpkgs e7439b6, 2026-10-08)
Previous: v0.4.0. Commits since then: 6.
- Version 0.4.1
- Document that the installer ignores BASIX_WORKSPACE
- Let BASIX_WORKSPACE name the default workspace folder
- update: restore the ref fallback, test the real lock shapes of the v0.3.0 to v0.4.0 update
- update: show the version of the old commit, not of the new tree
- README: say that pull requests are welcome and review is not promised
```

## v0.4.0

```text
Basix 0.4.0 (pynix pynix-v0.3.0; nixpkgs e7439b6, 2026-10-08)
Initial release.
```
