"""The capability `remote-login` as a Python class (BSX-194, BSX-300). The native part is `module.nix`.

Rules (Basix R13, R18), as in the NixOS module:
- The learner logs in with the account password. Root cannot log in. `password_login=False` allows keys only.
- TCP port 22 and UDP port 5353 (mDNS) open on the trusted interfaces of the machine only.
- A link where the machine opens SSH itself (`Port(22, interface="LINK")`) also gets Avahi and UDP port 5353.
  Port 22 on that link stays as the learner wrote it: this capability does not open it a second time (010-D45).
- Avahi runs only when such a link exists: an empty Avahi interface list means every interface.
- The server makes its host key at first start. No key enters the store.

The rule "warn when the firewall is off" reads the final configuration. Python runs before Nix, so that rule stays in
`module.nix`, which the capability names in `modules`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import TYPE_CHECKING, Annotated

from nixdantic import Expect, Model, Text
from pydantic import AfterValidator, ConfigDict
from pydantic_core import PydanticCustomError
from pynix_native import NativeModule
from pynix_os.networking.firewall import Firewall, Firewall_Interfaces_Value
from pynix_os.services.avahi import Avahi, Avahi_Publish
from pynix_os.services.openssh import Openssh, Openssh_Settings
from pynix_os.top import Top
from pynix_os.users.__options__ import Users, Users_Users_Value

from basix_config.component import Capability
from basix_config.ports import Port, Ports

if TYPE_CHECKING:
    from basix_config.machine import Machine

_KEY = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
    r" [A-Za-z0-9+/]+={0,3}( .*)?$"
)


def _public_key(value: str) -> str:
    """An authorized key is one public-key line. A private key must never get here, and the text never echoes it."""
    if "PRIVATE KEY" in value:
        raise PydanticCustomError(
            "key_private",
            "looks like a PRIVATE key. Never write it here. Give the public key, the line of the .pub file",
        )
    if not _KEY.match(value.strip()):
        raise PydanticCustomError(
            "key_format",
            'must be one public key line, for example "ssh-ed25519 AAAA... you@host" (the text of a .pub file)',
        )
    return value.strip()


# `Expect` comes first: it names a wrong type only. The validator after it keeps its own text.
PublicKey = Annotated[
    str, Expect('one public key line, for example "ssh-ed25519 AAAA... you@host"'), AfterValidator(_public_key)
]


class RemoteLogin(Capability):
    """Log in to this machine from another computer with SSH, on trusted interfaces only.

    Name the interfaces in `trusted_interfaces` of the machine. Then run `ssh ACCOUNT@NAME.local`.
    To turn off the password and use keys, write `RemoteLogin(password_login=False, authorized_keys=["ssh-ed25519 AAAA..."])`.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)

    password_login: bool = True
    """Let your account log in with its password. False: only a key logs in, so list your keys in `authorized_keys`."""
    authorized_keys: list[PublicKey] = []
    """Public keys that may log in to your account, one line each, for example "ssh-ed25519 AAAA... you@host"."""

    if TYPE_CHECKING:

        def __init__(self, *, password_login: bool = True, authorized_keys: list[PublicKey] = []) -> None:
            """Log in to this machine from another computer with SSH, on trusted interfaces only.

            Args:
                password_login: Let your account log in with its password. False: only a key logs in, so list your keys in `authorized_keys`.
                authorized_keys: Public keys that may log in to your account, one line each, for example "ssh-ed25519 AAAA... you@host".
            """
            ...

    def emit(self, machine: Machine) -> Iterator[Model | NativeModule]:
        trusted: list[str | Text] = [*machine.trusted_interfaces]
        # A port 22 that the machine opens on its own link (`Port(22, interface=...)`) also makes the server reachable.
        port_links = [
            p.interface
            for p in machine.user_ports
            if isinstance(p, Port | Ports)
            and p.interface is not None
            and p.protocol == "tcp"
            and (p.number == 22 if isinstance(p, Port) else p.low <= 22 <= p.high)
        ]
        # The links that Avahi serves: the trusted links first, then the port links. Each link appears once.
        announced: list[str | Text] = list({str(link): link for link in [*trusted, *port_links]}.values())
        port_link_names = {str(link) for link in port_links}
        yield Openssh(
            enable=True,
            openFirewall=False,  # the default opens port 22 on every interface; the firewall rule below opens it per interface
            settings=Openssh_Settings(
                PermitRootLogin="no",
                PasswordAuthentication=self.password_login,
                KbdInteractiveAuthentication=False,
                AllowUsers=[machine.user],
            ),
        )
        if announced:
            yield Avahi(
                enable=True,
                allowInterfaces=announced,
                openFirewall=False,
                publish=Avahi_Publish(enable=True, addresses=True),
                nssmdns4=True,
            )
        else:
            yield Top(
                warnings=[
                    "remote-login: no interface is trusted, so port 22 is closed on every interface. "
                    "Add your interface to `trusted_interfaces` of your machine in system.py."
                ]
            )
        if self.authorized_keys:
            yield Users(users={machine.user: Users_Users_Value(openssh_authorizedKeys_keys=list(self.authorized_keys))})
        # Key-only login with no key is a warning, not a refusal (009-D46): the console login still works, and the
        # learner can add a key to ~/.ssh/authorized_keys of the account later.
        if not self.password_login and not self.authorized_keys:
            yield Top(
                warnings=[
                    "remote-login: password login is off and no key is listed in `authorized_keys`. "
                    "Only a key in ~/.ssh/authorized_keys of your account can log in."
                ]
            )
        # UDP 5353 opens on every announced link. TCP 22 opens on a trusted link only: the learner opens it on a port link.
        yield Firewall(
            interfaces={
                str(name): Firewall_Interfaces_Value(allowedUDPPorts=[5353])
                if str(name) in port_link_names
                else Firewall_Interfaces_Value(allowedTCPPorts=[22], allowedUDPPorts=[5353])
                for name in announced
            }
        )
        yield NativeModule("capabilities/remote-login/module.nix")
