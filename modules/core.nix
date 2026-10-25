# The Basix core machine (CONCEPT, "The core machine"). Every Basix machine imports it.
# The core takes its answers as options (`basix.user`, `basix.console`, `basix.disk.device`). The `Machine`
# fragment of the compiled `system.py` sets them (`lib/machine.nix`). The core reads no JSON file.
#
# The core has no SSH key and no password in the store (BSX-131). Root stays locked.
#
# Two kinds of value (SG31, 009-D39):
#   - A value that a machine may change is `lib.mkDefault`. A machine sets its own value at normal priority,
#     for example `System(stateVersion="26.05")`. It needs no `mkForce`.
#   - A value that a Basix rule fixes keeps its value at `lib.mkDefault` too, and a line in `assertions` refuses
#     any other final value. Nix then prints the Basix line, not the advice "use lib.mkForce".
{ config, lib, options, pkgs, ... }:
let
  cfg = config.basix;
  # BSX-298: terminal descriptions for modern terminals. Without one, a pager prints "terminal is not
  # fully functional" over SSH from that terminal. Each entry is a small separate output of nixpkgs
  # that substitutes from cache.nixos.org (2 to 3 KiB download). `environment.enableAllTerminfo` is not
  # usable: it pulls `contour.terminfo`, which has no cache entry and fails to build.
  terminfo = with pkgs; [
    kitty.terminfo # kitty: TERM=xterm-kitty
    foot.terminfo # foot: TERM=foot
    alacritty.terminfo # Alacritty: TERM=alacritty
    wezterm.terminfo # WezTerm: TERM=wezterm
    ghostty.terminfo # Ghostty: TERM=xterm-ghostty
  ];
in
{
  imports = [ ./disk.nix ./minix.nix ];

  options.basix = {
    user = lib.mkOption {
      type = lib.types.nullOr (lib.types.strMatching "[a-z][a-z0-9-]{0,31}");
      default = null;
      description = "The account name the learner chose at install (`user` in `system.py`, BSX-160).";
    };
    console = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "ttyS0" ];
      description = ''
        Kernel console devices, in `console=` order. The default is empty, so the machine uses the
        screen. A test VM sets `ttyS0` to reach the machine over the serial line.
      '';
    };
  };

  config = lib.mkMerge [
    # BSX-297: NetworkManager is the only DHCP client. The facter module (nixos-facter-modules) sets
    # `networking.interfaces.<nic>.useDHCP = true` for each NIC of the report. That starts dhcpcd next to
    # NetworkManager, and both clients then run DHCP on one NIC. `mkDefault` lets the learner turn it on
    # again. The option exists only when the machine reads a hardware report. The seed, the ISO, and the
    # fixtures use `hardware.facter = null`, so the guard skips the line there.
    (lib.optionalAttrs (options ? facter) {
      facter.detected.dhcp.enable = lib.mkDefault false;
    })
    {
    assertions = [
      {
        assertion = cfg.user != null;
        message = "basix: the machine has no account; set basix.user (user in system.py)";
      }
      {
        assertion = config.users.mutableUsers;
        message = "basix: users.mutableUsers must stay true: the install sets the account password in place (BSX-160)";
      }
      {
        assertion = config.users.users.root.hashedPassword == "!";
        message = "basix: the root account stays locked (BSX-131). Log in as the learner account and use sudo";
      }
    ];

    boot.loader.systemd-boot.enable = lib.mkDefault true;
    # BSX-148: bound the growth of /boot.
    boot.loader.systemd-boot.configurationLimit = lib.mkDefault 10;
    # The fallback path \EFI\BOOT\BOOTX64.EFI boots on every UEFI machine. Real hardware is B10.
    boot.loader.efi.canTouchEfiVariables = lib.mkDefault false;
    boot.kernelParams = map (device: "console=${device}") cfg.console;

    hardware.enableRedistributableFirmware = lib.mkDefault true;
    networking.hostName = lib.mkDefault "basix";
    networking.networkmanager.enable = lib.mkDefault true;
    networking.useDHCP = lib.mkDefault false;

    # One account; the password is set at install with `nixos-enter`, never in the store.
    users.mutableUsers = lib.mkDefault true; # fixed by a Basix rule: see the assertion above
    users.users = lib.mkMerge [
      { root.hashedPassword = lib.mkDefault "!"; } # fixed by a Basix rule: see the assertion above
      (lib.mkIf (cfg.user != null) {
        ${cfg.user} = {
          isNormalUser = true;
          extraGroups = [ "wheel" "networkmanager" ];
          # L79, 010-D16: the user manager of the learner runs with no login, so the user services of the
          # home layer (minix) start at boot. `mkDefault`: a machine may turn it off.
          linger = lib.mkDefault true;
        };
      })
    ];
    security.sudo.wheelNeedsPassword = lib.mkDefault true;

    zramSwap.enable = lib.mkDefault true;

    # BSX-325 (Basix R33): Bluetooth works on every machine, so a Bluetooth keyboard works after the install.
    # The setting does not depend on the hardware report. Without this line, only a report that lists a
    # controller turns it on (fixture F4). `lib.mkDefault` is the priority 1000: the machine has Bluetooth on
    # unless a later module or the learner sets a plain value, so a later ruling can turn it off with a plain
    # `false`. The facter module sets `mkDefault true` too; the same value at the same priority does not conflict.
    # A machine with no controller has no failed unit: the unit `bluetooth.service` has the condition
    # `ConditionPathIsDirectory=/sys/class/bluetooth`, so systemd skips it.
    hardware.bluetooth.enable = lib.mkDefault true;
    hardware.bluetooth.powerOnBoot = lib.mkDefault true;

    # `timedatectl` and `localectl` change the zone and keyboard with no rebuild. The priority 1100 is weaker than
    # `lib.mkDefault` (1000): a profile may turn this off with a `Default(False)` (010-D24, `Desktop`) and not conflict.
    i18n.imperativeLocale = lib.mkOverride 1100 true;
    time.timeZone = lib.mkDefault null;

    # BSX-148: a weekly garbage collection that keeps 30 days.
    nix.gc = {
      automatic = lib.mkDefault true;
      dates = lib.mkDefault "weekly";
      options = lib.mkDefault "--delete-older-than 30d";
    };
    nix.settings.experimental-features = [ "nix-command" "flakes" ];

    environment.systemPackages = [ pkgs.git pkgs.networkmanager ] ++ terminfo;

    # The install writes `state_version` into `system.py` (the release of the first install). This default
    # serves only a `system.py` that has no `state_version`. It stays "26.11" in every later release: a new
    # default would move the state version of every machine that has no line of its own.
    system.stateVersion = lib.mkDefault "26.11";
    }
  ];
}
