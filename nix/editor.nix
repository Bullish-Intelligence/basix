# The editor launcher: `bin/basix-ty` starts the language server `ty` on `system.py` (BSX-371, 008 N4, 009 section 8).
#
#   pkgs   a nixpkgs package set
#   pynix  the `pynix` flake input (`flake = false`): its source folder, with the `.pyi` stubs
#   root   the Basix source folder (the flake `self`)
#
# The launcher belongs to the Basix input of the workspace lock, so it follows an update with no edit:
#
#   nix run --inputs-from . basix#editor
#
# `ty` reads `PYTHONPATH`. The launcher sets it to the Basix library, the `pynix` libraries with their stubs, Pydantic,
# and the typed packages of the inputs of the workspace (`.basix/pynix`). The compiler package holds no stub files, so
# it cannot serve the editor.
{ pkgs, pynix, root }:
let
  inherit (pkgs) lib;
  python = pkgs.python313.withPackages (p: [ p.pydantic ]);
  # The same source tree as the compiler: the capability folders must stay beside `src` for their links.
  basix = (import ./source.nix { inherit lib root; }).library;
in
pkgs.runCommand "basix-editor"
  {
    meta.mainProgram = "basix-ty";
  }
  ''
    mkdir -p $out/bin
    cat > $out/bin/basix-ty <<SH
    #!${pkgs.runtimeShell}
    # Run this in the workspace folder, or name it in BASIX_WORKSPACE.
    workspace="\''${BASIX_WORKSPACE:-\$PWD}"
    export PYTHONPATH="${basix}/src:${pynix}/src:${python}/${python.sitePackages}:\$workspace/.basix/pynix"
    exec ${pkgs.ty}/bin/ty server "\$@"
    SH
    chmod +x $out/bin/basix-ty
  ''
