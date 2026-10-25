# liveOff: turn units off on a live ISO (R29, BSX-097; W8 Q3).
#
# Add it to the module list of the live system (the ISO machine, or `extendModules` of a machine; see
# `scripts/check_eval.py`):
#
#   modules = [ (import ./modules/live-off.nix [ "harmonia" ]) ];
#
# Each name turns off the service, the socket, and the timer of that name. `lib.mkForce` is
# required: it wins over every other definition. A unit that another unit requires needs its
# dependents on the list too (W8, untested).
names: { lib, ... }:
let
  off = lib.genAttrs names (_: { enable = lib.mkForce false; });
in
{
  systemd.services = off;
  systemd.sockets = off;
  systemd.timers = off;
}
