# A fixture stub: one extra input. The input is an attribute set with a `narHash`, a stand-in for a flake input.
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }: {
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
      extraInputs.foo = { narHash = "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="; };
    };
  };
}
