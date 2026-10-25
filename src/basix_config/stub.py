"""The stub writer: the managed `flake.nix` of a Basix workspace (L129, 009-D51 to 009-D53, CONCEPT Appendix C).

With no `Input(...)` and no `NixpkgsPin(...)` in the entry, the stub holds one input, `basix`, and its text never
changes. With an input, the stub holds the extra inputs, `extraInputs` hands them to `lib.machine`, and the output
`input-packages` builds their typed packages (`lib.inputPackages`). The learner
never edits the stub (L15). `basix apply` writes it before it locks and compiles, from the calls that
`pynix_native.discover` reads from the syntax tree of the entry (009-D49).

Rules for an input (009-D51, 009-D53):

- It follows `nixpkgs` (the nixpkgs of the layer), when the input has a `nixpkgs` input of its own.
- `nixpkgs=OWN`: no `follows` line, so the input keeps its own locked nixpkgs.
- `nixpkgs=NAME`: it follows `nixpkgs-NAME`, the input that `NixpkgsPin("NAME", COMMIT)` adds.
- `flake=False`: a plain source folder, with `flake = false` and no `follows` line.
- A `follows` line for an input that does not exist makes Nix warn. So the writer reads the input list of each input
  first (`nix_input_list`) and writes only the lines that fit.

The exact text with an `Input` is a decision of slice P2 (009-D52 point 6): the text below is the spike text of
SG44 with `lib.machine` and `root = ./.;`.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from pynix_native import Found, FoundInput, FoundNixpkgsPin

__all__ = [
    "DEFAULT_BASIX_URL",
    "HEADER",
    "INPUTS_ARGS",
    "Stub",
    "extra_inputs_expr",
    "input_list_expr",
    "make_stub",
    "nix_input_list",
    "write_stub",
]

#: The first line of the managed file (009-D3).
HEADER = "# Managed by Basix. Do not edit."
#: The flake reference of Basix (010-D15, 012-D6). It follows the branch `release` (010-D54) of the public repository.
DEFAULT_BASIX_URL = "git+https://github.com/Bullish-Intelligence/basix?ref=release"

#: The `outputs` argument pattern of a stub with inputs.
INPUTS_ARGS = "inputs@{ self, basix, ... }"


def extra_inputs_expr(*skip: str) -> str:
    """The Nix value of `extraInputs`. A caller with an input of its own (`basix-try --home`) adds its name to `skip`."""
    names = " ".join(f'"{name}"' for name in ("self", "basix", "nixpkgs", *skip))
    return f"removeAttrs inputs [ {names} ]"


_FLAKE_REF = re.compile(r"[A-Za-z0-9+._:/?&=@%~#,-]+")

#: Reads the names of the `inputs` of an input: the text of a flake reference to a list of names, or None.
InputList = Callable[[str], list[str] | None]


@dataclass(frozen=True)
class Stub:
    """The text of the stub, and the notes for the learner (one plain line each)."""

    text: str
    notes: tuple[str, ...] = ()
    #: The input lines of the stub, with no indent. Empty when the entry has no input and no pin.
    inputs: tuple[str, ...] = ()
    #: The lines of the output `input-packages`, with no indent. Empty when `inputs` is empty.
    packages: tuple[str, ...] = ()


def input_list_expr(url: str) -> str:
    """The Nix expression that lists the names in `inputs` of the `flake.nix` of the flake reference `url`.

    The `dir=` part of the reference names the folder of the flake inside the fetched source. `fetchTree` does not take
    it, so the expression removes it and reads `DIR/flake.nix`. The other parts (`ref`, `rev`) go to `fetchTree`.
    """
    return (
        f'let ref = builtins.parseFlakeRef "{url}";'
        ' src = builtins.fetchTree (builtins.removeAttrs ref [ "dir" ]);'
        ' in builtins.attrNames (import (src + "/${ref.dir or "."}/flake.nix")).inputs or { }'
    )


def nix_input_list(url: str) -> list[str] | None:
    """The names in `inputs` of the `flake.nix` of an input, read from its fetched source.

    The first try is offline, so a source in the fetcher cache costs no network. The second try may fetch: apply
    needs the source for the lock in any case. None means that Nix could not read the list.
    """
    expr = input_list_expr(url)
    for offline in (["--offline"], []):
        try:
            done = subprocess.run(
                ["nix", "eval", *offline, "--json", "--impure", "--expr", expr],
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode == 0:
            return json.loads(done.stdout)
    return None


def _nix_string(text: str) -> str:
    return json.dumps(text)


def _pins(found: Found) -> dict[str, str]:
    """Every `NixpkgsPin(...)` of the entry, name to commit: the ones in `found.nixpkgs` and the ones that an input names."""
    pins = {pin.name: pin.commit for pin in found.nixpkgs}
    for item in found.inputs:
        if isinstance(item.nixpkgs, FoundNixpkgsPin):
            pins[item.nixpkgs.name] = item.nixpkgs.commit
    return pins


def _stanza(item: FoundInput, have: list[str] | None, notes: list[str]) -> list[str]:
    name = item.name
    out = [f"inputs.{name}.url = {_nix_string(item.url)};"]
    if not item.flake:
        out.append(f"inputs.{name}.flake = false;")
        return out
    if have is None:
        notes.append(f"{item.source}: could not read the inputs of `{name}`, so the stub has no follows line for it")
        have = []
    want = item.nixpkgs
    if "nixpkgs" in have:
        if want is None:
            out.append(f'inputs.{name}.inputs.nixpkgs.follows = "nixpkgs";')
        elif isinstance(want, FoundNixpkgsPin):
            out.append(f'inputs.{name}.inputs.nixpkgs.follows = "nixpkgs-{want.name}";')
        # OWN: no line, so the input keeps its own locked nixpkgs
    elif isinstance(want, FoundNixpkgsPin):
        notes.append(
            f"{item.source}: the input `{name}` has no `nixpkgs` input, so `nixpkgs={want.name}` changes nothing for it"
        )
    return out


def make_stub(
    found: Found | None = None, basix_url: str = DEFAULT_BASIX_URL, inspect: InputList = nix_input_list
) -> Stub:
    """The text of the managed `flake.nix`.

    `found` holds the calls of the entry (`pynix_native.discover`); None or an empty `Found` gives the plain stub.
    `inspect` reads the inputs of an input; a test passes a fake.
    """
    if not _FLAKE_REF.fullmatch(basix_url):
        raise ValueError(
            f"the flake reference of Basix has a character that a flake reference cannot hold: {basix_url!r}"
        )
    inputs = found.inputs if found else ()
    pins = _pins(found) if found else {}
    notes: list[str] = []
    input_lines: list[str] = []
    package_lines: list[str] = []
    if inputs or pins:
        input_lines.append('inputs.nixpkgs.follows = "basix/nixpkgs";')
        input_lines += [
            f'inputs.nixpkgs-{name}.url = "github:NixOS/nixpkgs/{commit}";' for name, commit in sorted(pins.items())
        ]
        for item in inputs:
            have = inspect(item.url) if item.flake else None
            input_lines += _stanza(item, have, notes)
        # The typed packages of the inputs: `basix apply` builds this output before it compiles `system.py`.
        modules = " ".join(
            f"{item.name} = [ {' '.join(_nix_string(m) for m in item.modules)} ];"
            for item in inputs
            if item.flake and item.modules
        )
        package_lines = [
            "packages.x86_64-linux.input-packages = basix.lib.inputPackages {",
            f"  extraInputs = {extra_inputs_expr()};",
            f"  modules = {{ {modules} }};" if modules else "  modules = { };",
            "};",
        ]
    lines = [HEADER, "{", f"  inputs.basix.url = {_nix_string(basix_url)};"]
    lines += [f"  {line}" for line in input_lines]
    lines += [
        f"  outputs = {INPUTS_ARGS if input_lines else '{ self, basix }'}: {{",
        "    packages.x86_64-linux.compiler = basix.packages.x86_64-linux.compiler;",
    ]
    lines += [f"    {line}" for line in package_lines]
    lines += [
        "    nixosConfigurations = basix.lib.machine {",
        "      compiled = builtins.fromJSON (builtins.readFile ./system.json);",
        "      source = ./system.py;",
        "      root = ./.;",
    ]
    if input_lines:
        lines.append(f"      extraInputs = {extra_inputs_expr()};")
    lines += ["    };", "  };", "}"]
    return Stub("\n".join(lines) + "\n", tuple(notes), tuple(input_lines), tuple(package_lines))


def write_stub(
    workspace: Path, found: Found | None = None, basix_url: str = DEFAULT_BASIX_URL, inspect: InputList = nix_input_list
) -> Stub:
    """Write `flake.nix` in the workspace folder. Returns the stub, so the caller can print its notes."""
    stub = make_stub(found, basix_url, inspect)
    (workspace / "flake.nix").write_text(stub.text)
    return stub
