# TEST ONLY. The parts of the test VM of `scripts/desktop_vm.py` (`opt:basix-desktop`). No learner has this file.
# It is `vmcheck.nix` with a screen: a virtio GPU, 4 GiB of memory, and a known password for the account `learner`, so
# that the check can type it on the login screen. The serial line keeps the automatic root login for the commands of
# the check. The machine name is `vmcheck`.
{ lib, modulesPath, ... }:
{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];
  services.getty.autologinUser = lib.mkForce "root";
  boot.kernelParams = [ "console=ttyS0" ];
  boot.initrd.availableKernelModules = [ "virtio_blk" "virtio_pci" "virtio_net" "virtio_gpu" ];
  users.users.learner.initialPassword = "basix-try";
  virtualisation.vmVariant.virtualisation = {
    graphics = true;
    memorySize = 4096;
    cores = 2;
  };
}
