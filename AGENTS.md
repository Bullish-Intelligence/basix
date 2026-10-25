# AGENTS.md — general rules

This file holds general rules for style, code, verification, and records. It holds no project
information. Read [`DEVELOPING.md`](DEVELOPING.md) next: the product rules, the checks, the fast loop, and the
repository layout.

If the folder `.loci` exists, read `.loci/AGENTS.md` and `.loci/CURRENT.md`.

Do not add project information here: no slice, release, requirement row, ruling, task list, or
project state. Put it in `DEVELOPING.md` or in the project records.

## Design rules

- Simplicity beats capability. Constrain workflows. Serve the common path first.
- Never hard-wire a personal host, network name, binary cache, path, or secret into a shipped file.
- Never weaken a safety check to make a fixture pass.

## Verification

```bash
testee verify
```

Run the command from the repository root. It is the normal gate. A change needs a green
`testee verify`. The `testee` command is the host program (see the nix profile). It checks the
commit in its own workspace. Do not wrap it in `devenv shell`. Do not add a `testee` input or any
`testee.*` option.

Run other in-repo commands through the devenv: `devenv shell -- <command>`.

Use Gitman to open and close workspaces: `gitman work NAME`, then `gitman done NAME`. Before you
open a workspace, list the existing ones and any work that `main` lacks, and report it.

## Shared instructions

Follow your active user and machine instructions for rules that apply across repositories.
