# The facts that `scripts/check_eval.py` reads from an evaluated machine (BSX-131, BSX-134, BSX-148, BSX-297,
# BSX-298, BSX-325, SG31, L125, L66, 010-D45). The check passes this file to `nix eval --apply`, so it is one function of
# `config` and uses `builtins` only. It returns plain data.
c:
let
  b = builtins;
  passwordOptions = [ "password" "hashedPassword" "hashedPasswordFile" "initialPassword" "initialHashedPassword" ];
  names = b.attrNames c.users.users;
  user = c.basix.user;
  hasKeys = u: c.users.users.${u}.openssh.authorizedKeys.keys != [ ] || c.users.users.${u}.openssh.authorizedKeys.keyFiles != [ ];
  startsWith = prefix: s: b.substring 0 (b.stringLength prefix) s == prefix;
  # A terminfo output is either its own package (wezterm) or the output `terminfo` of the terminal.
  isTerminfo = p: (p.outputName or "") == "terminfo" || b.match ".*terminfo" p.name != null;
  enabledUnits = b.filter (n: c.systemd.units.${n}.enable) (b.attrNames c.systemd.units);
in
{
  rootKeys = c.users.users.root.openssh.authorizedKeys.keys ++ c.users.users.root.openssh.authorizedKeys.keyFiles;
  rootHashedPassword = c.users.users.root.hashedPassword;
  # Users other than root with a password option set. The list must be empty.
  passwordUsers = b.filter (u: u != "root" && b.any (o: c.users.users.${u}.${o} != null) passwordOptions) names;
  keyUsers = b.filter hasKeys names;
  learnerGroups = if user == null then [ ] else c.users.users.${user}.extraGroups;
  # 010-D16: the learner has a user manager that runs with no login.
  linger = if user == null then null else c.users.users.${user}.linger;
  mutableUsers = c.users.mutableUsers;
  sshd = c.services.openssh.enable;
  timeZone = c.time.timeZone;
  nameservers = c.networking.nameservers;
  imperativeLocale = c.i18n.imperativeLocale;
  zram = c.zramSwap.enable;
  # BSX-325: Bluetooth is on for a machine with no hardware report and for a report with no controller.
  bluetooth = { inherit (c.hardware.bluetooth) enable powerOnBoot; };
  swapDevices = c.swapDevices;
  networkManager = c.networking.networkmanager.enable;
  firmware = c.hardware.enableRedistributableFirmware;
  systemdBoot = c.boot.loader.systemd-boot.enable;
  bootLimit = c.boot.loader.systemd-boot.configurationLimit;
  gc = { inherit (c.nix.gc) automatic dates options; };
  kernelConsole = b.filter (startsWith "console=") c.boot.kernelParams;
  dhcpInterfaces = b.filter (n: c.networking.interfaces.${n}.useDHCP == true) (b.attrNames c.networking.interfaces);
  dhcpcdService = c.systemd.services ? dhcpcd;
  globalDhcp = c.networking.useDHCP;
  terminfo = map (p: p.pname or p.name) (b.filter isTerminfo c.environment.systemPackages);
  packages = map (p: p.pname or p.name) c.environment.systemPackages;
  # L113: the script `minix` of the core. `text` is the script, read with no build.
  minixLauncher = map (p: p.text) (b.filter (p: (p.pname or p.name) == "minix") c.environment.systemPackages);
  stateVersion = c.system.stateVersion;
  hostName = c.networking.hostName;
  wheelNeedsPassword = c.security.sudo.wheelNeedsPassword;
  # L125: the trusted links come from `networking.firewall.trustedInterfaces`; `remote-login` opens port 22 on those links only.
  # NixOS adds the loopback link `lo` to the option, so the fact leaves it out.
  trustedInterfaces = b.filter (i: i != "lo") c.networking.firewall.trustedInterfaces;
  openTcpByInterface = b.mapAttrs (_: i: i.allowedTCPPorts) c.networking.firewall.interfaces;
  openTcpGlobal = c.networking.firewall.allowedTCPPorts;
  openUdpByInterface = b.mapAttrs (_: i: i.allowedUDPPorts) c.networking.firewall.interfaces;
  # 010-D45: `remote-login` announces the machine (Avahi) on the trusted links and on the links of `Port(22, interface=...)`.
  avahi = {
    inherit (c.services.avahi) enable allowInterfaces nssmdns4;
    publishAddresses = c.services.avahi.publish.addresses;
  };
  # The disko disks that Basix wrote (none for `disk = None`) and the file systems of the machine.
  diskNames = b.attrNames c.disko.devices.disk;
  rootFsType = c.fileSystems."/".fsType or null;
  bootFsType = c.fileSystems."/boot".fsType or null;
  # L66, L67, 009-D30: the desktop profile. A machine with another profile shows `false` and `null`.
  hyprland = c.programs.hyprland.enable;
  noctalia = c.programs.noctalia.enable;
  greetd = c.services.greetd.enable;
  # `settings` has no value while greetd is off, so both facts read it only when greetd is on.
  greeterCommand = if c.services.greetd.enable then c.services.greetd.settings.default_session.command or null else null;
  greeterAutologin = c.services.greetd.enable && c.services.greetd.settings ? initial_session;
  defaultLocale = c.i18n.defaultLocale;
  consoleKeyMap = c.console.keyMap;
  userTmpfiles = c.systemd.user.tmpfiles.rules;
  # The head of each default file of `/etc/xdg`, or null when the machine has none.
  etcHyprland = if c.environment.etc ? "xdg/hypr/hyprland.lua" then b.substring 0 60 (b.readFile c.environment.etc."xdg/hypr/hyprland.lua".source) else null;
  etcNoctalia = if c.environment.etc ? "xdg/noctalia/00-basix.toml" then b.substring 0 40 (b.readFile c.environment.etc."xdg/noctalia/00-basix.toml".source) else null;
  # `liveOff`: the units that the machine enables under the name `basix-live-test.` (BSX-097).
  liveUnits = b.filter (startsWith "basix-live-test.") enabledUnits;
}
