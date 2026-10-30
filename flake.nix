{
  description = "Basix: a distribution for people who learn computing. The library `lib.machine` builds a NixOS machine from the compiled `system.py`.";

  # The pin layout. Pins live here and in `flake.lock` only. A learner workspace names `basix` as its one input.
  # `scripts/check_pins.py` compares this file with `flake.lock`, and checks that one `nixpkgs` serves every input.
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/e7439b6b14ad3cc35d05608ebca9bce01a25f5f8";

    flake-parts.url = "github:hercules-ci/flake-parts/024633cd702b10285db5cb19b40ad48d2399ba60";
    flake-parts.inputs.nixpkgs-lib.follows = "nixpkgs";

    disko.url = "github:nix-community/disko/de5708739256238fb912c62f03988815db89ec9a";
    disko.inputs.nixpkgs.follows = "nixpkgs";

    nixos-facter-modules.url = "github:nix-community/nixos-facter-modules/139dcef4dfc97009629c445806f197883351ab4a";

    # The typed Python libraries and the decoder (010-D8). The input is a source folder, not a flake:
    # the Nix code reads `"${inputs.pynix}/src"`. The tag is the record of the release.
    pynix.url = "git+https://github.com/Bullish-Intelligence/pynix?ref=refs/tags/pynix-v0.7.0";
    pynix.flake = false;

    # The Python packages of the compiler and the `basix` command are built from `uv.lock` with uv2nix.
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix/ddc400a6883b042646d0940e65971bb7f7e96107";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";

    uv2nix.url = "github:pyproject-nix/uv2nix/7aba73a3d85454f1118f8cf9de97979b4d26bcac";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    uv2nix.inputs.pyproject-nix.follows = "pyproject-nix";

    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs/a17819c85163988e063806ffc4f6d2716bc8ead8";
    pyproject-build-systems.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.inputs.uv2nix.follows = "uv2nix";
  };

  outputs = inputs@{ self, nixpkgs, flake-parts, ... }:
    flake-parts.lib.mkFlake { inherit inputs; } {
      systems = [ "x86_64-linux" ];

      flake = {
        # The compiled `system.json` becomes `nixosConfigurations` (CONCEPT Appendix C).
        lib.machine = import ./lib/machine.nix { inherit self inputs; };
        # The typed packages of the extra inputs: the stub names this as `packages.<system>.input-packages`.
        lib.inputPackages = import ./lib/input-packages.nix { inherit self inputs; };
        # The source trees that the outputs copy (`nix/source.nix`): only listed files, never a stray `__pycache__`.
        lib.sourceTrees = import ./nix/source.nix { lib = nixpkgs.lib; root = ./.; };
      };

      perSystem = { system, ... }:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          lib = nixpkgs.lib;
          python = import ./nix/python.nix { inherit pkgs lib inputs; root = ./.; };
        in
        {
          # Lane `p2-py` writes both files. The attributes are lazy: `lib.machine` does not read them.
          packages.compiler = import ./nix/compiler.nix { inherit pkgs; pynix = inputs.pynix; root = ./.; };
          # The editor launcher: `nix run --inputs-from . basix#editor` starts `ty` on `system.py` (BSX-371).
          packages.editor = import ./nix/editor.nix { inherit pkgs; pynix = inputs.pynix; root = ./.; };
          packages.default = python.default;
          packages.basix = python.basix;
          packages.iso = import ./nix/iso.nix { inherit pkgs lib inputs system self; };
          # TEST ONLY. The shipped ISO with a fast squashfs compression, for `opt:basix-iso` and `opt:basix-install`.
          packages.iso-test = import ./nix/iso.nix {
            inherit pkgs lib inputs system self;
            squashfsCompression = "zstd -Xcompression-level 3";
            serialConsole = "ttyS0";
          };
        };
    };
}
