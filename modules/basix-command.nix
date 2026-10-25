# The `basix` command and its compiler on every Basix machine (010-D37). `lib.machine` passes the packages of the
# Basix flake that built the machine, so the installed system runs the same code as the flake that holds it.
# The compiler comes too: `basix apply` builds `packages.compiler` of the workspace lock, and that build needs no
# network only when the compiler is already in the store. The package `basix` does not carry the compiler.
{ basixPackages, ... }:
{
  environment.systemPackages = [ basixPackages.basix basixPackages.compiler ];
}
