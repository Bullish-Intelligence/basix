# TEST ONLY (lane p3-vm). The NixOS module of the VM check `scripts/apply_vm.py`. No learner has this file.
#
# The check copies it into the test workspace as `vm.nix` and lists it in `Machine(native_modules=[...])`, so the
# machine that `basix apply` builds always holds it. The VM that boots is the first state of that machine, and every
# later state keeps the same VM parts. This is what lets a switch change the running system in place.
#
# What it adds to a Basix machine:
# - `qemu-vm.nix`: the VM runner, with a store image (no host store) and a persistent root disk;
# - a test channel: a second SSH server on port 2222 (the unit `testlink`), so the `sshd.service` of the machine stays
#   under the control of `system.py`. The key is the file `vm.pub` of the workspace;
# - the password of the learner (`basix-test`), for the `sudo` prompt of `basix apply`;
# - the `basix` command: every machine of `lib.machine` has it (`modules/basix-command.nix`, 010-D37);
# - a test cache server on 127.0.0.1:8765 (the unit `testcache`), which serves the folder /var/lib/testcache;
# - a stand-in for the boot loader. `switch-to-configuration boot` calls it with the new system, and it records that
#   call in /var/lib/basix-test, and it fails while the file /var/lib/basix-test/fail-boot exists. The VM is booted by QEMU with a fixed kernel, so a real loader has nothing to do. The
#   check starts every boot after the first with `init=/nix/var/nix/profiles/system/init`: the boot default is the
#   system that the profile names, as the entry of a boot loader would be.
{ config, lib, pkgs, modulesPath, ... }:
{
  imports = [
    (modulesPath + "/virtualisation/qemu-vm.nix")
    (modulesPath + "/profiles/qemu-guest.nix")
  ];

  boot.loader.systemd-boot.enable = lib.mkForce false;
  boot.loader.grub.enable = false;
  system.build.installBootLoader = lib.mkOverride 90 (toString (pkgs.writeShellScript "basix-test-boot-loader" ''
    mkdir -p /var/lib/basix-test
    if [ -e /var/lib/basix-test/fail-boot ]; then
      echo "the test boot loader fails on request" >&2
      exit 1
    fi
    echo "$1" > /var/lib/basix-test/boot-default
    echo "$1" >> /var/lib/basix-test/boot-installs
  ''));
  boot.kernelParams = [ "console=ttyS0" ];
  boot.initrd.availableKernelModules = [ "virtio_blk" "virtio_pci" "virtio_net" ];
  virtualisation = {
    graphics = false;
    memorySize = 6144;
    cores = 4;
    diskSize = 20480;
    writableStoreUseTmpfs = false;
    # The VM has its own store (an image of the first system), so a collection in the VM cannot reach the host.
    mountHostNixStore = false;
    useNixStoreImage = true;
    writableStore = true;
  };
  nix.settings.substituters = lib.mkForce [ ];
  nix.settings.experimental-features = [ "nix-command" "flakes" ];

  # The learner types this password at the `sudo` prompt. Only this file holds it.
  users.users.learner.initialPassword = "basix-test";

  # The test channel.
  users.users.sshd = { isSystemUser = true; group = "sshd"; };
  users.groups.sshd = { };
  environment.etc."testlink/learner.keys".text = builtins.readFile ./vm.pub;
  environment.etc."testlink/root.keys".text = builtins.readFile ./vm.pub;
  networking.firewall.allowedTCPPorts = [ 2222 ];
  security.pam.services.sshd.startSession = true;
  systemd.services.testlink = {
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" ];
    path = [ pkgs.openssh ];
    preStart = "mkdir -p /var/lib/testlink; [ -f /var/lib/testlink/host_key ] || ssh-keygen -q -t ed25519 -N '' -f /var/lib/testlink/host_key";
    serviceConfig.ExecStart = "${pkgs.openssh}/bin/sshd -D -e -p 2222 -o HostKey=/var/lib/testlink/host_key -o PermitRootLogin=prohibit-password -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -f /dev/null -o UsePAM=yes -o StrictModes=no -o PerSourcePenalties=no -o AuthorizedKeysFile=/etc/testlink/%u.keys";
  };

  # The test cache: a folder of a binary cache that the host fills, served over HTTP on the loopback address.
  systemd.tmpfiles.rules = [ "d /var/lib/testcache 0755 root root -" ];
  systemd.services.testcache = {
    wantedBy = [ "multi-user.target" ];
    serviceConfig.ExecStart = "${pkgs.python313}/bin/python3 -m http.server 8765 --bind 127.0.0.1 --directory /var/lib/testcache";
  };

  environment.systemPackages = [ pkgs.curl pkgs.util-linux ];
}
