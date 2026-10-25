# Basix disk layout: one GPT disk, a 512 MiB ESP, and an ext4 root (BSX-134, BSX-161).
# Every disko disk runs the blank-disk guard before disko writes (BSX-096, BSX-140, BSX-141).
# Adopted from the B3 spike (`basix-disk.nix`); only the guard file name and this header differ.
{ config, lib, pkgs, ... }:
let
  cfg = config.basix.disk;
  disks = config.disko.devices.disk;
  names = builtins.attrNames disks;
  guard = pkgs.writeShellScriptBin "basix-disk-guard" ''
    export PATH=${lib.makeBinPath [ pkgs.util-linux pkgs.coreutils pkgs.gnugrep ]}
    ${builtins.readFile ./disk-guard.sh}
  '';
  unknown = builtins.filter (name: name != "main" && !(builtins.elem name cfg.guarded)) names;
  destructive = builtins.filter (name: disks.${name}.destroy) names;
  missing = builtins.filter (name: !(builtins.elem name names)) cfg.guarded;
  hookless = builtins.filter (name:
    !(lib.hasInfix "/bin/basix-disk-guard all" disks.${name}.preCreateHook)) names;
  validatedContentType = assert lib.assertMsg (hookless == [])
    "basix: disk \"${builtins.head hookless}\" has no Basix guard in preCreateHook"; "gpt";
  allArgs = lib.concatMapStringsSep " " (name:
    "${lib.escapeShellArg disks.${name}.device} ${lib.escapeShellArg (if name == "main" && cfg.guard.expectedSize != null then toString cfg.guard.expectedSize else "-")}") names;
  allHook = assert lib.assertMsg (unknown == []) "basix: disk \"${builtins.head unknown}\" has no Basix guard; add its name to basix.disk.guarded";
    assert lib.assertMsg (destructive == []) "basix: disk \"${builtins.head destructive}\" sets destroy = true; Basix requires false";
    assert lib.assertMsg (missing == []) "basix: guarded disk \"${builtins.head missing}\" is not defined";
    ''${guard}/bin/basix-disk-guard all ${allArgs}'';
in {
  options.basix.disk = {
    device = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      description = ''
        Target disk, a /dev/disk/by-id path. SG31 (proposed): `null` is the migration mode. The machine then keeps its
        own `fileSystems`, and this module adds no disko disk, no layout, and no guard. Basix writes no disk for it.
      '';
    };
    guarded = lib.mkOption { type = lib.types.listOf lib.types.str; default = []; description = "Names of additional disko disks for which Basix installs the guard hook."; };
    guard.expectedSize = lib.mkOption { type = lib.types.nullOr lib.types.ints.positive; default = null; description = "Exact main disk size in bytes, when known."; };
    guardPackage = lib.mkOption { type = lib.types.package; default = guard; readOnly = true; };
  };
  config = lib.mkMerge [
  # SG31 (proposed): the migration mode. The machine keeps its own file systems; Basix adds no disk and no guard.
  # The machine must then say where its root is. The text names the one thing to do.
  (lib.mkIf (cfg.device == null) {
    assertions = [{
      assertion = config.fileSystems ? "/";
      message = "basix: the machine has no disk setting, so it keeps its own file systems, but it defines no root file system \"/\". Add the root file system of this machine, or set a disk";
    }];
  })
  (lib.mkIf (cfg.device != null) {
    # The all-disk check is repeated before each create; therefore it runs before
    # the first disk, regardless of disko's iteration order.
    disko.devices.disk = (lib.genAttrs ([ "main" ] ++ cfg.guarded) (_: {
      destroy = lib.mkDefault false;
      preCreateHook = lib.mkBefore allHook;
    })) // {
      main = {
        device = cfg.device;
        type = "disk";
        destroy = lib.mkDefault false;
        preCreateHook = lib.mkBefore allHook;
        content = {
          type = validatedContentType;
          partitions = {
            ESP = {
              size = "512M";
              type = "EF00";
              content = { type = "filesystem"; format = "vfat"; mountpoint = "/boot"; mountOptions = [ "umask=0077" ]; };
            };
            root = {
              size = "100%";
              content = { type = "filesystem"; format = "ext4"; mountpoint = "/"; };
            };
          };
        };
      };
    };
  })
  ];
}
