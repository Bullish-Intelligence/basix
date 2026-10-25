"""The VM check `opt:basix-release`: `basix update` from one Basix release to the next (G8 for Basix, 009-D10, 010-D31).

Usage: python3 -I scripts/release_vm.py [ROOT]      (run by `testee verify opt:basix-release`)

The check needs `/dev/kvm`. It never counts for green. It ports `opt:minix-update` to the installed Basix machine.

The host makes a scratch channel. It never writes to a real repository and never makes a real tag:

- a bare copy of `pynix`, with one more scratch tag `pynix-v0.1.0-rc.93`. The tag has the tree of the pinned tag, and a
  changelog section with one line that names `environment.systemPackages` (the option that `programs=` sets);
- a scratch Git repository with the files of the checked tree. `scripts/release.sh --skip-status` makes two scratch
  releases there: R1 (`vVERSION-rc.91`, the files as they are) and R2 (`vVERSION-rc.92`: one more commit that pins
  `pynix-v0.1.0-rc.93` and adds the file `/etc/basix/release`, only for a machine with a hardware report, so the
  anchor machines and the checks of the release job stay as they are);
- a bare scratch repository whose branch `release` is at R1, then at R2 (the stub of a workspace follows
  `git+https://github.com/Bullish-Intelligence/basix?ref=release`, 010-D54);
- a Git config that maps the public Basix and `pynix` URLs to the scratch repositories (`insteadOf`), and the public
  minix URL to the read-only local repository (the layer compare of the release job reads it).

Every host `nix` and `git` command of the channel uses its own cache folder, `$BASIX_WORK/release/cache`
(`XDG_CACHE_HOME`, cleared at the start), and its own Git config (`GIT_CONFIG_GLOBAL`). The Nix cache of the owner never
maps a public URL to a scratch ref. The ISO and QEMU builds run before the channel and use the owner's cache.

The installed disk: `iv.install_and_login` boots a qcow2 overlay of the disk that `opt:basix-install` stores. It
installs first when no stored disk matches (`BASIX_FRESH_INSTALL=1` forces it). The VM has no network. The name of
the scenario starts with `nvme`, so the install types the same answers as the `nvme` scenario and the disk key is shared.

How the host reaches the VM: the installed system runs no SSH server. The login VM gets one 9p share (`-virtfs`) of
`$BASIX_WORK/release/share`. The learner mounts it at `/mnt/host` with `sudo`. The VM writes its workspace and its store
list there. The host builds the R1 and R2 systems from that workspace, writes what the VM lacks into one archive
(`nix-store --export`) in the share, and the VM imports it with `nix-store --import`.

Cases (each prints `PASS` or `FAIL` lines):

- `repos`, `channel`: the scratch releases and the scratch channel, before the VM.
- `mount`, `start`: the share mounts; the workspace is as the install leaves it (the stub follows the `path:` copy of
  the ISO); the learner makes the first Git commit of that state.
- `prepare`: the host builds R1 and R2 from the workspace of the VM; the locks equal the locks of the VM later.
- `update1`: `basix update` moves the stub to the release channel (011 bug 2) and `basix` to R1. `apply1`: `basix apply` switches; the second apply changes nothing.
- `update2`: the channel moves to R2. `basix update` prints `Basix vVERSION-rc.91 -> vVERSION-rc.92`, the nixpkgs age,
  the changelog part with the line, and the next step. `apply2`: the switch; `/etc/basix/release` holds the R2 tag;
  a second apply changes nothing.
- `again`: a second `basix update` says that Basix is up to date and prints no `pynix` line.
- `cache`: the scratch Nix cache holds the scratch refs, and the cache of the owner got none.

Environment (all optional):

- `BASIX_WORK`: the folder of the VM files and the scratch (default `~/.cache/basix/010/P7/basix-vm`).
- `BASIX_LOGS`: the raw logs (default `~/.local/state/basix/010/P7/basix-vm/logs`).
- `BASIX_DISK_CACHE`: the folder of the installed disks (see `install_vm.DiskCache`).
- `BASIX_RELEASE_PYNIX` (required): a local clone of `pynix` for the scratch `pynix`. Clones only read it.
- `BASIX_RELEASE_MINIX` (required): a local clone of minix that the public minix URL maps to. Clones only read it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import install_vm as iv  # noqa: E402
import iso_boot  # noqa: E402
import vmlib  # noqa: E402

report = iv.report
FEATURES = ["--extra-experimental-features", "nix-command flakes"]
PYNIX_URL = "https://github.com/Bullish-Intelligence/pynix"
MINIX_URL = "https://github.com/Bullish-Intelligence/minix"
PYNIX_NEW = "pynix-v0.1.0-rc.93"
STAMP = "2026-10-21T12:00:00+00:00"  # a fixed date: the same inputs make the same commits
MARKER = "/etc/basix/release"  # the visible change of R2
MOUNT = "/mnt/host"
SHARE_TAG = "hostshare"
SCENARIO = "nvme-release"  # starts with `nvme`: the install types the answers of the `nvme` scenario (the disk key)
WS = "~/basix"
WS_ABS = f"/home/{iv.ACCOUNT}/basix"  # for a command that runs as root
QUIET = iv.QUIET_STALL_S
# What the scratch copy of the checked tree leaves out: state of tools, caches, notes, and links of the workspace.
SKIP_TOP = {
    ".git", ".jj", ".gitman", ".devenv", ".devenv.flake.nix", ".direnv", ".venv", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", ".testee", ".agents", ".claude", ".copyroom", ".copyroom-local.json", ".scratch", ".loci", "devenv.local.nix",
    "build", "dist", "result",
}  # fmt: skip
# The line that the changelog of the scratch pynix tag names. Its form is the form of `pynix.changelog` (a type change).
CHANGE_LINE = "environment.systemPackages: list of package -> list of (package or string)"
NEXT_LINE = "next: run basix apply to use it"
MOVED_LINE = "This machine used the copy of Basix from the installer. It now follows the release channel."
# The anchor of the change of R2 in `modules/core.nix`, and the change. `options ? facter` is true only for a machine
# with a hardware report, so the anchor machines (T3) and the fixtures of the release job keep their derivations.
CORE_ANCHOR = '    networking.hostName = lib.mkDefault "basix";\n'
COUNTS: dict[str, list[int]] = {}  # case -> [passed, failed]
TIMES: dict[str, float] = {}
FACTS: dict = {}  # revisions and store paths of this run, for the log and for results.json


def check(case: str, text: str, want: object, got: object) -> bool:
    """Report one comparison. The case name goes into the counts."""
    ok = want == got
    detail = "" if ok else f" (want {str(want)[:400]!r}, got {str(got)[:400]!r})"
    return tally(case, ok, f"release/{case}: {text}{detail}")


def tally(case: str, ok: bool, text: str) -> bool:
    COUNTS.setdefault(case, [0, 0])[0 if ok else 1] += 1
    return report(ok, text)


def tick(name: str, started: float) -> None:
    TIMES[name] = round(time.monotonic() - started, 1)


# --- the scratch channel on the host ---------------------------------------------------------------------------
def channel_names(root: Path) -> tuple[str, str, str]:
    """The stub URL of the checked tree, the public Basix URL, and the branch (010-D38)."""
    text = (root / "src/basix_config/stub.py").read_text()
    found = re.search(r'^DEFAULT_BASIX_URL = "([^"]+)"', text, re.MULTILINE)
    if not found:
        raise vmlib.Stop("src/basix_config/stub.py has no DEFAULT_BASIX_URL line")
    parts = re.fullmatch(r"git\+(https://[^?]+)\?ref=(\S+)", found[1])
    if not parts:
        raise vmlib.Stop(f"DEFAULT_BASIX_URL is not git+https://URL?ref=BRANCH: {found[1]!r}")
    return found[1], parts[1], parts[2]


def version_of(root: Path) -> str:
    return tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]


def section_text(old: str) -> str:
    return f"""## {PYNIX_NEW}

