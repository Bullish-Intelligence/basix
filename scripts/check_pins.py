"""Check the pin layout of the Basix flake (BSX-070, BSX-146, 010-D8).

Usage:
    python3 -I scripts/check_pins.py [ROOT]    the check
    python3 -I scripts/check_pins.py --selftest    run the built-in cases

The check fails when:
- an input of `flake.nix` that names a 40-character revision differs from the revision in `flake.lock`;
- `flake.nix` and `flake.lock` hold different input names;
- the input `pynix` is not a tag (`?ref=refs/tags/pynix-v...`) with `flake = false`, or its lock node differs;
- an input that has a `nixpkgs` (or `nixpkgs-lib`) input of its own does not follow `nixpkgs`, so the lock
  holds more than one `nixpkgs` node;
- the input `nixpkgs` of `devenv.yaml` differs from the pin of `flake.nix`, or an input that both files name
  has another URL, or the `nixpkgs` revision of `devenv.lock` differs from `flake.lock`;

The check does not scan for owner facts. `scripts/check_public.py` does (BSX-415).
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

#: The pins that name a revision in the URL (010 pin set).
REQUIRED = [
    "nixpkgs",
    "flake-parts",
    "disko",
    "nixos-facter-modules",
    "pynix",
    "pyproject-nix",
    "uv2nix",
    "pyproject-build-systems",
]
#: The input names that stand for a nixpkgs inside another flake.
NIXPKGS_INPUTS = ("nixpkgs", "nixpkgs-lib")

PIN_TAG = re.compile(r"^git\+https://[^?]+\?ref=refs/tags/pynix-v[0-9][^&\s]*$")
FLAKE_URL = re.compile(r'^\s*([A-Za-z0-9_-]+)\.url\s*=\s*"([^"]+)";')
FLAKE_FALSE = re.compile(r"^\s*([A-Za-z0-9_-]+)\.flake\s*=\s*false;")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def lock_nodes(data: dict) -> dict[str, dict]:
    """The lock node of each root input, by input name."""
    nodes = data["nodes"]
    return {name: nodes[target] for name, target in nodes["root"]["inputs"].items() if isinstance(target, str)}


def flake_urls(text: str) -> dict[str, str]:
    """Map input name to URL from the `NAME.url = "...";` lines of flake.nix."""
    urls: dict[str, str] = {}
    for line in text.splitlines():
        match = FLAKE_URL.match(line)
        if match:
            urls[match.group(1)] = match.group(2)
    return urls


def yaml_inputs(text: str) -> dict[str, str]:
    """Map input name to URL from the simple `inputs:` block of devenv.yaml."""
    urls: dict[str, str] = {}
    in_inputs = False
    current: str | None = None
    for line in text.splitlines():
        if re.match(r"^inputs:\s*$", line):
            in_inputs = True
            continue
        if in_inputs and re.match(r"^\S", line) and not line.startswith("#"):
            in_inputs = False
        if not in_inputs:
            continue
        name = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if name:
            current = name.group(1)
            continue
        url = re.match(r"^    url:\s*(\S+)", line)
        if url and current:
            urls[current] = url.group(1)
    return urls


def rev_of(url: str) -> str | None:
    """The 40-character revision a URL names, or None."""
    match = re.search(r"[0-9a-f]{40}", url)
    return match.group(0) if match else None


def lock_errors(flake_text: str, urls: dict[str, str], data: dict) -> list[str]:
    errors: list[str] = []
    nodes = data["nodes"]
    roots = nodes["root"]["inputs"]
    lock = lock_nodes(data)

    # 1. the same input names in both files, and the required inputs
    for name in REQUIRED:
        if name not in urls:
            errors.append(f"flake.nix: input {name} is missing")
    for name in sorted(set(urls) ^ set(roots)):
        where = "flake.nix" if name in urls else "flake.lock"
        errors.append(f"{where}: input {name} is not in the other file")

    # 2. a pinned revision in the URL equals the locked revision
    for name, url in urls.items():
        want = rev_of(url)
        have = lock.get(name, {}).get("locked", {}).get("rev")
        if want is not None and name in lock and want != have:
            errors.append(f"flake.lock: input {name} is at {have}, flake.nix pins {want}")
        if want is None and name not in {"pynix"} and name in REQUIRED:
            errors.append(f"flake.nix: input {name} names no 40-character revision")

    # 3. the input pynix is a tag, and a source folder (010-D8)
    if "pynix" in urls:
        if not PIN_TAG.match(urls["pynix"]):
            errors.append(f"flake.nix: input pynix must name a tag (git+https://...?ref=refs/tags/pynix-vX.Y.Z), found {urls['pynix']}")
        if not re.search(r"^\s*pynix\.flake\s*=\s*false;", flake_text, re.MULTILINE):
            errors.append("flake.nix: input pynix must have `flake = false`")
        node = lock.get("pynix")
        if node is not None:
            if node.get("flake") is not False:
                errors.append("flake.lock: input pynix must have `flake: false`")
            ref = node.get("original", {}).get("ref")
            want_ref = re.search(r"\?ref=([^&\s]+)", urls["pynix"])
            if want_ref and ref != want_ref.group(1):
                errors.append(f"flake.lock: input pynix is at {ref}, flake.nix names {want_ref.group(1)}")
            if not node.get("locked", {}).get("rev") or not node.get("locked", {}).get("narHash"):
                errors.append("flake.lock: input pynix has no rev and narHash")

    # 4. one nixpkgs: every input that has a nixpkgs follows it
    extra = sorted(n for n in nodes if n.startswith("nixpkgs") and n != "nixpkgs")
    if extra:
        errors.append(f"flake.lock: more than one nixpkgs node ({', '.join(['nixpkgs', *extra])}); make each input follow nixpkgs")
    for name, node in nodes.items():
        for key, target in node.get("inputs", {}).items():
            if key in NIXPKGS_INPUTS and target not in (["nixpkgs"], "nixpkgs"):
                errors.append(f"flake.lock: input {name}/{key} does not follow nixpkgs (found {target!r})")
    return errors


def devenv_errors(root: Path, urls: dict[str, str], lock: dict[str, dict]) -> list[str]:
    """`devenv.yaml` repeats the nixpkgs pin for the development project. `devenv.lock` follows `flake.lock`."""
    errors: list[str] = []
    yaml_path = root / "devenv.yaml"
    if yaml_path.exists():
        yurls = yaml_inputs(yaml_path.read_text(encoding="utf-8"))
        if "nixpkgs" not in yurls:
            errors.append("devenv.yaml: input nixpkgs is missing")
        for name, url in yurls.items():
            if name in urls and url != urls[name]:
                errors.append(f"devenv.yaml: input {name} is {url}, flake.nix has {urls[name]}")
    lock_path = root / "devenv.lock"
    if lock_path.exists():
        have = lock_nodes(read_json(lock_path)).get("nixpkgs", {}).get("locked", {}).get("rev")
        want = lock.get("nixpkgs", {}).get("locked", {}).get("rev")
        if have != want:
            errors.append(f"devenv.lock: input nixpkgs is at {have}, flake.lock has {want}")
    return errors


def check(root: Path) -> list[str]:
    flake_text = (root / "flake.nix").read_text(encoding="utf-8")
    urls = flake_urls(flake_text)
    data = read_json(root / "flake.lock")
    errors = lock_errors(flake_text, urls, data)
    errors.extend(devenv_errors(root, urls, lock_nodes(data)))
    return errors


# --- self test --------------------------------------------------------------------------------

FLAKE = """{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/%(rev)s";
    flake-parts.url = "github:hercules-ci/flake-parts/%(rev)s";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";
    disko.url = "github:nix-community/disko/%(rev)s";
    disko.inputs.nixpkgs.follows = "nixpkgs";
    nixos-facter-modules.url = "github:nix-community/nixos-facter-modules/%(rev)s";
    pynix.url = "git+https://github.com/example/pynix?ref=refs/tags/pynix-v0.1.0-rc.1";
    pynix.flake = false;
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix/%(rev)s";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.url = "github:pyproject-nix/uv2nix/%(rev)s";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs/%(rev)s";
    pyproject-build-systems.inputs.nixpkgs.follows = "nixpkgs";
  };
}
"""

YAML = """inputs:
  nixpkgs:
    url: github:NixOS/nixpkgs/%(rev)s
  disko:
    url: github:nix-community/disko/%(rev)s
    inputs:
      nixpkgs:
        follows: nixpkgs
