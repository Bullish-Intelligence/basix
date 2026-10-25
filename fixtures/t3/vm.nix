# TEST ONLY (S4 of project 008; S12 change: the VM store is an image, see `virtualisation` below). The VM runs the machine of the workspace. The learner has no such file.
# It imports `qemu-vm.nix` into the machine itself (as in 007 S4), so `toplevel` is the system that the VM boots.
# The test channel is a second SSH server, the unit `testlink` on port 2222, so that `sshd.service` stays under the
# control of `RemoteLogin()`. The VM has no substituter: every build uses the store as it is.
{ config, lib, pkgs, modulesPath, ... }:
{
  imports = [
    (modulesPath + "/virtualisation/qemu-vm.nix")
    (modulesPath + "/profiles/qemu-guest.nix")
  ];
  boot.loader.systemd-boot.enable = lib.mkForce false;
  boot.loader.grub.enable = false;
  system.build.installBootLoader = lib.mkOverride 90 "${pkgs.coreutils}/bin/true";
  boot.kernelParams = [ "console=ttyS0" ];
  boot.initrd.availableKernelModules = [ "virtio_blk" "virtio_pci" "virtio_net" ];
  virtualisation = {
    graphics = false;
    memorySize = 6144;
    cores = 4;
    diskSize = 12288;
    writableStoreUseTmpfs = false;
    # S12: the VM gets its own store (an image of the system closure) instead of the host store. With the host store
    # (S4), the VM saw every host path as an unregistered path, and `nix-collect-garbage -d` tried to delete about
    # 21,000 of them; virtiofsd then failed with "Too many open files in system". With the image, a collection in
    # the VM deals with the paths of the VM only, and the host store is not mounted at all.
    mountHostNixStore = false;
    useNixStoreImage = true;
    writableStore = true;
  };
  users.users.learner.initialPassword = "basix-try";
  security.sudo.wheelNeedsPassword = lib.mkOverride 90 false;
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  nix.settings.substituters = lib.mkForce [ ];

  # The test channel.
  users.users.sshd = { isSystemUser = true; group = "sshd"; };
  users.groups.sshd = { };
  environment.etc."testlink/learner.keys".text = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGJ/Lg5EFlUJN6ems0FaIIrtr40l5sGLnWYjSNwRA55G s4\n";
  environment.etc."testlink/root.keys".text = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGJ/Lg5EFlUJN6ems0FaIIrtr40l5sGLnWYjSNwRA55G s4\n";
  networking.firewall.allowedTCPPorts = [ 2222 ];
  security.pam.services.sshd = { };
  systemd.services.testlink = {
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    path = [ pkgs.openssh ];
    preStart = "mkdir -p /var/lib/testlink; [ -f /var/lib/testlink/host_key ] || ssh-keygen -q -t ed25519 -N '' -f /var/lib/testlink/host_key";
    serviceConfig.ExecStart = "${pkgs.openssh}/bin/sshd -D -e -p 2222 -o HostKey=/var/lib/testlink/host_key -o PermitRootLogin=prohibit-password -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -f /dev/null -o UsePAM=yes -o StrictModes=no -o PerSourcePenalties=no -o AuthorizedKeysFile=/etc/testlink/%u.keys";
  };

  # The Python of the `basix` command (Typer), and the tools of the measurements.
  environment.systemPackages = [ (pkgs.python313.withPackages (p: [ p.typer ])) pkgs.time ];
}
