# remote-login: log in to this machine from another computer

## What it does

It starts an SSH server (secure shell). You log in from another computer on the same network with your
account name and password. The `root` account cannot log in.

Port 22 is open on trusted interfaces only. Until you name one, nobody can log in. The machine also announces its
name (mDNS) on those interfaces, so you reach it as `NAME.local`. `NAME` is the machine name in `system.py`.
If you open SSH on one link yourself, with `Port(22, interface="eno1")` in `user_ports`, the machine also announces
its name on that link.

## Turn it on

1. In `system.py`, add `RemoteLogin()` to `capabilities`.
2. In `system.py`, add your network interface to `trusted_interfaces`, for example `["enp1s0"]`. Run `ip link` to
   see the names. Wired names often start with `en`, Wi-Fi names with `wl`.
3. Run `basix apply`, then `ssh ACCOUNT@NAME.local` from the other computer.

## Turn it off

Remove `RemoteLogin()` from `capabilities` and run `basix apply`. Or run `basix undo`.

## Settings

- `password_login=False`: only a key logs in. List your public keys in `authorized_keys`, one line each:
  `RemoteLogin(password_login=False, authorized_keys=["ssh-ed25519 AAAA... you@host"])`. With no key, Basix
  warns you: nobody can log in over the network until you add a key.

**Safety.** A trusted interface admits all traffic, and anyone on a trusted network can try your password. Trust
your home network, not a cafe or a train. Use a long password, or keys.

## What undo does not undo

- Your account password.
- The SSH host key. The server makes it at its first start and keeps it.
