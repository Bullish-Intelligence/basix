"""`Port` and `Ports`: the ports that a machine opens (009-D33, 009-D46).

A port with no link opens on every network link. A port with `interface=` opens on that link only. A port below 1024
is a system port (SSH, web): it needs `interface=`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Annotated, Literal

from nixdantic import Expect, Model, Plain
from pydantic import ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError
from pynix_os.networking.firewall import Firewall, Firewall_Interfaces_Value

from basix_config.fields import Interface

__all__ = ["Port", "Ports", "open_ports"]

UserPort = Annotated[int, Field(ge=1, le=65535), Expect("a port from 1 to 65535, for example Port(8080)")]
Protocol = Literal["tcp", "udp"]


def _below_1024(number: int, interface: str | None) -> None:
    """A port below 1024 is a system port. It opens on one named link only, never on every link."""
    if number < 1024 and interface is None:
        raise PydanticCustomError(
            "port_system",
            'must be 1024 or higher, or name its link: Port({number}, interface="NAME")',
            {"number": number},
        )


class Port(Plain):
    """One port that you open: `Port(8080)` on every network link, `Port(22, interface="tailscale0")` on one link."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, use_attribute_docstrings=True)
    number: UserPort
    """The port number. Below 1024 it needs `interface`: a system port opens on one named link only."""
    interface: Interface | None = None
    """Open the port on this link only, for example "tailscale0". Run `ip link` to see the names. Empty: every link."""
    protocol: Protocol = "tcp"
    """"tcp" (default) or "udp"."""

    if TYPE_CHECKING:

        def __init__(self, number: int, /, *, interface: str | None = None, protocol: Protocol = "tcp") -> None:
            """One port that you open.

            Args:
                number: The port number. Below 1024 it needs `interface`: a system port opens on one named link only.
                interface: Open the port on this link only, for example "tailscale0". Empty: every link.
                protocol: "tcp" (default) or "udp".
            """
            ...

    else:

        def __init__(self, number: int, **data) -> None:
            super().__init__(number=number, **data)

        __init__.__pydantic_base_init__ = True

    @model_validator(mode="after")
    def _system_port(self) -> Port:
        _below_1024(self.number, self.interface)
        return self


class Ports(Plain):
    """A range of ports that you open: `Ports(8000, 8099)`, or on one link `Ports(8000, 8099, interface="eno1")`."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, use_attribute_docstrings=True)
    low: UserPort
    """The first port of the range."""
    high: UserPort
    """The last port of the range. It must not be lower than the first port."""
    interface: Interface | None = None
    """Open the range on this link only, for example "eno1". Empty: every link. A range that starts below 1024 needs it."""
    protocol: Protocol = "tcp"
    """"tcp" (default) or "udp"."""

    if TYPE_CHECKING:

        def __init__(self, low: int, high: int, /, *, interface: str | None = None, protocol: Protocol = "tcp") -> None:
            """A range of ports that you open.

            Args:
                low: The first port of the range.
                high: The last port of the range. It must not be lower than the first port.
                interface: Open the range on this link only, for example "eno1". Empty: every link.
                protocol: "tcp" (default) or "udp".
            """
            ...

    else:

        def __init__(self, low: int, high: int, **data) -> None:
            super().__init__(low=low, high=high, **data)

        __init__.__pydantic_base_init__ = True

    @model_validator(mode="after")
    def _order(self) -> Ports:
        if self.high < self.low:
            raise PydanticCustomError("port_order", "must run from the lower port to the higher: Ports(LOW, HIGH)")
        _below_1024(self.low, self.interface)
        return self


def _rules(ports: list[Port | Ports], link: str | None, proto: str) -> dict[str, object]:
    """The firewall settings for the ports of one link (None: every link) and one protocol."""
    name = proto.upper()
    singles = [p.number for p in ports if isinstance(p, Port) and p.interface == link and p.protocol == proto]
    ranges = [
        {"from": p.low, "to": p.high}
        for p in ports
        if isinstance(p, Ports) and p.interface == link and p.protocol == proto
    ]
    out: dict[str, object] = {}
    if singles:
        out[f"allowed{name}Ports"] = singles
    if ranges:
        out[f"allowed{name}PortRanges"] = ranges
    return out


def open_ports(ports: list[Port | Ports]) -> Iterator[Model]:
    """The firewall values of `user_ports`.

    A port with no link gives the fragments `allowedTCPPorts` and `allowedTCPPortRanges` (UDP likewise). A port with a
    link goes to `networking.firewall.interfaces.LINK`: one value for each link.
    """
    for proto in ("tcp", "udp"):
        singles = _rules([p for p in ports if isinstance(p, Port)], None, proto)
        ranges = _rules([p for p in ports if isinstance(p, Ports)], None, proto)
        for part in (singles, ranges):
            if part:
                yield Firewall.model_validate(part)
    for link in dict.fromkeys(p.interface for p in ports if p.interface is not None):
        values = {**_rules(ports, link, "tcp"), **_rules(ports, link, "udp")}
        yield Firewall(interfaces={link: Firewall_Interfaces_Value.model_validate(values)})
