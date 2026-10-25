# A fixture stub in the form of CONCEPT Appendix C. `scripts/check_eval.py` overrides the input `basix`.
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }: {
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
    };
  };
}
