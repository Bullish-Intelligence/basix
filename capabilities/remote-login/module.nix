# The capability `remote-login` (BSX-194), native part: the one rule that reads the final configuration.
#
# The Python component (`component.py`) emits the rest:
#   - the SSH server for the learner account, with no root login and a password login (Basix R13);
#   - port 22 and the mDNS port 5353 on the trusted interfaces only (R13, R18). The trusted interfaces are the
#     field `trusted_interfaces` of `Machine`, which sets `networking.firewall.trustedInterfaces` (L125);
#   - Avahi and UDP port 5353 on the trusted interfaces and on each link where the machine opens SSH with
#     `Port(22, interface=...)` (010-D45). TCP port 22 on such a link stays as the learner wrote it.
#     An empty Avahi interface list means every interface, so Avahi stays off when no such link exists.
#
# Python runs before Nix, so it cannot see the state of the firewall. This module reads it.
{ config, lib, ... }:
{
  warnings = lib.optional (!config.networking.firewall.enable)
    "remote-login: the firewall is off, so port 22 is open on every interface and `trusted_interfaces` has no effect.";
}