```text
pynix 0.1.0-rc.93 (nixpkgs e7439b6, 2026-10-08; Home Manager fae6e9e; flake-parts 024633c)
Previous: 0.1.0-rc.3 (nixpkgs e7439b6, 2026-10-08). Gap: 0 d 0 h.
NixOS options: 21699 -> 21699. Added 0, removed 0, type changed 1, renamed 0.
Home Manager options: 5229 -> 5229. Added 0, removed 0, type changed 0, renamed 0.
Package names: 25285 -> 25285. Added 0, removed 0.
NixOS:
 Type changed:
  {CHANGE_LINE}
```
"""


@dataclass
class Scratch:
    """The folders, the environment, and the commands of the host side of the check."""

    logs: vmlib.Logs
    work: Path
    stub_url: str
    public_url: str
    branch: str
    version: str
    tag1: str
    tag2: str
    r1: str = ""
    r2: str = ""
    owner_uv: str = field(
        default_factory=lambda: str(Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "uv")
    )

    @property
    def cache(self) -> Path:
        return self.work / "cache"

    @property
    def config(self) -> Path:
        return self.work / "channel.gitconfig"

    @property
    def repos(self) -> Path:
        return self.work / "repos"

    @property
    def bare(self) -> Path:
        return self.repos / "basix.git"

    @property
    def pynix(self) -> Path:
        return self.repos / "pynix.git"

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The environment of a host command that fetches through the channel."""
        env = vmlib.clean_env()
        env.pop("VIRTUAL_ENV", None)
        nix_config = (env.get("NIX_CONFIG", "") + "\ntarball-ttl = 0").strip()  # a ref that moves is read again at once
        env.update(
            GIT_AUTHOR_NAME="scratch", GIT_AUTHOR_EMAIL="scratch@example.invalid",
            GIT_COMMITTER_NAME="scratch", GIT_COMMITTER_EMAIL="scratch@example.invalid",
            GIT_AUTHOR_DATE=STAMP, GIT_COMMITTER_DATE=STAMP,
            GIT_CONFIG_GLOBAL=str(self.config), GIT_CONFIG_NOSYSTEM="1",
            XDG_CACHE_HOME=str(self.cache), NIX_CONFIG=nix_config,
        )  # fmt: skip
        env.setdefault(
            "UV_CACHE_DIR", self.owner_uv
        )  # the release job runs `uv sync --locked`: it needs the owner's wheels
        env.update(extra or {})
        return env

    def run(self, name: str, argv: list[str], cwd: Path | None = None, timeout: int = 3600, env: dict | None = None):
        done = self.logs.run(name, argv, cwd=cwd, env=env or self.env(), timeout=timeout)
        if done.returncode != 0:
            raise vmlib.Stop(
                f"{name} failed ({done.returncode}): {' '.join(argv)}\n{(done.stdout + done.stderr)[-2500:]}"
            )
        return done

    def git(self, cwd: Path, *args: str) -> str:
        return self.run("git-" + (args[0] if args else "x"), ["git", *args], cwd=cwd).stdout

    def nix(self, name: str, *args: str, cwd: Path | None = None) -> str:
        return self.run(name, ["nix", *FEATURES, *args], cwd=cwd).stdout

    def gitconfig(self, bare: Path | str, pynix: Path | str, minix: str | None = None) -> str:
        text = f'[url "{bare}"]\n\tinsteadOf = {self.public_url}\n[url "{pynix}"]\n\tinsteadOf = {PYNIX_URL}\n'
        if minix:
            text += f'[url "{minix}"]\n\tinsteadOf = {MINIX_URL}\n'
        return text + "[safe]\n\tdirectory = *\n"


