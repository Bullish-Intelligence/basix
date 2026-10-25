# The file systems of a migrated machine: a Btrfs root and a VFAT boot partition.
{ ... }: {
  fileSystems."/" = { device = "/dev/disk/by-uuid/00000000-0000-0000-0000-000000000001"; fsType = "btrfs"; options = [ "subvol=@" ]; };
  fileSystems."/boot" = { device = "/dev/disk/by-uuid/0000-0001"; fsType = "vfat"; };
}