"""


def sample_lock(rev: str) -> dict:
    github = ["nixpkgs", "flake-parts", "disko", "nixos-facter-modules", "pyproject-nix", "uv2nix", "pyproject-build-systems"]
    nodes: dict = {name: {"locked": {"type": "github", "rev": rev, "narHash": "sha256-x"}} for name in github}
    for name in ("flake-parts", "disko", "pyproject-nix", "uv2nix", "pyproject-build-systems"):
        key = "nixpkgs-lib" if name == "flake-parts" else "nixpkgs"
        nodes[name]["inputs"] = {key: ["nixpkgs"]}
    nodes["pynix"] = {
        "flake": False,
        "locked": {"type": "git", "rev": "5" * 40, "narHash": "sha256-y", "ref": "refs/tags/pynix-v0.1.0-rc.1"},
        "original": {"type": "git", "ref": "refs/tags/pynix-v0.1.0-rc.1"},
    }
    nodes["root"] = {"inputs": {name: name for name in [*github, "pynix"]}}
    return {"nodes": nodes, "root": "root", "version": 7}


def selftest() -> int:
    rev = "a" * 40
    failures = 0

    def case(name: str, expect_errors: bool, mutate=None) -> None:
        nonlocal failures
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "flake.nix").write_text(FLAKE % {"rev": rev}, encoding="utf-8")
            (root / "flake.lock").write_text(json.dumps(sample_lock(rev)), encoding="utf-8")
            (root / "devenv.yaml").write_text(YAML % {"rev": rev}, encoding="utf-8")
            if mutate:
                mutate(root)
            errors = check(root)
        ok = bool(errors) == expect_errors
        print(("PASS" if ok else "FAIL") + f" selftest {name}" + ("" if ok else f": {errors}"))
        failures += 0 if ok else 1

    def edit_flake(old: str, new: str):
        def mutate(root: Path) -> None:
            text = (root / "flake.nix").read_text(encoding="utf-8")
            assert old in text, old
            (root / "flake.nix").write_text(text.replace(old, new, 1), encoding="utf-8")

        return mutate

    def edit_lock(change):
        def mutate(root: Path) -> None:
            data = read_json(root / "flake.lock")
            change(data)
            (root / "flake.lock").write_text(json.dumps(data), encoding="utf-8")

        return mutate

    def write(relative: str, text: str):
        def mutate(root: Path) -> None:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

        return mutate

    def move_nixpkgs(data: dict) -> None:
        data["nodes"]["nixpkgs"]["locked"]["rev"] = "b" * 40

    def split_nixpkgs(data: dict) -> None:
        data["nodes"]["nixpkgs_2"] = {"locked": {"type": "github", "rev": "c" * 40}}
        data["nodes"]["uv2nix"]["inputs"]["nixpkgs"] = "nixpkgs_2"

    def own_nixpkgs_lib(data: dict) -> None:
        data["nodes"]["flake-parts"]["inputs"]["nixpkgs-lib"] = "nixpkgs_lib"

    def pynix_with_flake(data: dict) -> None:
        del data["nodes"]["pynix"]["flake"]

    def drop_input(data: dict) -> None:
        del data["nodes"]["root"]["inputs"]["disko"]

    case("a consistent layout passes", expect_errors=False)
    case("a lock revision that differs fails", expect_errors=True, mutate=edit_lock(move_nixpkgs))
    case("a second nixpkgs node fails", expect_errors=True, mutate=edit_lock(split_nixpkgs))
    case("a nixpkgs-lib that does not follow fails", expect_errors=True, mutate=edit_lock(own_nixpkgs_lib))
    case("an input that the lock lacks fails", expect_errors=True, mutate=edit_lock(drop_input))
    case("a pynix input that is a branch fails", expect_errors=True, mutate=edit_flake("?ref=refs/tags/pynix-v0.1.0-rc.1", "?ref=main"))
    case("a pynix input with no flake = false fails", expect_errors=True, mutate=edit_flake("    pynix.flake = false;\n", ""))
    case("a pynix lock node that is a flake fails", expect_errors=True, mutate=edit_lock(pynix_with_flake))
    case("a devenv.yaml nixpkgs that differs fails", expect_errors=True, mutate=write("devenv.yaml", YAML % {"rev": "d" * 40}))
    case("a devenv.yaml with a missing nixpkgs fails", expect_errors=True, mutate=write("devenv.yaml", "inputs: {}\n"))
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    args = [arg for arg in argv[1:] if not arg.startswith("--")]
    flags = {arg for arg in argv[1:] if arg.startswith("--")}
    unknown = flags - {"--selftest"}
    if unknown:
        print(f"check_pins: unknown option {sorted(unknown)[0]}", file=sys.stderr)
        return 2
    if "--selftest" in flags:
        return selftest()
    root = Path(args[0] if args else ".").resolve()
    errors = check(root)
    for error in errors:
        print(f"FAIL {error}")
    if errors:
        return 1
    print("PASS pins")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