def copy_checkout(root: Path, target: Path) -> None:
    """Copy the files of the checked tree into `target`. `.gitignore` then keeps the same files out of the scratch commit."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info")
    target.mkdir(parents=True)
    for entry in sorted(root.iterdir()):
        if entry.name in SKIP_TOP:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, target / entry.name, symlinks=True, ignore=ignore)
        else:
            shutil.copy2(entry, target / entry.name, follow_symlinks=False)


def source_repo(variable: str) -> Path:
    """The local clone that the environment variable names. There is no default: the place is a fact of the machine."""
    value = os.environ.get(variable)
    if not value:
        raise vmlib.Stop(f"stop: set {variable} to a local clone of the repository")
    return Path(value)


def prepare_pynix(s: Scratch, vendor: Path, old_tag: str) -> None:
    """A bare copy of `pynix` (the clone only reads the source) with one scratch tag: the old tree and one more section."""
    s.git(s.repos, "clone", "-q", "--bare", "--no-hardlinks", str(vendor), "pynix.git")
    s.git(s.pynix, "remote", "remove", "origin")
    s.git(s.repos, "clone", "-q", "--no-hardlinks", str(s.pynix), "pynix-work")
    work = s.repos / "pynix-work"
    s.git(work, "remote", "remove", "origin")
    s.git(work, "checkout", "-q", "--detach", old_tag)
    text = (work / "CHANGELOG.md").read_text()
    head = "# Changelog\n\n"
    if not text.startswith(head):
        raise vmlib.Stop("the changelog of pynix does not start with the title `# Changelog`")
    (work / "CHANGELOG.md").write_text(head + section_text(old_tag) + "\n" + text[len(head) :])
    s.git(work, "add", "CHANGELOG.md")
    s.git(work, "commit", "-q", "-m", f"Scratch release {PYNIX_NEW}: one changelog section")
    s.git(work, "tag", "-a", PYNIX_NEW, "-m", f"{PYNIX_NEW} (scratch)")
    s.git(work, "push", "-q", str(s.pynix), PYNIX_NEW)
    FACTS["pynix_old"] = s.git(s.pynix, "rev-parse", f"{old_tag}^{{commit}}").strip()
    FACTS["pynix_new"] = s.git(s.pynix, "rev-parse", f"{PYNIX_NEW}^{{commit}}").strip()
    tree_old = s.git(s.pynix, "rev-parse", f"{old_tag}^{{commit}}:src").strip()
    tree_new = s.git(s.pynix, "rev-parse", f"{PYNIX_NEW}^{{commit}}:src").strip()
    check(
        "repos",
        "the src tree of the scratch pynix tag equals the src tree of the pinned tag (the pair check holds)",
        tree_old,
        tree_new,
    )


def release(s: Scratch, clone: Path, tag: str) -> str:
    """Run the release job in the scratch clone. Return the revision of the tag."""
    work = s.work / f"release-work-{tag}"
    env = s.env(
        {
            "BASIX_RELEASE_LOG": str(s.logs.root / f"release-{tag}"),
            "BASIX_RELEASE_WORK": str(work),
            "BASIX_RELEASE_BRANCH": s.branch,
            "BASIX_RELEASE_PYNIX_URL": PYNIX_URL,
            "BASIX_RELEASE_MINIX_URL": MINIX_URL,
        }
    )
    started = time.monotonic()
    done = s.logs.run(f"release-{tag}", ["scripts/release.sh", "--skip-status", tag], cwd=clone, env=env, timeout=3600)
    s.logs.note(f"release.sh --skip-status {tag}: exit {done.returncode}, {time.monotonic() - started:.0f} s")
    for line in done.stdout.splitlines():
        if line.startswith("[release"):
            s.logs.note("   " + line)
    if done.returncode != 0:
        raise vmlib.Stop(f"release.sh {tag} failed ({done.returncode}):\n{(done.stdout + done.stderr)[-2500:]}")
    shutil.rmtree(work, ignore_errors=True)
    return s.git(clone, "rev-parse", f"{tag}^{{commit}}").strip()


def move_channel(s: Scratch, tag: str) -> None:
    """Point the branch of the scratch channel at a release. Both releases are fast-forwards of each other's parent."""
    s.git(s.bare, "update-ref", f"refs/heads/{s.branch}", s.r1 if tag == s.tag1 else s.r2)
    s.logs.note(f"the channel {s.branch} is at {tag}")


def prepare_channel(s: Scratch, root: Path, vendor_pynix: Path, vendor_minix: Path) -> None:
    """Make the scratch pynix, the two scratch releases of Basix, and the scratch channel."""
    shutil.rmtree(s.work / "repos", ignore_errors=True)
    s.repos.mkdir(parents=True)
    flake_text = (root / "flake.nix").read_text()
    tags = set(re.findall(r"refs/tags/(pynix-v[0-9][0-9A-Za-z.+-]*)", flake_text))
    if len(tags) != 1:
        raise vmlib.Stop(f"flake.nix names {len(tags)} pynix tags; the check needs exactly one: {sorted(tags)}")
    old_tag = tags.pop()
    s.config.write_text("")  # the scratch config exists (and is empty) before the first command
    prepare_pynix(s, vendor_pynix, old_tag)
    s.git(s.repos, "init", "-q", "--bare", "basix.git")
    s.git(s.bare, "symbolic-ref", "HEAD", f"refs/heads/{s.branch}")
    s.config.write_text(s.gitconfig(s.bare, s.pynix, f"file://{vendor_minix}"))
    clone = s.repos / "basix-work"
    copy_checkout(root, clone)
    s.git(clone, "init", "-q", "-b", "main")
    s.git(clone, "add", "-A")
    s.git(clone, "commit", "-q", "-m", "Scratch copy of the tree under check (opt:basix-release)")
    FACTS["base"] = s.git(clone, "rev-parse", "HEAD").strip()
    # R1 is the tree as it is.
    s.r1 = release(s, clone, s.tag1)
    # R2 is one scratch commit on top of R1: the new pin of pynix and the marker file.
    flake = (clone / "flake.nix").read_text()
    if flake.count(old_tag) != 1:
        raise vmlib.Stop(f"flake.nix names the pynix tag {old_tag} {flake.count(old_tag)} times; the check needs once")
    (clone / "flake.nix").write_text(flake.replace(old_tag, PYNIX_NEW))
    core = (clone / "modules/core.nix").read_text()
    if core.count(CORE_ANCHOR) != 1:
        raise vmlib.Stop(f"modules/core.nix does not hold the anchor line once: {CORE_ANCHOR.strip()!r}")
    add = (
        "    # The scratch change of the check opt:basix-release: a file that only a machine with a hardware report gets.\n"
        f'    environment.etc = lib.mkIf (options ? facter) {{ "basix/release".text = "{s.tag2}\\n"; }};\n'
    )
    (clone / "modules/core.nix").write_text(core.replace(CORE_ANCHOR, CORE_ANCHOR + add))
    # The lock names only the public URL: the channel maps it to the scratch repository while the lock is made.
    s.nix("flake-update-pynix", "flake", "update", "pynix", cwd=clone)
    s.git(clone, "add", "-A")
    s.git(
        clone,
        "commit",
        "-q",
        "-m",
        "Scratch change for opt:basix-release: pin pynix-v0.1.0-rc.93, add the release marker file",
    )
    s.r2 = release(s, clone, s.tag2)
    s.git(clone, "push", "-q", str(s.bare), s.tag1, s.tag2, f"{s.r1}:refs/heads/{s.branch}")
    FACTS.update(r1=s.r1, r2=s.r2, vendor_minix=str(vendor_minix))
    check(
        "repos",
        "R1 and R2 are two commits, and R2 follows R1",
        True,
        s.r1 != s.r2 and s.git(clone, "merge-base", s.r1, s.r2).strip() == s.r1,
    )
    check(
        "repos",
        "the branch of the scratch channel is at R1",
        s.r1,
        s.git(s.bare, "rev-parse", f"refs/heads/{s.branch}").strip(),
    )
    prove_channel(s)


