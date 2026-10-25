# basix

Basix is a distribution for people who learn computing on their own machines. A learner writes one typed Python file,
`system.py`, and `basix apply` turns it into a NixOS machine. The sister project minix does the same for the
personal home of one account, with `home.py` and Home Manager.

[`DEVELOPING.md`](DEVELOPING.md) names the product rules, the commands, the layout, and the checks.
[`AGENTS.md`](AGENTS.md) holds the general rules.

Start at `.loci/CURRENT.md` when the folder `.loci` exists, else at `DEVELOPING.md`. Run the gate with `testee verify`.

## Release

```bash
devenv shell -- scripts/release.sh vX.Y.Z-rc.N
```

The job needs a GREEN `testee status` for the commit at HEAD. It checks the locks and the pair of `pynix_os` with
nixpkgs, compares the pins with the newest minix tag (it warns only), writes `provenance/` and `CHANGELOG.md`, makes
one release commit, and makes the annotated tag. It never pushes. It prints the two push commands for `origin`. The branch is
`release` (`BASIX_RELEASE_BRANCH` changes it). The release commit needs a GREEN `testee verify` before you push it.

## Author

Bullish-Intelligence <242310653+Bullish-Intelligence@users.noreply.github.com>
