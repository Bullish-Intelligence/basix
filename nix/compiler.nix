# The compiler: `bin/basix-compile system.py system.json`. It holds its own Python (with Pydantic from nixpkgs,
# 008-Q7) and the libraries that it imports: `nixdantic`, `pynix_os`, `pynix_pkgs`, and `pynix_native` from the
# pinned `pynix` source, and `basix_config` from `root`. It needs nothing from `PATH`.
#
#   pkgs   a nixpkgs package set
#   pynix  the `pynix` flake input (`flake = false`): its source folder; the libraries are in `"${pynix}/src"`
#   root   the Basix source folder (the flake `self`)
#
# The package copies the libraries and compiles them to bytecode. The store is read-only, so Python could not keep
# bytecode there at run time, and every run would parse the large `pynix_os` modules again.
{ pkgs, pynix, root }:
let
  inherit (pkgs) lib;
  python = pkgs.python313.withPackages (p: [ p.pydantic ]);
  # The Basix library, and the `component.py` of each capability: `src/basix_config/capabilities/NAME.py` is a
  # symlink to `capabilities/NAME/component.py` (010-D10), so both must stay in one source tree.
  basix = (import ./source.nix { inherit lib root; }).library;
in
pkgs.runCommand "basix-compiler" { } ''
  mkdir -p $out/bin $out/lib
  for name in nixdantic pynix_os pynix_pkgs pynix_native; do
    cp -r ${pynix}/src/$name $out/lib/
  done
  # `cp -L` resolves the capability symlinks into real files.
  cp -rL ${basix}/src/basix_config $out/lib/
  chmod -R u+w $out/lib
  # The compiler imports no stub file; `pkgs.pyi` and the set stubs are 11 MB.
  find $out/lib -name '*.pyi' -delete
  ${python}/bin/python -m compileall -q -j 0 $out/lib
  cat > $out/bin/basix-compile <<SH
  #!${pkgs.runtimeShell}
  export PYTHONPATH=$out/lib
  exec ${python}/bin/python -P -m basix_config.compile "\$@"
  SH
  chmod +x $out/bin/basix-compile
''
