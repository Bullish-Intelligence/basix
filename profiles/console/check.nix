# The check of the profile `console` (BSX-192). `scripts/capabilities_vm.py` reads it.
#
# The file has the shape of the `check.nix` of a capability (see `capabilities/remote-login/check.nix`),
# with `cases` only. The VM check boots the machine with this profile and no capability. The cases state
# what the core gives a learner: a text console, NetworkManager, zram, one account, the tools, and no
# SSH server. Slice B5 proved the same facts on the installed core (B2, `B5-CORE-ISO.md`).
{ pkgs, ... }:
{
  cases = [
    { name = "the machine runs no display manager"; run = "! systemctl is-active display-manager.service"; }
    { name = "the machine has no failed unit"; run = ''test "$(systemctl --failed --no-legend | wc -l)" = 0''; }
    { name = "the host name is the machine name"; run = ''test "$(hostname)" = vmcheck''; }
    { name = "NetworkManager runs"; run = "systemctl is-active NetworkManager.service && nmcli general status"; }
    { name = "zram swap is on"; run = "swapon --show=NAME --noheadings | grep -q zram"; }
    { name = "the learner account is in the groups wheel and networkmanager"; run = ''id -nG "$BASIX_USER" | tr ' ' '\n' | grep -qx wheel && id -nG "$BASIX_USER" | tr ' ' '\n' | grep -qx networkmanager''; }
    { name = "the root account has no password"; run = "passwd -S root | grep -q '^root L '"; }
    { name = "the tools of the core are on the path"; run = "command -v git && command -v nmcli"; }
    { name = "NetworkManager is the only DHCP client: no dhcpcd unit (BSX-297)"; run = ''! systemctl cat dhcpcd.service && ! systemctl is-active dhcpcd.service''; }
    { name = "the terminfo of kitty and foot is installed (BSX-298)"; run = "infocmp xterm-kitty >/dev/null && infocmp foot >/dev/null"; }
    { name = "the Bluetooth unit is known, enabled, and not failed, whether or not the machine has a controller (BSX-325)"; run = ''systemctl cat bluetooth.service >/dev/null && systemctl is-enabled bluetooth.service && ! systemctl is-failed bluetooth.service''; }
    { name = "the controller powers on at boot (BSX-325)"; run = "grep -q '^AutoEnable=true' /etc/bluetooth/main.conf"; }
    { name = "bluetoothctl is on the path (BSX-325)"; run = "command -v bluetoothctl"; }
    { name = "the machine runs no SSH server"; run = "! systemctl is-active sshd.service"; }
    { name = "nothing listens on TCP port 22"; run = "! ss -ltn | grep -q ':22 '"; }
  ];
}
