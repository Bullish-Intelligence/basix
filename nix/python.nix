# The uv2nix virtual environment of the Basix project: Basix and the dependencies that `uv.lock` pins. It proves that
# `uv.lock` builds, and it is the package `default` of the flake. The compiler (`nix/compiler.nix`) is the package
# that runs in a workspace; it takes Pydantic from nixpkgs (008-Q7). The package `basix` is the `basix` command: the
# uv2nix application of `uv.lock`, with the `src` folder of the locked `pynix` on its `PYTHONPATH` (010-D8).
#
#   pkgs    a nixpkgs package set
#   lib     the nixpkgs library
#   inputs  the flake inputs: `uv2nix`, `pyproject-nix`, and `pyproject-build-systems`
#   root    the Basix source folder (the flake `self`)
#
# `pynix` is not in `uv.lock`: the compiler and the command read it from the flake input (010-D8).
{ pkgs, lib, inputs, root }:
let
  workspace = inputs.uv2nix.lib.workspace.loadWorkspace { workspaceRoot = root; };
  overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
  # The project build reads these files only: a change to another file does not rebuild the environment.
  source = (import ./source.nix { inherit lib root; }).project;
  pythonSet =
    (pkgs.callPackage inputs.pyproject-nix.build.packages { python = pkgs.python313; }).overrideScope (
      lib.composeManyExtensions [
        inputs.pyproject-build-systems.overlays.wheel
        overlay
        (final: prev: {
          basix = prev.basix.overrideAttrs (old: {
            src = source;
            nativeBuildInputs = (old.nativeBuildInputs or [ ]) ++ final.resolveBuildSystem { hatchling = [ ]; };
          });
        })
      ]
    );
  venv = pythonSet.mkVirtualEnv "basix-env" workspace.deps.default;
  inherit (pkgs.callPackages inputs.pyproject-nix.build.util { }) mkApplication;
  application = mkApplication {
    inherit venv;
    package = pythonSet.basix;
  };
  project = (builtins.fromTOML (builtins.readFile (root + "/pyproject.toml"))).project;
in
{
  default = venv;

  # The `basix` command. The wrapper puts `pynix_engine` and `pynix_native` on the path: `uv.lock` does not list them.
  basix = pkgs.runCommand "basix-${project.version}"
    {
      nativeBuildInputs = [ pkgs.makeWrapper ];
      meta = (application.meta or { }) // {
        description = project.description;
        mainProgram = "basix";
      };
    }
    ''
      mkdir -p "$out/bin"
      makeWrapper ${application}/bin/basix "$out/bin/basix" --prefix PYTHONPATH : ${inputs.pynix}/src
    '';
}
