"""The compile step of `basix apply` (CONCEPT 4.1, step 1; 009-D49).

Order: read the `Input(...)` calls from the syntax tree of `system.py`, write the stub when they changed, lock the
new inputs, make the input packages (every run, when the entry has inputs: 010-D13), build the compiler from the
workspace lock, run it, and build the machine as root when a cache is new (L134). A wrong value stops here with one
plain line, and nothing else changes.
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pynix_engine import Session, steps
from pynix_native import DiscoveryError, Found, discover

from basix.caches import new_caches
from basix.layer import NIX_FEATURES, SUDO, Host, ensure_sudo, machine_attr, stopped
from basix_config.stub import DEFAULT_BASIX_URL, HEADER, input_list_expr, make_stub

# The flake output that builds the input packages (`pynix_NAME`) of the workspace. The stub must define it.
INPUT_PACKAGES_ATTR = ".#input-packages"
PACKAGES_LINK = Path(".basix") / "pynix"
IGNORED = ".basix/"

# The URL line of the managed stub. A later apply keeps the URL that the stub already holds.
STUB_URL = re.compile(r'inputs\.basix\.url\s*=\s*"([^"]+)"')

# Reads the names in `inputs` of a flake input: the text of a flake reference to a list of names, or None.
InputList = Callable[[str], list[str] | None]


def stub_url(workspace: Path) -> str | None:
    """The URL of the `basix` input in the stub of the workspace, or None when there is no stub or no such line."""
    try:
        found = STUB_URL.search((workspace / "flake.nix").read_text())
    except OSError:
        return None
    return found.group(1) if found else None


def nix_inputs(session: Session, url: str) -> list[str] | None:
    """The names in `inputs` of the `flake.nix` of an input, read from its fetched source (offline first).

    The expression honours the `dir=` part of the URL (PF-6).
    """
    expr = input_list_expr(url)
    for offline in (["--offline"], []):
        result = session.run(["nix", "eval", *offline, "--json", "--impure", "--expr", expr], quiet=True)
        if result.code == 0:
            try:
                return json.loads(result.out[result.out.index("[") :])
            except ValueError:
                return None
    return None


def read_found(session: Session) -> Found:
    """The `Input` and `NixpkgsPin` calls of `system.py`. Stop with one line when the file is missing or unreadable."""
    entry = session.ws / session.layer.entry
    if not entry.is_file():
        raise stopped(session.word("no_entry"))
    try:
        return discover(entry)
    except DiscoveryError as error:
        raise stopped(str(error)) from None


def ensure_ignored(session: Session) -> None:
    """Keep `.basix/` out of git: it holds a link to the store, and the stage step refuses such a link."""
    if session.git("check-ignore", "-q", str(PACKAGES_LINK), check=False).code == 0:
        return
    ignore = session.ws / ".gitignore"
    old = ignore.read_text() if ignore.exists() else ""
    ignore.write_text(old + ("" if not old or old.endswith("\n") else "\n") + IGNORED + "\n")
    session.say(session.word("scratch_ignored", names=IGNORED))


def lock_inputs(session: Session) -> None:
    """Lock the inputs of the stub. A new lock must not look like an edit, so apply stages it afterwards."""
    result = session.run(["nix", "flake", "lock", "--no-warn-dirty"])
    if result.code != 0:
        raise stopped(session.word("lock_failed", detail=steps.plain(steps.trim_nix_error(result.out))))


def build_input_packages(session: Session) -> None:
    """Build the input packages and link them to `.basix/pynix`. The compiler and ty read that folder."""
    (session.ws / PACKAGES_LINK).parent.mkdir(exist_ok=True)
    argv = ["nix", "build", "--out-link", str(session.ws / PACKAGES_LINK), "--print-out-paths", "--no-warn-dirty"]
    result = session.run([*argv, INPUT_PACKAGES_ATTR])
    if result.code != 0:
        detail = steps.plain(steps.trim_nix_error(result.out))
        raise stopped(session.word("inputs_build_failed", detail=detail))


def make_input_packages(session: Session, found: Found) -> None:
    """Build the input packages on every run when the entry has inputs that need them (010-D13)."""
    if any(item.flake for item in found.inputs) or found.nixpkgs:
        build_input_packages(session)


def build_with_new_caches(session: Session, host: Host, has_terminal: bool) -> None:
    """L134: when `system.py` adds a cache that the running system lacks, root builds the machine with it.

    The account evaluates the machine and gets the derivation. Root then builds that derivation, with the new caches
    as `--option` values, because a cache option of an account that Nix does not trust is ignored. The build that the
    engine runs next finds the result in the store.
    """
    compiled = json.loads((session.ws / session.layer.json_file).read_text())
    caches = new_caches(compiled, host.nix_conf)
    if caches is None:
        return
    urls, keys = caches
    session.say(session.word("caches_building"))
    log = session.display(session.log_path)
    ensure_sudo(session, has_terminal)
    evaluated = session.run(["nix", "eval", "--raw", "--no-warn-dirty", f"{machine_attr(session.ws)}.drvPath"])
    drv = evaluated.out.strip().splitlines()[-1] if evaluated.out.strip() else ""
    if evaluated.code != 0 or not drv.endswith(".drv"):
        detail = steps.plain(steps.trim_nix_error(evaluated.out))
        raise stopped(session.word("caches_failed", detail=detail, log=log))
    argv = [SUDO, "nix", *NIX_FEATURES, "build", "--no-link", "--print-out-paths"]
    argv += ["--option", "extra-substituters", " ".join(urls)]
    argv += ["--option", "extra-trusted-public-keys", " ".join(keys), f"{drv}^*"]
    built = session.run(argv)
    if built.code != 0:
        raise stopped(session.word("caches_failed", detail=steps.plain(steps.trim_nix_error(built.out)), log=log))


@dataclass(frozen=True)
class Compiler:
    """The compile step of the Basix layer. `Layer.compile` calls it with the session."""

    host: Host
    has_terminal: bool
    read_inputs: InputList | None = None  # None: read the inputs through the Nix runner of the session

    def __call__(self, session: Session) -> None:
        found = read_found(session)
        self.write_stub(session, found)
        steps.stage(session)  # the flake sees tracked files only
        make_input_packages(session, found)
        tool = steps.build_compiler(session)
        steps.run_compiler(session, tool)
        build_with_new_caches(session, self.host, self.has_terminal)

    def write_stub(self, session: Session, found: Found) -> None:
        """Write the managed stub. Lock the inputs when the stub changed or no lock exists."""
        stub = session.ws / "flake.nix"
        before = stub.read_text() if stub.exists() else None
        if before is not None and before.splitlines()[:1] != [HEADER]:
            raise stopped(session.word("not_a_stub", path=session.display(stub), header=HEADER))
        inspect = self.read_inputs or (lambda url: nix_inputs(session, url))
        try:
            made = make_stub(found, stub_url(session.ws) or DEFAULT_BASIX_URL, inspect)
        except ValueError as error:
            raise stopped(str(error)) from None
        for line in made.notes:
            session.say(line)
        if before != made.text:
            stub.write_text(made.text)
        if before != made.text or not (session.ws / "flake.lock").exists():
            lock_inputs(session)
