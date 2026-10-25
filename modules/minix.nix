# The `minix` launcher (L113, 010-D15): a script in the core that runs the public release of minix, so a learner
# types `minix init` and `minix apply` before the home exists. It holds the one URL and none of the code of minix.
# The Basix flake has no `minix` input (009-D27). After the first `minix apply`, the command of the home takes over.
{ config, pkgs, ... }:
let
  # The URL of the public release. `nix run` fetches it on the first run and keeps it in the cache of Nix.
  minixUrl = "git+https://github.com/Bullish-Intelligence/minix?ref=release";
  launcher = pkgs.writeShellApplication {
    name = "minix";
    runtimeInputs = [ config.nix.package pkgs.git ]; # `nix run` fetches a git URL with git
    text = ''
      exec nix --extra-experimental-features "nix-command flakes" run ${minixUrl} -- "$@"
    '';
  };
in
{
  environment.systemPackages = [ launcher ];
}
