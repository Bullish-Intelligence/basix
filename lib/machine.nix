# `lib.machine`: the compiled JSON of `system.py` becomes `nixosConfigurations` (CONCEPT section 5, Appendix C).
# The module list is the core, the profile that the JSON names, one module per fragment, the native modules, and
# the modules of the inputs. A file `facter.json` in the workspace is the hardware report: the install writes it.
#
# Four guards stop an evaluation with one plain line:
#   - the pair check: `pynix_os` must come from the nixpkgs that `flake.lock` holds (009-D28);
#   - the staleness guard: `system.py` and every file that it read must equal the compile (009-D45, `guard.nix`);
#   - the input check: every `Input` and `NixpkgsPin` of the entry has its input in the stub (009-D49, 009-D53);
#   - the input-package pair check: the package `pynix_NAME` must come from the locked input (009-D48).
{ self, inputs }:
{ compiled                 # the decoded JSON: { name, profile, fragments, modules, source_sha256, reads, ... }
, source                   # the file `system.py` of the workspace (a path), for the guard
, root ? null              # the folder of the workspace (a path); the folder of `source` when null
, system ? "x86_64-linux"
, extraModules ? [ ]
, extraInputs ? { }        # the flake inputs that the stub passes: all of them but `self`, `basix`, and `nixpkgs`
}:
let
  lib = inputs.nixpkgs.lib;
  workspace = if root != null then root else dirOf source;
  pynix = inputs.pynix;
  decode = fragment: import "${pynix}/src/decoder.nix" { inherit fragment; nativeRoot = workspace; };

  # The pair check (009-D28). `_generation.py` of the generated library names the nixpkgs revision that the
  # library came from. The reader finds the `SOURCE` line and takes the revision from it.
  generated =
    let
      text = builtins.readFile "${pynix}/src/pynix_os/_generation.py";
      line = lib.findFirst (l: lib.hasPrefix "SOURCE" l) "" (lib.splitString "\n" text);
      found = builtins.match ".*\"nixpkgs\": *\"([0-9a-f]+)\".*" line;
    in
    if found == null then null else builtins.head found;
  locked = inputs.nixpkgs.rev or null;
  pairProblem =
    if generated == null then "basix: the `pynix` input holds no record of the nixpkgs revision of `pynix_os`. Use a `pynix` tag from a release"
    else if locked != null && generated != locked then
      "basix: pynix_os was generated from nixpkgs ${generated}, but flake.lock holds nixpkgs ${locked}. Use the `pynix` tag that matches the locked nixpkgs"
    else null;

  # `Input(..., modules=["m"])` imports `inputs.NAME.nixosModules.m`, so the typed classes of `pynix_NAME` need no .nix file.
  allInputs = { inherit (inputs) nixpkgs disko; basix = self; } // extraInputs;
  declared = compiled.inputs or [ ];

  inputCheck = lib.forEach declared (i:
    if extraInputs ? ${i.name} then true
    else throw "${i.source}: the input `${i.name}` is not in flake.nix: run `basix apply`");
  # Every `NixpkgsPin(...)` of the entry is the input `nixpkgs-NAME` of the stub (009-D53).
  nixpkgsCheck = lib.forEach (compiled.nixpkgs or [ ]) (n:
    if extraInputs ? "nixpkgs-${n.name}" then true
    else throw "the nixpkgs `${n.name}` is not in flake.nix: run `basix apply`");

  # The pair check of the input packages (009-D48). The compiler records the narHash of each input package that
  # the entry imported (`input_packages`). It must equal the narHash of the input that this flake evaluates.
  # A different generator version is no error: apply makes the package again (010-D5).
  packageCheck = lib.mapAttrsToList (n: want:
    let
      have = (extraInputs.${n} or { }).narHash or null;
      at = (lib.findFirst (i: i.name == n) { source = "system.py"; } declared).source;
    in
    if have == null || have == want then true
    else throw "${at}: the package `pynix_${lib.replaceStrings [ "-" ] [ "_" ] n}` was made from another version of `${n}` than flake.lock holds; run `basix apply` to make it again")
    (compiled.input_packages or { });

  inputModules = lib.concatMap
    (i: map (m: { _file = "${i.source} (Input ${i.name})"; imports = [ allInputs.${i.name}.nixosModules.${m} ]; }) (i.modules or [ ]))
    declared;

  # A native module is a file of the flake (a shipped capability: `capabilities/NAME/module.nix`) or a file of the
  # workspace (`root = "workspace"`, a file of the learner).
  place = n:
    if (n.root or "") == "workspace" then
      (if lib.hasPrefix "/" n.file || lib.elem ".." (lib.splitString "/" n.file) then throw "${n.source}: the native module `${n.file}` must be below the workspace folder"
       else if !builtins.pathExists (workspace + "/${n.file}") then throw "${n.source}: the native module `${n.file}` does not exist (is it in the workspace folder, and added to version control?)"
       else workspace + "/${n.file}")
    else self + "/${n.file}";

  # The hardware report of the install (CONCEPT section 10). A workspace with no report builds a machine with none.
  facterFile = workspace + "/facter.json";
  facter = lib.optionals (builtins.pathExists facterFile) [
    inputs.nixos-facter-modules.nixosModules.facter
    { facter.reportPath = facterFile; }
  ];

  problem = import ./guard.nix { inherit lib; } { data = compiled; inherit source; name = "system.py"; command = "basix apply"; };
in
if pairProblem != null then throw pairProblem else
if problem != null then throw problem else
assert builtins.all (x: x) inputCheck;
assert builtins.all (x: x) nixpkgsCheck;
assert builtins.all (x: x) packageCheck;
{
  ${compiled.name} = inputs.nixpkgs.lib.nixosSystem {
    # `machine` is the compiled entry; a native module reads `machine.values.NAME` (009-D40).
    specialArgs = {
      inputs = allInputs; machine = compiled; self = null;
      # The `basix` command and the compiler of this Basix flake, for `modules/basix-command.nix` (010-D37).
      basixPackages = self.packages.${system};
    };
    modules = [
      { nixpkgs.hostPlatform = system; }
      inputs.disko.nixosModules.disko
    ]
    ++ facter
    ++ [
      (self + "/modules/core.nix")
      (self + "/modules/basix-command.nix")
      (self + "/profiles/${compiled.profile}/module.nix")
    ]
    ++ map decode compiled.fragments
    ++ inputModules
    ++ map (n: { _file = n.source; imports = [ (place n) ]; }) (compiled.modules or [ ])
    ++ extraModules;
  };
}
