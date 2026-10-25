# The check of the capability `remote-login` (BSX-194, BSX-270). `scripts/capabilities_vm.py` reads it.
#
# The file is a function of `pkgs`. It returns plain data:
#   nixos               extra NixOS configuration for the check VM (a module).
#   trustedInterfaces   the interface names that the VM check writes into `system.py` as the field
#                       `trusted_interfaces` of `Machine` (Basix R13).
#   cases               shell commands that run as root in the VM, in order. A case passes when its command exits 0.
#   gone                shell commands that must exit 0 in a VM of the same machine without the capability.
#
# The VM check runs the machine twice: with the capability (`cases`) and without it (`gone`). The second run
# stands in for removing the name and running `basix apply`. It is not an undo proof.
#
# The cases for mDNS (Basix R18) ask the name `<host name>.local` from the peer and compare the answer.
#
# The VM has one network that QEMU supplies, and no peer. The cases make a peer: a network namespace `peer`
# with two virtual links to the VM. The link `veth-t` is trusted. The link `veth-u` is not. A connection from
# the peer arrives on one of the two links, so the firewall rule of one interface decides the result.
# (A connection from the VM to its own address arrives on the loopback link, which the firewall always opens.)
{ pkgs, ... }:
let
  trusted = "veth-t";
  untrusted = "veth-u";
  trustedAddress = "10.9.1.1";
  untrustedAddress = "10.9.2.1";

  # Make the peer and the two links. Each link has an address on both ends.
  makePeer = ''
    set -e
    ip netns add peer
    ip link add ${trusted} type veth peer name peer-t
    ip link add ${untrusted} type veth peer name peer-u
    ip link set peer-t netns peer
    ip link set peer-u netns peer
    nmcli device set ${trusted} managed no 2>/dev/null || true
    nmcli device set ${untrusted} managed no 2>/dev/null || true
    ip addr add ${trustedAddress}/24 dev ${trusted}
    ip addr add ${untrustedAddress}/24 dev ${untrusted}
    ip link set ${trusted} up
    ip link set ${untrusted} up
    ip netns exec peer ip addr add 10.9.1.2/24 dev peer-t
    ip netns exec peer ip addr add 10.9.2.2/24 dev peer-u
    ip netns exec peer ip link set lo up
    ip netns exec peer ip link set peer-t up
    ip netns exec peer ip link set peer-u up
    ip netns exec peer ping -c 1 -W 5 ${trustedAddress}
    ip netns exec peer ping -c 1 -W 5 ${untrustedAddress}
  '';

  # A TCP connection to port 22 from the peer. It exits 0 when the connection opens.
  connect = address: "ip netns exec peer timeout 8 bash -c 'exec 3<>/dev/tcp/${address}/22'";

  # An SSH login from the peer with a password.
  login = user: address: ''SSHPASS=basix-check-password ip netns exec peer sshpass -e ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o PreferredAuthentications=password ${user}@${address} true'';

  # An mDNS query for `<host name>.local` from the peer. It goes by unicast to port 5353 of one address, with a
  # source port other than 5353. Avahi treats it as a legacy unicast query and answers it with the addresses
  # of the interface where the query arrived. A multicast query over a veth pair needs a multicast route in the
  # peer, and it would not tell which link carried the answer. The unicast query tests the same two rules:
  # Avahi must listen on that interface, and the firewall must pass UDP 5353 on it. The output is the answer
  # (empty when there is none).
  ask = address: "ip netns exec peer dig +short +time=3 +tries=1 -p 5353 @${address} \"$(hostname).local\" A 2>/dev/null | grep -E '^[0-9]+(\\.[0-9]+){3}$' || true";
in
{
  nixos = {
    environment.systemPackages = [ pkgs.sshpass pkgs.openssh pkgs.iproute2 pkgs.iputils pkgs.dig ];
  };

  trustedInterfaces = [ trusted ];

  cases = [
    { name = "the ssh service is active"; run = "systemctl is-active sshd.service"; }
    { name = "the server listens on TCP port 22"; run = "ss -ltn | grep -q ':22 '"; }
    { name = "root login is refused"; run = "sshd -T | grep -qi '^permitrootlogin no$'"; }
    { name = "password login is on"; run = "sshd -T | grep -qi '^passwordauthentication yes$'"; }
    { name = "only the learner account may log in"; run = "sshd -T | grep -i '^allowusers ' | grep -qi \"$BASIX_USER\""; }
    { name = "the host key exists and is not in the store"; run = "test -s /etc/ssh/ssh_host_ed25519_key && ! readlink -f /etc/ssh/ssh_host_ed25519_key | grep -q '^/nix/store/'"; }
    { name = "the peer reaches the VM on both links"; run = makePeer; }
    { name = "port 22 accepts a connection on the trusted link"; run = connect trustedAddress; }
    { name = "port 22 refuses a connection on the untrusted link"; run = "! ${connect untrustedAddress}"; }
    { name = "the mDNS service is active"; run = "systemctl is-active avahi-daemon.service"; }
    { name = "the mDNS service uses the trusted interface only"; run = "grep -qx 'allow-interfaces=${trusted}' \"$(systemctl cat avahi-daemon.service | grep -o '/nix/store/[^ ]*avahi-daemon.conf' | head -n 1)\""; }
    {
      name = "the peer finds the host name through mDNS on the trusted link";
      run = ''test "$(${ask trustedAddress})" = ${trustedAddress}'';
    }
    {
      name = "the peer gets no mDNS answer on the untrusted link";
      run = ''test -z "$(${ask untrustedAddress})"'';
    }
    {
      name = "the learner account logs in over the trusted link";
      run = ''echo "$BASIX_USER:basix-check-password" | chpasswd && ${login "$BASIX_USER" trustedAddress}'';
    }
    {
      name = "root cannot log in over the trusted link";
      run = ''echo "root:basix-check-password" | chpasswd && ! ${login "root" trustedAddress}'';
    }
    {
      name = "the learner account cannot log in over the untrusted link";
      run = "! ${login "$BASIX_USER" untrustedAddress}";
    }
  ];

  gone = [
    { name = "the ssh service does not exist"; run = "! systemctl is-active sshd.service"; }
    { name = "nothing listens on TCP port 22"; run = "! ss -ltn | grep -q ':22 '"; }
    { name = "the peer reaches the VM on both links"; run = makePeer; }
    { name = "port 22 refuses a connection on the trusted link"; run = "! ${connect trustedAddress}"; }
    { name = "the mDNS service does not exist"; run = "! systemctl is-active avahi-daemon.service"; }
    { name = "nothing listens on UDP port 5353"; run = "! ss -lun | grep -q ':5353 '"; }
    { name = "the peer gets no mDNS answer on the trusted link"; run = ''test -z "$(${ask trustedAddress})"''; }
  ];
}