def prove_channel(s: Scratch) -> None:
    """Show that the public URLs lead to the scratch repositories, not to the repositories of the owner."""
    heads = s.git(s.pynix, "ls-remote", "--heads", s.public_url)
    check(
        "channel",
        "the public Basix URL lists the branch of the channel at R1 through the config",
        f"{s.r1}\trefs/heads/{s.branch}\n",
        heads,
    )
    tags = s.git(s.pynix, "ls-remote", "--tags", PYNIX_URL)
    check(
        "channel",
        "the public pynix URL lists the scratch tag rc.93 through the config",
        True,
        f"refs/tags/{PYNIX_NEW}" in tags,
    )
    s.logs.note("git ls-remote --tags " + PYNIX_URL + " (GIT_CONFIG_GLOBAL = the scratch config):\n" + tags)
    vendor = source_repo("BASIX_RELEASE_PYNIX")
    packed = vendor / ".git/packed-refs"
    refs = packed.read_text(errors="replace") if packed.is_file() else ""
    loose = vendor / ".git/refs/tags"
    names = [p.name for p in loose.iterdir()] if loose.is_dir() else []
    check(
        "channel",
        "the tag rc.93 is not in the pynix repository of the owner (packed-refs and loose tags)",
        False,
        "rc.93" in refs or any("rc.93" in n for n in names),
    )


# --- the Nix cache of the host ---------------------------------------------------------------------------------
def owner_nix_cache() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "nix"


def owner_cache_state() -> dict[str, str]:
    """The names and times of the entries of the owner's Nix cache (top level and the git cache folders)."""
    cache = owner_nix_cache()
    state: dict[str, str] = {}
    if not cache.is_dir():
        return state
    for entry in sorted(cache.iterdir()):
        state[entry.name] = str(entry.lstat().st_mtime_ns)
    git = cache / "gitv3"
    for entry in sorted(git.iterdir()) if git.is_dir() else []:
        state[f"gitv3/{entry.name}"] = str(entry.lstat().st_mtime_ns)
    return state


def cache_refs(folder: Path) -> list[str]:
    """The ref names of a Nix git cache folder."""
    names = [p.name for p in (folder / "refs").rglob("*") if p.is_file()] if (folder / "refs").is_dir() else []
    packed = folder / "packed-refs"
    if packed.is_file():
        names += [
            line.split()[-1].removeprefix("refs/heads/").removeprefix("refs/tags/")
            for line in packed.read_text().splitlines()
            if line and line[0] not in "#^"
        ]
    return sorted(set(names))


def scratch_refs(folder: Path, branch: str) -> list[str]:
    return [n for n in cache_refs(folder) if re.search(r"rc\.9[0-9]$", n) or n == branch]


def prove_cache(s: Scratch, before: dict[str, str]) -> None:
    """Show what the scratch cache holds, and that the owner's cache got no entry for a scratch ref."""
    scratch = s.cache / "nix"
    git = scratch / "gitv3"
    s.logs.note(f"scratch Nix cache: {scratch}")
    held: list[str] = []
    for entry in sorted(git.iterdir()) if git.is_dir() else []:
        if not entry.is_dir() or entry.name.endswith(".lock"):
            continue
        fetch = entry / "FETCH_HEAD"
        text = fetch.read_text() if fetch.is_file() else ""
        source = text.split(" of ")[-1].strip() if " of " in text else "-"
        refs = scratch_refs(entry, s.branch)
        held += refs
        s.logs.note(f"   gitv3/{entry.name}: from {source}; scratch refs {refs}")
    check("cache", "the scratch cache holds the scratch tag of pynix", True, PYNIX_NEW in held)
    check("cache", "the scratch cache holds the branch of the Basix channel", True, s.branch in held)
    fetcher = scratch / "fetcher-cache-v4.sqlite"
    downloads = -1
    if fetcher.is_file():
        with contextlib.suppress(sqlite3.Error):
            db = sqlite3.connect(f"file:{fetcher}?mode=ro", uri=True)
            downloads = db.execute("select count(*) from Cache where domain in ('file', 'tarball')").fetchone()[0]
            db.close()
    s.logs.note(f"INFO: the scratch cache holds {downloads} download entries (file or tarball; -1: not readable)")
    after = owner_cache_state()
    changed = sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))
    cache = owner_nix_cache()
    s.logs.note(
        f"owner Nix cache {cache}: {len(before)} entries before, {len(after)} after; changed during the channel: {changed}"
    )
    touched = [
        n for n in changed if n.startswith("gitv3/") and [r for r in scratch_refs(cache / n, s.branch) if r != s.branch]
    ]
    check("cache", "no git folder of the owner's cache changed during the channel and holds a scratch ref", [], touched)
    gitv3 = (before.get("gitv3"), after.get("gitv3"))
    check(
        "cache",
        "the gitv3 folder of the owner's cache has the same mtime before and after the channel",
        gitv3[0],
        gitv3[1],
    )


# --- the host: the systems of R1 and R2 -------------------------------------------------------------------------
def archive_paths(node: dict, top: bool = True) -> list[str]:
    """The store paths of the inputs in the tree of `nix flake archive --json`."""
    found = [] if top else [node["path"]]
    for item in node.get("inputs", {}).values():
        found += archive_paths(item, False)
    return found


@dataclass
class Prepared:
    top1: Path
    top2: Path
    lock1: dict
    lock2: dict


