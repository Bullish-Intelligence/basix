# The check of the profile `desktop` (L66, L67, 009-D30). `scripts/capabilities_vm.py` reads it.
#
# The file has the shape of the `check.nix` of the profile `console`: `cases` only. The VM check boots the machine with
# this profile in a headless VM, so the cases look at files, programs, and units. They do not look at a screen.
# `scripts/desktop_vm.py` (`opt:basix-desktop`) boots the same machine with a screen, shows the login screen, and logs in.
#
# greetd gets its settings file with `--config PATH` on the command line of its unit, so the cases read the unit.
{ pkgs, ... }:
let
  greeterConfig = ''cfg="$(systemctl cat greetd.service | grep -o -- '--config [^ ]*' | cut -d' ' -f2)"; test -f "$cfg"'';
in
{
  cases = [
    { name = "Hyprland, foot, and Firefox are on the path of the machine"; run = "command -v Hyprland && command -v foot && command -v firefox"; }
    { name = "the Noctalia shell and the session wrapper are on the path"; run = "command -v noctalia && command -v basix-session"; }
    { name = "the greetd unit is known and enabled"; run = "systemctl cat greetd.service >/dev/null && systemctl is-enabled greetd.service"; }
    { name = "the login screen runs tuigreet and sets no autologin"; run = ''${greeterConfig} && grep -q tuigreet "$cfg" && ! grep -q initial_session "$cfg"''; }
    { name = "the login screen runs the wrapper, and its session shows no store path (L67)"; run = ''${greeterConfig} && grep -q -- "--cmd basix-session" "$cfg" && ! grep -o -- "--cmd .*" "$cfg" | grep -q /nix/store''; }
    { name = "the wrapper starts Hyprland under UWSM"; run = ''grep -q "start -e -D Hyprland hyprland.desktop" "$(command -v basix-session)"''; }
    { name = "the default Hyprland file is in /etc/xdg (009-D30)"; run = "grep -q 'hl.bind' /etc/xdg/hypr/hyprland.lua"; }
    { name = "the default Noctalia file is in /etc/xdg"; run = "grep -q '^thickness = 44' /etc/xdg/noctalia/00-basix.toml"; }
    { name = "the first boot seeds a UTF-8 locale"; run = "grep -q '^LANG=C.UTF-8' /etc/locale.conf"; }
    { name = "the terminfo of foot is installed (BSX-298)"; run = "infocmp foot >/dev/null"; }
  ];
}
