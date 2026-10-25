# TEST ONLY (BSX-411): a small NixOS system with one fixture module. Use: nix build --impure --expr
# 'import ./system.nix { nixpkgs = builtins.getFlake "..."; module = ./cycle.nix; }'
{ nixpkgs, module }:
(nixpkgs.lib.nixosSystem {
  system = "x86_64-linux";
  modules = [
    module
    (
      { ... }:
      {
        fileSystems."/" = {
          device = "/dev/vda";
          fsType = "ext4";
        };
        boot.loader.grub.enable = false;
        system.stateVersion = "26.11";
      }
    )
  ];
}).config.system.build.toplevel
