{
  inputs.basix.url = "path:../..";
  outputs = { basix, ... }: {
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
      extraModules = [ (import ./guard-vm.nix) ];
    };
  };
}
