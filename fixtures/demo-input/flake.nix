# TEST ONLY (the input of the end-to-end checks of `basix apply`): a tiny NixOS module in a local flake.
# The managed stub of a workspace makes this input follow the `nixpkgs` of Basix: the checks lock no second copy.
{
  description = "A tiny NixOS module for the Basix checks (one Input with a typed value).";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/e7439b6b14ad3cc35d05608ebca9bce01a25f5f8";

  # The module is a function that the flake exports as it is (as many real inputs do): the module system then reports
  # `<unknown-file>` as the declaration, and the generator reads the position of the option instead.
  outputs = { self, ... }: {
    nixosModules.demo = import ./module.nix;
  };
}
