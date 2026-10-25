# A fixture stub in the form of CONCEPT Appendix C. The minix VM check (`MINIX_VM_HOST=basix`) replaces the input
# `basix` with a folder of this repository (`--override-input`, or the line `inputs.basix.url`).
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
