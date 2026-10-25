"""`lib.inputPackages` hands the `nixpkgs-NAME` inputs to pynix as named nixpkgs pins (009-D53, 009-D54, PF-2).

The test replaces pynix with a stub that returns its arguments. It proves what Basix passes, with no build. It needs
`nix` and the pinned nixpkgs library in the store, and no network: a missing prerequisite skips it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("nix") is None, reason="nix is not installed")

#: `lib` from the nixpkgs of `flake.lock`, the stub pynix `pynix`, and the function as the stub of a workspace calls it.
EXPRESSION = """
let
  lock = (builtins.fromJSON (builtins.readFile "@ROOT@/flake.lock")).nodes;
  lib = import ((builtins.fetchTree (builtins.removeAttrs lock.nixpkgs.locked [ "dir" ])) + "/lib");
  call = import "@ROOT@/lib/input-packages.nix" {
    self = null;
    inputs = {
      nixpkgs = { inherit lib; legacyPackages.x86_64-linux = { }; };
      disko = null;
      pynix = "@PYNIX@";
    };
  };
  got = call { extraInputs = { demo.id = "demo"; } // (@PINS@); };
in
{ inputs = got.inputs; nixpkgsInputs = got.nixpkgsInputs or null; }
"""


def evaluate(tmp_path: Path, pins: str) -> dict:
    (tmp_path / "nix").mkdir()
    (tmp_path / "nix" / "input-packages.nix").write_text("args: args\n")
    expression = EXPRESSION.replace("@ROOT@", str(ROOT)).replace("@PYNIX@", str(tmp_path)).replace("@PINS@", pins)
    done = subprocess.run(
        [
            "nix",
            "--extra-experimental-features",
            "nix-command flakes",
            "--offline",
            "eval",
            "--json",
            "--impure",
            "--expr",
            expression,
        ],
        capture_output=True,
        text=True,
    )
    if "fetching" in done.stderr or "unable to download" in done.stderr:
        pytest.skip(f"the pinned nixpkgs of flake.lock is not in the store: {done.stderr[-300:]}")
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout)


def test_named_nixpkgs_inputs_go_to_pynix_as_pins(tmp_path: Path) -> None:
    """Before PF-2, `nixpkgs-*` inputs were dropped, so `NixpkgsPin(name, commit)` had no package list."""
    got = evaluate(tmp_path, '{ nixpkgs-old.id = "old"; nixpkgs-older.id = "older"; }')
    assert got["inputs"] == {"demo": {"id": "demo"}}
    assert got["nixpkgsInputs"] == {"old": {"id": "old"}, "older": {"id": "older"}}


def test_an_entry_with_no_pin_passes_no_argument(tmp_path: Path) -> None:
    """A pynix that predates `nixpkgsInputs` rejects the argument, so Basix leaves it out when there is no pin."""
    got = evaluate(tmp_path, "{ }")
    assert got == {"inputs": {"demo": {"id": "demo"}}, "nixpkgsInputs": None}
