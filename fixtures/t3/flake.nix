# A fixture stub in the form of CONCEPT Appendix C, with the test VM module `vm.nix` (it is part of the T3 anchor).
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }: {
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
      extraModules = [ ./vm.nix ];
    };
  };
}