def build_state(s: Scratch, ws: Path, label: str) -> tuple[Path, Path]:
    """Compile and build the system of a prepared workspace, with the steps of `basix apply`. Return compiler and system."""
    s.run("git-add", ["git", "add", "-A"], cwd=ws)
    out = s.nix(
        f"{label}-compiler",
        "build",
        "--no-warn-dirty",
        "--print-out-paths",
        "--out-link",
        str(s.work / f"links/{label}-compiler"),
        ".#compiler",
        cwd=ws,
    )
    compiler = Path(out.split()[-1])
    s.run(f"{label}-compile", [str(compiler / "bin/basix-compile"), "system.py", "system.json"], cwd=ws)
    s.run("git-add", ["git", "add", "-A"], cwd=ws)
    attr = ".#nixosConfigurations.basix.config.system.build.toplevel"
    top = s.nix(
        f"{label}-system",
        "build",
        "--no-warn-dirty",
        "--print-out-paths",
        "--out-link",
        str(s.work / f"links/{label}-system"),
        attr,
        cwd=ws,
    )
    system = Path(top.split()[-1])
    vmlib.refuse_cycles(system, f"{label} system")
    return compiler, system


def read_lock(ws: Path) -> dict:
    return json.loads((ws / "flake.lock").read_text())


def prepare_systems(s: Scratch, share: Path) -> Prepared:
    """Build R1 and R2 from the workspace of the VM, and write what the VM lacks into an archive in the share."""
    started = time.monotonic()
    ws0 = s.work / "prep/ws0"
    shutil.rmtree(s.work / "prep", ignore_errors=True)
    (s.work / "links").mkdir(parents=True, exist_ok=True)
    ws0.mkdir(parents=True)
    with tarfile.open(share / "guest/ws.tar") as pack:
        pack.extractall(ws0, filter="data")
    guest_store = set((share / "guest/store.txt").read_text().split())
    s.logs.note(
        f"the VM store holds {len(guest_store)} entries; the workspace of the VM: {sorted(p.name for p in ws0.iterdir())}"
    )
    roots: list[str] = []
    gitconfigs: dict[str, str] = {}
    built: dict[str, tuple[Path, Path, dict]] = {}
    previous = ws0
    for label, tag in (("r1", s.tag1), ("r2", s.tag2)):
        move_channel(s, tag)
        ws = s.work / f"prep/{label}"
        shutil.copytree(previous, ws, symlinks=True)
        if label == "r1":  # the move that `basix update` makes: the path: stub follows the channel
            stub = (ws / "flake.nix").read_text()
            moved = re.sub(
                r"^(  inputs\.basix\.url = ).*$", lambda m: f'{m[1]}"{s.stub_url}";', stub, flags=re.MULTILINE
            )
            (ws / "flake.nix").write_text(moved)
        s.run("git-add", ["git", "add", "-A"], cwd=ws)
        # The command that `basix update` runs.
        s.nix(f"{label}-update", "flake", "update", "basix", "--no-warn-dirty", cwd=ws)
        compiler, top = build_state(s, ws, label)
        tree = json.loads(s.nix(f"{label}-archive", "flake", "archive", "--json", "--no-write-lock-file", cwd=ws))
        sources = archive_paths(tree)
        repo = s.nix(f"{label}-store-add", "store", "add", "--name", "basix.git", str(s.bare)).strip()
        pynix_repo = s.nix(f"{label}-store-add-pynix", "store", "add", "--name", "pynix.git", str(s.pynix)).strip()
        gitconfigs[label] = s.gitconfig(repo, pynix_repo)
        roots += [str(compiler), str(top), repo, pynix_repo, *sources]
        built[label] = (compiler, top, read_lock(ws))
        s.logs.note(
            f"{label}: compiler {compiler}; system {top}; {len(sources)} input sources; channel repository {repo}"
        )
        previous = ws
    for label, text in gitconfigs.items():
        (share / f"gitconfig-{label}").write_text(text)
    # What the VM lacks: the closure of the roots, less the store entries of the VM.
    roots = sorted(set(roots))
    closure: set[str] = set()
    for chunk in range(0, len(roots), 200):
        closure |= set(
            s.run("closure", ["nix-store", "-qR", *roots[chunk : chunk + 200]], env=vmlib.clean_env()).stdout.split()
        )
    needed = sorted(path for path in closure if Path(path).name not in guest_store)
    archive = share / "closure.nar"
    with archive.open("wb") as out:  # `--export` sorts the paths so that each follows its references
        done = subprocess.run(
            ["nix-store", "--export", *needed], stdout=out, stderr=subprocess.PIPE, env=vmlib.clean_env()
        )
    if done.returncode != 0:
        raise vmlib.Stop(f"nix-store --export failed: {done.stderr.decode(errors='replace')[-1500:]}")
    size = archive.stat().st_size
    s.logs.note(f"closure {len(closure)} paths; the VM lacks {len(needed)}; archive {size / 1024**2:.0f} MiB")
    FACTS.update(
        top1=str(built["r1"][1]), top2=str(built["r2"][1]), needed=len(needed), archive_mib=round(size / 1024**2)
    )
    tick("host prepare systems", started)
    return Prepared(built["r1"][1], built["r2"][1], built["r1"][2], built["r2"][2])


# --- the VM ----------------------------------------------------------------------------------------------------
class Guest:
    """Commands on the serial console of the installed machine, as the learner."""

    def __init__(self, console: vmlib.Console):
        self.console = console

    def run(self, line: str, timeout: float = 120, stall: float | None = None) -> tuple[int, str]:
        return self.console.command(line, timeout, stall=stall)

    def prime(self) -> str:
        return f"printf '%s\\n' {shlex.quote(iv.PASSWORD)} | sudo -S -p '' -v"

    def sudo(self, line: str, timeout: float = 120, stall: float | None = None) -> tuple[int, str]:
        """Run `line` as the learner after a fresh `sudo` ticket, so `basix` asks for no password."""
        return self.console.command(f"{self.prime()} && ( {line} )", timeout, stall=stall)

    def root(self, line: str, timeout: float = 120, stall: float | None = None) -> tuple[int, str]:
        """Run `line` as root, in `bash -c`, so its redirections are root's too."""
        return self.console.command(f"{self.prime()} && sudo bash -c {shlex.quote(line)}", timeout, stall=stall)

    def lines(self, path: str) -> list[str]:
        _, text = self.run(f"cat {path}")
        return [line.rstrip() for line in text.splitlines() if line.strip()]

    def lock(self) -> dict:
        _, text = self.run(f"cat {WS}/flake.lock")
        return json.loads(text)


def resolve(lock: dict, target: str | list[str]) -> str:
    """The name of the lock node that an input points to. A list is a `follows` path from the root."""
    if isinstance(target, str):
        return target
    node = "root"
    for step in target:
        node = resolve(lock, lock["nodes"][node]["inputs"][step])
    return node


