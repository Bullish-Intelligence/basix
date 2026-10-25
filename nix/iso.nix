# The live Basix ISO on the same lib.machine path as an installed machine.
# `squashfsCompression` is null for the shipped ISO, which keeps the default of nixpkgs. The test ISO (`packages.iso-test`)
# passes a fast setting: the content is the same, and only the compression differs.
# `serialConsole` is null for the shipped ISO. The test ISO passes a device such as "ttyS0". It adds the kernel parameter
# `basix.serial=DEVICE`, and `basix install` then writes that serial console into the installed system (010-D35).
{ pkgs, lib, inputs, system, self, squashfsCompression ? null, serialConsole ? null }:
let
  sourceRoot = ../.;
  # The source that the ISO carries holds only the files that a build of a machine reads (`nix/source.nix`). A script,
  # a test, a note, or a stray file such as `__pycache__` is not in it. Its store path then stays the same when such a
  # file changes, and so do the ISO and the disk cache.
  # Every other use of the source in this file reads `sourceTree`, never `self.outPath`, so the whole flake source
  # does not enter the closure of the ISO.
  sourceTree = (import ./source.nix { inherit lib; root = sourceRoot; }).iso;
  lock = builtins.fromJSON (builtins.readFile (sourceTree + "/flake.lock"));
  sources = lib.mapAttrs'
    (name: node: lib.nameValuePair "basix/inputs/${name}" {
      source = builtins.fetchTree node.locked;
    })
    (lib.filterAttrs (name: node: name != "root" && node ? locked) lock.nodes);
  dropFilter =
    if builtins.pathExists (sourceTree + "/install/drop.jq")
    then sourceTree + "/install/drop.jq"
    else sourceTree + "/iso/drop.jq";
  machine = self.lib.machine {
    compiled = builtins.fromJSON (builtins.readFile (sourceTree + "/iso/system.json"));
    source = sourceTree + "/iso/system.py";
    root = sourceTree + "/iso";
    inherit system;
    extraModules = [
      ({ ... }: {
        imports = [
          (inputs.nixpkgs + "/nixos/modules/installer/cd-dvd/installation-cd-minimal.nix")
          (import (sourceTree + "/modules/live-off.nix") [ "sshd" "sshd-keygen" ])
        ];
        # The `basix` command and the compiler come from `lib.machine` (`modules/basix-command.nix`, 010-D37).
        environment.systemPackages = [
          pkgs.git
          pkgs.nixos-facter
        ];
        environment.etc = {
          "basix/flake.lock".source = sourceTree + "/flake.lock";
          "basix/source".source = sourceTree;
          "basix/drop.jq".source = dropFilter;
        } // sources;
        services.openssh.enable = lib.mkForce false;
        hardware.bluetooth.enable = lib.mkDefault true;
        hardware.bluetooth.powerOnBoot = lib.mkDefault true;
        users.users.root.initialHashedPassword = lib.mkForce null;
        image.baseName = lib.mkForce "basix";
      })
      ({ ... }: lib.mkIf (squashfsCompression != null) {
        isoImage.squashfsCompression = squashfsCompression;
      })
      ({ ... }: lib.mkIf (serialConsole != null) {
        boot.kernelParams = [ "basix.serial=${serialConsole}" ];
      })
    ];
  };
  configuration = machine."basix-iso";
in
configuration.config.system.build.isoImage
