# The profile `desktop` (L66), native part. The Python component `Desktop` (`src/basix_config/desktop.py`) emits every
# option. This file holds what Python cannot name: the default look in `/etc/xdg` and the session wrapper.
#
# Default config files (009-D25, 009-D30). The machine names no minix part:
# - Hyprland looks for `hypr/hyprland.lua`, then `hypr/hyprland.conf`, in `$XDG_CONFIG_HOME`, `~/.config`, each
#   directory of `$XDG_CONFIG_DIRS`, and `/etc/xdg`. The first file wins. A file in the home wins over this one. The
#   file of minix loads this one first, when it exists.
# - Noctalia reads only `~/.config/noctalia/*.toml` (merged in name order) and has no `/etc` path. `Desktop` adds a user
#   rule that links `/etc/xdg/noctalia/00-basix.toml` into `~/.config/noctalia/`. A later file of the learner wins.
#
# The files are `lib.mkDefault`: a native module of the learner may replace one with a normal value (L42).
#
# The session wrapper `basix-session` (L67). The login screen shows the command of the session in its footer. A
# wrapper on `PATH` shows its name, not a store path.
{ config, lib, pkgs, ... }:
let
  session = pkgs.writeShellApplication {
    name = "basix-session";
    # UWSM starts `graphical-session.target`, so user units with that target run. The session is the Exec line of
    # the file `hyprland-uwsm.desktop` of the Hyprland package: UWSM reads `hyprland.desktop`, which runs the
    # watchdog `start-hyprland`.
    text = ''
      exec ${lib.getExe config.programs.uwsm.package} start -e -D Hyprland hyprland.desktop
    '';
  };
in
{
  environment.systemPackages = [ session ];
  environment.etc."xdg/hypr/hyprland.lua".source = lib.mkDefault ./hyprland.lua;
  environment.etc."xdg/noctalia/00-basix.toml".source = lib.mkDefault ./00-basix.toml;
}