def node_of(lock: dict, *path: str) -> dict:
    """The lock node that the input path reaches from the root, for example `("basix", "pynix")`."""
    node = "root"
    for name in path:
        node = resolve(lock, lock["nodes"][node]["inputs"][name])
    return lock["nodes"][node]


def tag_of(node: dict) -> str:
    return node["locked"]["ref"].removeprefix("refs/tags/")


def leak_free(lines: list[str]) -> bool:
    """The plain lines name no store path and no git command (009-D15)."""
    return not any("/nix/store" in line or re.search(r"\bgit\b", line) for line in lines)


def vm_steps(vm: iv.Vm, s: Scratch, share: Path, token: str) -> None:
    """The steps in the VM. A failed precondition stops them; the harness then powers the VM off."""
    g = Guest(vm.console)
    logs = vm.lab.logs
    r1, r2 = s.tag1, s.tag2
    # --- the share
    started = time.monotonic()
    rc, _ = g.root(f"mkdir -p {MOUNT} && mount -t 9p -o trans=virtio,version=9p2000.L,msize=524288 {SHARE_TAG} {MOUNT}")
    check("mount", "the 9p share of the host mounts", 0, rc)
    if rc != 0:
        raise vmlib.Stop("the 9p share does not mount; the guest kernel may lack 9p (try: sudo modprobe 9pnet_virtio)")
    _, seen = g.run(f"cat {MOUNT}/ready")
    check("mount", "the guest reads the token that the host wrote", token, seen.strip())
    rc, _ = g.root(f"mkdir -p {MOUNT}/guest && echo ok > {MOUNT}/guest/write-test")
    check("mount", "the guest writes into the share", 0, rc)
    _, route = g.run("ip route | grep -c '^default ' || true")
    check("mount", "the VM has no default route", "0", route.strip())

    # --- the start: a workspace as `basix install` leaves it
    # `basix install` locks the Basix copy of the ISO as a `path:` input, and the first `basix update` moves the stub
    # to the release channel (011 bug 2). The check keeps the stub as it is, then adds a program (as `offline_apply`
    # of install_vm does) so that `programs=` sets `environment.systemPackages`.
    sed_prog = r's/^    serial_console="ttyS0",$/&\n    programs=[Program(pkgs.git)],/'
    rc, _ = g.run(f"sed -i {shlex.quote(sed_prog)} {WS}/system.py")
    check("start", "system.py holds a program", 0, rc)
    _, text = g.run(
        f"grep -c '^  inputs.basix.url = \"path:' {WS}/flake.nix; grep -c '^    programs=\\[Program(pkgs.git)\\],$' {WS}/system.py"
    )
    check("start", "the stub follows a path: copy once, and the program line is there once", ["1", "1"], text.split())
    _, head = g.run(f"head -1 {WS}/flake.nix")
    check("start", "the stub keeps its managed header", "# Managed by Basix. Do not edit.", head.strip())
    # `basix install` must leave flake.lock writable: Nix writes it at `basix update`.
    _, mode = g.run(f"stat -c '%a %U' {WS}/flake.lock")
    logs.note(f"release: mode and owner of flake.lock after the install: {mode.strip()}")
    check("start", "flake.lock is writable by the learner after the install", True, mode.split()[0][-3] in "67")
    # The install holds a flake.lock for the ISO copy of Basix: it keeps that lock until `basix update`.
    rc, _ = g.run(
        f"git -C {WS} -c user.name=learner -c user.email=learner@basix commit -q -am 'Start: Basix from the release channel'"
    )
    check("start", "the start state is committed", 0, rc)
    _, dirty = g.run(f"git -C {WS} status --porcelain")
    check("start", "the working copy is clean at the start", "", dirty.strip())
    rc, _ = g.root(
        f"tar -C {WS_ABS} --exclude=./.basix -cf {MOUNT}/guest/ws.tar . && ls /nix/store > {MOUNT}/guest/store.txt"
    )
    check("start", "the VM wrote its workspace and its store list into the share", 0, rc)
    tick("vm: mount and start", started)

    # --- the host builds the systems from the workspace of the VM
    started = time.monotonic()
    prepared = prepare_systems(s, share)
    check("prepare", "the host built two different systems", True, prepared.top1 != prepared.top2)
    check("prepare", "the lock of R2 holds R2", s.r2, node_of(prepared.lock2, "basix")["locked"]["rev"])
    check("prepare", "the lock of R1 holds R1", s.r1, node_of(prepared.lock1, "basix")["locked"]["rev"])
    check(
        "prepare",
        "the lock of R2 holds the scratch pynix tag",
        (PYNIX_NEW, FACTS["pynix_new"]),
        (tag_of(node_of(prepared.lock2, "basix", "pynix")), node_of(prepared.lock2, "basix", "pynix")["locked"]["rev"]),
    )
    tick("host: prepare", started)

    # --- the VM imports what it lacks
    started = time.monotonic()
    rc, out = g.root(
        f"nix-store --import < {MOUNT}/closure.nar > /tmp/import.log 2>&1; rc=$?; tail -3 /tmp/import.log; exit $rc",
        3600,
        stall=QUIET,
    )
    logs.note(f"release: nix-store --import in the VM: rc {rc}; {out[-400:]}")
    check("prepare", "the VM imported the closures from the share", 0, rc)
    rc, _ = g.run(f"test -e {prepared.top1} && test -e {prepared.top2}")
    check("prepare", "both systems are in the store of the VM", 0, rc)
    rc, _ = g.run(f"cp {MOUNT}/gitconfig-r1 ~/.gitconfig && export NIX_CONFIG='tarball-ttl = 0'")
    check("prepare", "the learner config maps the public URLs to the repositories of R1", 0, rc)
    tick("vm: import", started)
    if rc != 0:
        raise vmlib.Stop("the VM cannot import the host build; the later cases need it")

    # --- update to R1, apply
    started = time.monotonic()
    rc, _ = g.run(f"cd {WS} && basix update > /tmp/update1.log 2>&1", 1800, stall=QUIET)
    lines = g.lines("/tmp/update1.log")
    logs.note("release: `basix update` to R1, output:\n  " + "\n  ".join(lines))
    check("update1", "basix update exits 0", 0, rc)
    check("update1", "the last line names the next step", NEXT_LINE, lines[-1] if lines else "")
    check("update1", "the first line says that Basix now follows the channel", MOVED_LINE, lines[0] if lines else "")
    _, stub_line = g.run(f"grep '^  inputs.basix.url = ' {WS}/flake.nix")
    check("update1", "the stub follows the channel", f'inputs.basix.url = "{s.stub_url}";', stub_line.strip())
    lock = g.lock()
    basix = node_of(lock, "basix")
    check("update1", "the lock holds the revision of R1", s.r1, basix["locked"].get("rev"))
    check(
        "update1",
        "the lock follows the branch of the channel",
        (s.branch, s.public_url),
        (basix["original"].get("ref"), basix["original"].get("url")),
    )
    check(
        "update1",
        "the nested pynix is the pinned tag",
        FACTS["pynix_old"],
        node_of(lock, "basix", "pynix")["locked"].get("rev"),
    )
    check("update1", "the VM and the host made the same lock", prepared.lock1, lock)
    _, state = g.run(f"git -C {WS} status --porcelain")
    check(
        "update1",
        "update changes only flake.lock and the stub line",
        ["M flake.lock", "M flake.nix"],
        sorted(x.strip() for x in state.splitlines()),
    )
    rc, out = g.sudo(f"cd {WS} && basix apply > /tmp/apply1.log 2>&1", 3600, stall=QUIET)
    lines = g.lines("/tmp/apply1.log")
    logs.note("release: `basix apply` for R1, output:\n  " + "\n  ".join(lines))
    check("apply1", "the apply exits 0", 0, rc)
    check("apply1", "the apply built and switched", True, iv.APPLIED in lines)
    check("apply1", "the lines name no store path and no git command", True, leak_free(lines))
    _, now = g.run("readlink -f /run/current-system; readlink -f /nix/var/nix/profiles/system")
    check(
        "apply1",
        "the running system and the profile are the system that the host built for R1",
        [str(prepared.top1.resolve())] * 2,
        now.split(),
    )
    rc, _ = g.run(f"test ! -e {MARKER}")
    check("apply1", "the visible change of R2 is absent", 0, rc)
    _, subject = g.run(f"git -C {WS} log -1 --format=%s; git -C {WS} status --porcelain")
    check(
        "apply1",
        "the apply saved its files and left a clean tree",
        True,
        subject.startswith("apply:") and subject.count("\n") == 0,
    )
    rc, _ = g.sudo(f"cd {WS} && basix apply > /tmp/apply1b.log 2>&1", 3600, stall=QUIET)
    check(
        "apply1",
        "the second apply exits 0 and says nothing to change",
        (0, True),
        (rc, "nothing to change" in g.lines("/tmp/apply1b.log")),
    )
    tick("vm: update1 and apply1", started)

    # --- the channel moves to R2
    started = time.monotonic()
    rc, _ = g.run(f"cp {MOUNT}/gitconfig-r2 ~/.gitconfig")
    check("update2", "the learner config maps the public URLs to the repositories of R2", 0, rc)
    rc, _ = g.run(f"cd {WS} && basix update > /tmp/update2.log 2>&1", 1800, stall=QUIET)
    lines = g.lines("/tmp/update2.log")
    logs.note("release: `basix update` to R2, output:\n  " + "\n  ".join(lines))
    FACTS["update_output"] = lines
    check("update2", "basix update exits 0", 0, rc)
    check("update2", "the output has five lines", 5, len(lines))
    check("update2", "line 1 names the two releases", f"Basix {r1} -> {r2}", lines[0] if lines else "")
    check(
        "update2",
        "line 2 is the nixpkgs age line",
        True,
        bool(lines[1:] and re.fullmatch(r"nixpkgs [0-9a-f]{8}, \d+ days? old", lines[1])),
    )
    check(
        "update2",
        "line 3 names the changelog and the file",
        f"{PYNIX_NEW} changes options that system.py sets:",
        lines[2] if len(lines) > 2 else "",
    )
    line4 = lines[3] if len(lines) > 3 else ""
    check("update2", "line 4 is the changelog line, with the name that sets it", f"  {CHANGE_LINE}  [programs]", line4)
    check("update2", "line 5 names the next step", NEXT_LINE, lines[-1] if lines else "")
    lock = g.lock()
    check("update2", "the lock holds the revision of R2", s.r2, node_of(lock, "basix")["locked"].get("rev"))
    pynix = node_of(lock, "basix", "pynix")["locked"]
    check(
        "update2",
        "the nested pynix is the scratch tag rc.93",
        (PYNIX_NEW, FACTS["pynix_new"]),
        (tag_of({"locked": pynix}), pynix.get("rev")),
    )
    check("update2", "the VM and the host made the same lock", prepared.lock2, lock)
    _, state = g.run(f"git -C {WS} status --porcelain")
    check("update2", "update changes only flake.lock in the tree", "M flake.lock", state.strip())
    _, now = g.run("readlink -f /run/current-system")
    check("update2", "update did not switch", str(prepared.top1.resolve()), now.strip())

    # --- apply R2
    rc, _ = g.sudo(f"cd {WS} && basix apply > /tmp/apply2.log 2>&1", 3600, stall=QUIET)
    lines = g.lines("/tmp/apply2.log")
    logs.note("release: `basix apply` for R2, output:\n  " + "\n  ".join(lines))
    check("apply2", "the apply exits 0", 0, rc)
    check("apply2", "the apply built and switched", True, iv.APPLIED in lines)
    check("apply2", "the lines name no store path and no git command", True, leak_free(lines))
    _, now = g.run("readlink -f /run/current-system; readlink -f /nix/var/nix/profiles/system")
    check(
        "apply2",
        "the running system and the profile are the system that the host built for R2",
        [str(prepared.top2.resolve())] * 2,
        now.split(),
    )
    _, marker = g.run(f"cat {MARKER}")
    check("apply2", "the file of R2 holds the R2 tag", r2, marker.strip())
    _, subject = g.run(f"git -C {WS} log -1 --format=%s; git -C {WS} status --porcelain")
    check(
        "apply2",
        "the apply saved flake.lock and left a clean tree",
        True,
        subject.startswith("apply:") and subject.count("\n") == 0,
    )
    rc, _ = g.sudo(f"cd {WS} && basix apply > /tmp/apply2b.log 2>&1", 3600, stall=QUIET)
    check(
        "apply2",
        "the second apply exits 0 and says nothing to change",
        (0, True),
        (rc, "nothing to change" in g.lines("/tmp/apply2b.log")),
    )
    tick("vm: update2 and apply2", started)

    # --- a second update
    before = g.lock()
    rc, _ = g.run(f"cd {WS} && basix update > /tmp/update3.log 2>&1", 1800, stall=QUIET)
    lines = g.lines("/tmp/update3.log")
    logs.note("release: second `basix update`, output:\n  " + "\n  ".join(lines))
    check("again", "basix update exits 0", 0, rc)
    check("again", "line 1 says that Basix is up to date", f"Basix is up to date ({r2})", lines[0] if lines else "")
    check("again", "the output has no pynix line", [], [line for line in lines if "pynix" in line])
    check("again", "the lock did not change", before, g.lock())
    check("again", "the last line names the next step", NEXT_LINE, lines[-1] if lines else "")
    rc, failed = g.run("systemctl is-system-running --wait; systemctl --failed --no-legend --plain", 300)
    check("again", "the system runs and no unit failed", "running", failed.strip())


