# TEST ONLY (the Basix host of the minix VM check). `disk=None` writes no disk layout, so the machine lists its own root file system.
# The VM module of the minix check (`nix/vm.nix`) replaces it with the file systems of the VM image.
{
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };
}
