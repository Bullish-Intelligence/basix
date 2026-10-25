# `lib.inputPackages`: the typed Python package `pynix_NAME` of each extra flake input (009-D47, 009-D48, 010-D13).
#
# The managed stub calls it as `packages.<system>.input-packages`. `basix apply` builds that output to `.basix/pynix`
# before it compiles `system.py`, because the entry imports the classes of the package. The options come from the
# locked input and from the NixOS base options, so the package follows `flake.lock`.
#
# `extraInputs` is what the stub passes to `lib.machine`: every flake input but `self`, `basix`, and `nixpkgs`. The
# `nixpkgs-NAME` inputs are pins of `NixpkgsPin("NAME", commit)`. They have no options, so they get no classes. They get
# the package list `pynix_nixpkgs_NAME` instead, so `NAME.pkgs.PACKAGE` finds its packages (009-D53, 009-D54).
{ self, inputs }:
{ extraInputs, modules ? { }, system ? "x86_64-linux" }:
let
  lib = inputs.nixpkgs.lib;
  pins = lib.mapAttrs' (n: v: lib.nameValuePair (lib.removePrefix "nixpkgs-" n) v)
    (lib.filterAttrs (n: _: lib.hasPrefix "nixpkgs-" n) extraInputs);
in
import "${inputs.pynix}/nix/input-packages.nix" ({
  pkgs = inputs.nixpkgs.legacyPackages.${system};
  layer = "nixos";
  inputs = lib.filterAttrs (n: _: !(lib.hasPrefix "nixpkgs-" n)) extraInputs;
  inherit modules;
  nixpkgs = inputs.nixpkgs;
  src = "${inputs.pynix}/src";
  specialArgs.inputs = { inherit (inputs) nixpkgs disko; basix = self; } // extraInputs;
}
# Only an entry with a `NixpkgsPin(...)` passes the argument, so a pynix that predates it still serves the other entries.
// lib.optionalAttrs (pins != { }) { nixpkgsInputs = pins; })
