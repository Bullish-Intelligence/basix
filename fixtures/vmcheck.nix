# TEST ONLY. The parts of a test VM for `scripts/capabilities_vm.py`: a serial console, an automatic root login, and
# the virtio drivers. No learner has this file. The machine name is `vmcheck`; the check types commands at the
# root prompt `[root@vmcheck:...]#`.
{ lib, modulesPath, ... }:
{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
  services.getty.autologinUser = lib.mkForce "root";
  boot.kernelParams = [ "console=ttyS0" ];
  boot.initrd.availableKernelModules = [ "virtio_blk" "virtio_pci" "virtio_net" ];
  virtualisation.vmVariant.virtualisation = {
    graphics = false;
    memorySize = 2048;
    cores = 2;
  };
}
