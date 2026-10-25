# TEST ONLY. Boot the Basix disk guard against qcow2 fixture disks.
{ config, lib, pkgs, modulesPath, ... }:
{
  imports = [
    (modulesPath + "/virtualisation/qemu-vm.nix")
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

  boot.kernelParams = [ "console=ttyS0" ];
  boot.initrd.availableKernelModules = [ "virtio_blk" "virtio_pci" ];
  boot.loader.systemd-boot.enable = lib.mkForce false;
  boot.loader.grub.enable = false;
  virtualisation = {
    graphics = false;
    memorySize = 2048;
    cores = 2;
    diskSize = 10240;
    writableStoreUseTmpfs = false;
    mountHostNixStore = false;
    useNixStoreImage = true;
    writableStore = true;
  };
  services.getty.autologinUser = "root";
  environment.systemPackages = [
    config.basix.disk.guardPackage
    (pkgs.writeShellScriptBin "basix-p4-disko-script" ''exec ${config.system.build.diskoScript} "$@"'')
    pkgs.util-linux
    pkgs.gnugrep
    pkgs.gptfdisk
  ];
  basix.disk.guarded = [ "extra" ];
  disko.devices.disk.extra = {
    type = "disk";
    device = "/dev/disk/by-id/virtio-vd-extra";
    destroy = lib.mkDefault false;
    content = { type = "gpt"; partitions = { }; };
  };
}