def release_steps(vm: iv.Vm, s: Scratch, share: Path, token: str) -> None:
    """Run `vm_steps`. A stop in a step is a failure of the check, not a crash."""
    try:
        vm_steps(vm, s, share, token)
    except vmlib.Stop as error:
        tally("vm", False, f"release/vm: {str(error)[-1800:]}")
    except Exception as error:  # noqa: BLE001 - a crashed step must still power the VM off
        import traceback

        tally("vm", False, f"release/vm: unexpected {type(error).__name__}: {error}\n{traceback.format_exc()[-1500:]}")


class ShareSpawner:
    """Stands in for `subprocess` inside `install_vm`. It adds the 9p share to the QEMU command of the login VM.

    The live installer VM (it has a CD-ROM) gets no share: the hardware report of the install stays as the
    `nvme` scenario makes it, so the stored disk is the same disk.
    """

    def __init__(self, share: Path, logs: vmlib.Logs):
        self.share, self.logs = share, logs

    def __getattr__(self, name: str):
        return getattr(subprocess, name)

    def Popen(self, argv, *args, **kwargs):  # noqa: N802 - the name of the standard function
        if (
            argv
            and str(argv[0]).endswith("qemu-system-x86_64")
            and not any("media=cdrom" in str(item) for item in argv)
        ):
            extra = ["-virtfs", f"local,path={self.share},mount_tag={SHARE_TAG},security_model=none,id=hostshare"]
            self.logs.note("login VM gets the share: " + " ".join(extra))
            argv = [*argv, *extra]
        return subprocess.Popen(argv, *args, **kwargs)


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    os.environ.setdefault("BASIX_WORK", str(vmlib.workdir.default_work("010/P7/basix-vm")))
    os.environ.setdefault("BASIX_LOGS", str(Path.home() / ".local/state/basix/010/P7/basix-vm/logs"))
    logs = vmlib.Logs.create("release_vm")
    env = vmlib.clean_env()
    env["TMPDIR"] = str(vmlib.work_dir("vmtmp"))
    lab = iv.Lab(logs, root, env)
    work = vmlib.work_dir("release")
    began = time.monotonic()
    stub_url, public_url, branch = channel_names(root)
    version = version_of(root)
    s = Scratch(logs, work, stub_url, public_url, branch, version, f"v{version}-rc.91", f"v{version}-rc.92")
    vendor_pynix = source_repo("BASIX_RELEASE_PYNIX")
    vendor_minix = source_repo("BASIX_RELEASE_MINIX")
    share = work / "share"
    token = f"basix-release-{os.getpid()}-{int(time.time())}"
    try:
        logs.note(f"raw logs: {logs.root}; work {work}; channel {stub_url}; releases {s.tag1} and {s.tag2}")
        shutil.rmtree(s.cache, ignore_errors=True)  # a new cache for each run: the scratch tags move from run to run
        lab.space_check("the ISO build")
        lab.iso = iso_boot.build_iso(logs, root, env)
        lab.inputs = iv.iso_inputs(logs, root, env)
        logs.note(f"iso {lab.iso} sha256 {iv.sha256(lab.iso)}")
        lab.qemu, lab.ovmf = vmlib.qemu_tools(logs, root, env)
        owner_before = owner_cache_state()
        started = time.monotonic()
        prepare_channel(s, root, vendor_pynix, vendor_minix)
        tick("host: scratch channel", started)
        (share / "guest").mkdir(parents=True)
        (share / "ready").write_text(token + "\n")
        started = time.monotonic()
        iv.subprocess = ShareSpawner(share, logs)  # ty: ignore[invalid-assignment]
        try:
            iv.install_and_login(
                lab, SCENARIO, "q35-nvme", extra=lambda vm: release_steps(vm, s, share, token), cache="use"
            )
        finally:
            iv.subprocess = subprocess
        tick("vm: install or reuse, boot, steps", started)
        prove_cache(s, owner_before)
    except vmlib.Stop as error:
        tally("run", False, f"release/run: {str(error)[-1800:]}")
    except Exception as error:  # noqa: BLE001 - the summary and the logs must still be written
        import traceback

        tally(
            "run", False, f"release/run: unexpected {type(error).__name__}: {error}\n{traceback.format_exc()[-1500:]}"
        )
    failed = [text for ok, text in iv.RESULTS if not ok]
    total = round(time.monotonic() - began, 1)
    summary = {"counts": COUNTS, "times_s": TIMES, "total_s": total, "facts": FACTS, "failed": failed}
    (logs.root / "results.json").write_text(json.dumps(summary, indent=2, default=str) + "\n")
    print("--- cases")
    for name, (passed, bad) in COUNTS.items():
        print(f"{name}: {passed} passed, {bad} failed")
    print("--- times (s): " + ", ".join(f"{k} {v}" for k, v in TIMES.items()) + f"; total {total}")
    if FACTS.get("update_output"):
        print("--- basix update to R2 in the VM:\n" + "\n".join(FACTS["update_output"]))
    if not failed:
        shutil.rmtree(lab.work, ignore_errors=True)
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"scratch kept: {lab.work} and {work}")
    print(f"{len(iv.RESULTS) - len(failed)} passed, {len(failed)} failed; logs {logs.root}")
    print("GREEN" if not failed else "RED")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
