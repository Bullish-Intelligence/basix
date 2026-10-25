"""`Machine` and the curated tier: the fragments, the fields, and the refusals (CONCEPT 3.1, 009-D33, D39, D46)."""

from __future__ import annotations

import pytest
from nixdantic import Package
from pydantic import ValidationError

import basix_config
from basix_config import (
    Cache,
    Console,
    Machine,
    NativeModule,
    Port,
    Ports,
    Program,
    RemoteLogin,
    nixos,
    pkgs,
)

DISK = "/dev/disk/by-id/nvme-EXAMPLE"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE ada@laptop"


def machine(**more: object) -> Machine:
    return Machine(name="box", user="ada", disk=DISK, **more)  # ty: ignore[invalid-argument-type]


def paths(compiled: dict) -> list[tuple[str, ...]]:
    return [tuple(f["path"]) for f in compiled["fragments"]]


def fragment(compiled: dict, *path: str) -> dict:
    return next(f["value"] for f in compiled["fragments"] if tuple(f["path"]) == path)


def test_learner_names() -> None:
    """The star import gives the learner names only, and no priority wrapper (L128)."""
    assert set(basix_config.__all__) == {
        "Cache",
        "ConfigRef",
        "Console",
        "Desktop",
        "Input",
        "Machine",
        "Native",
        "NativeModule",
        "NixpkgsPin",
        "OWN",
        "Package",
        "Port",
        "Ports",
        "Program",
        "RemoteLogin",
        "Text",
        "nixos",
        "pkgs",
    }
    for wrapper in ("Force", "Before", "After", "Order", "Default", "Override"):
        assert wrapper not in basix_config.__all__


def test_core_fragments() -> None:
    compiled = machine().compile()
    assert compiled["name"] == "box"
    assert compiled["profile"] == "console"
    assert paths(compiled) == [("basix",), ("basix", "disk"), ("networking",), ("networking", "firewall")]
    assert fragment(compiled, "basix") == {"user": "ada"}
    assert fragment(compiled, "basix", "disk") == {"device": DISK}
    assert fragment(compiled, "networking") == {"hostName": "box"}
    assert "inputs" not in compiled and "nixpkgs" not in compiled and "values" not in compiled


def test_serial_console() -> None:
    assert fragment(machine(serial_console="ttyS0").compile(), "basix") == {"user": "ada", "console": ["ttyS0"]}


def test_state_version_and_no_disk() -> None:
    """`disk=None` writes no disk layout (009-D39); `state_version` sets `system.stateVersion`."""
    compiled = Machine(name="box", user="ada", disk=None, state_version="26.05").compile()
    assert ("basix", "disk") not in paths(compiled)
    assert fragment(compiled, "system") == {"stateVersion": "26.05"}


def test_trusted_interfaces_map_to_the_firewall() -> None:
    """A trusted link admits all traffic: the field maps straight to the NixOS firewall option (L125)."""
    compiled = machine(trusted_interfaces=["enp1s0", "wlan0"]).compile()
    assert fragment(compiled, "networking", "firewall") == {"trustedInterfaces": ["enp1s0", "wlan0"]}
    assert ("vendomat", "sharing") not in paths(compiled)
    assert fragment(machine().compile(), "networking", "firewall") == {"trustedInterfaces": []}


def test_user_ports() -> None:
    compiled = machine(
        user_ports=[
            Port(8080),
            Ports(8000, 8099),
            Port(53, protocol="udp", interface="eth0"),
            Port(22, interface="tailscale0"),
        ]
    ).compile()
    firewall = [f["value"] for f in compiled["fragments"] if tuple(f["path"]) == ("networking", "firewall")]
    assert {"allowedTCPPorts": [8080]} in firewall
    assert {"allowedTCPPortRanges": [{"from": 8000, "to": 8099}]} in firewall
    assert {"interfaces": {"eth0": {"allowedUDPPorts": [53]}}} in firewall
    assert {"interfaces": {"tailscale0": {"allowedTCPPorts": [22]}}} in firewall


def test_programs_are_package_references() -> None:
    compiled = machine(programs=[Program(pkgs.htop)]).compile()
    packages = fragment(compiled, "environment")["systemPackages"]
    assert packages[0]["__nixdantic_ref__"]["attr"] == "htop"
    assert Package("htop").attr == "htop"


def test_caches_write_extra_settings() -> None:
    key = "example.cachix.org-1:" + "A" * 43 + "="
    compiled = machine(caches=[Cache("https://example.cachix.org", key)]).compile()
    assert fragment(compiled, "nix") == {
        "settings": {"extra-substituters": ["https://example.cachix.org"], "extra-trusted-public-keys": [key]}
    }


def test_native_modules_and_values() -> None:
    compiled = machine(native_modules=[NativeModule("fans.nix")], values={"gpu": ["0000:19:00.0"]}).compile()
    assert compiled["modules"] == [
        {"file": "fans.nix", "source": compiled["modules"][0]["source"], "root": "workspace"}
    ]
    assert compiled["values"] == {"gpu": ["0000:19:00.0"]}


def test_console_profile_is_the_default() -> None:
    assert isinstance(machine().profile, Console)


@pytest.mark.parametrize(
    ("fields", "text"),
    [
        ({"user": 42}, "must be an account name"),
        ({"name": "Box"}, "must be a machine name"),
        ({"disk": "/dev/sda"}, "must not be a kernel name"),
        ({"disk": "nvme-X"}, "must be a path below /dev/disk/by-id/"),
        ({"trusted_interfaces": ["veth@if5"]}, "must not hold '@'"),
        ({"trusted_interfaces": ["eth0:1"]}, "must not hold ':'"),
        ({"trusted_interfaces": ["a-very-long-interface-name"]}, "must be an interface name"),
        ({"state_version": "26.06"}, "must be a release name"),
        ({"serial_console": "serial0"}, "must be a kernel console device"),
        ({"user_ports": [8080]}, "must be a port"),
        ({"programs": ["htop"]}, "must be a program"),
        ({"colour": "blue"}, "is not a setting"),
    ],
)
def test_machine_refusals(fields: dict, text: str) -> None:
    arguments = {"name": "box", "user": "ada", "disk": DISK} | fields
    with pytest.raises(ValidationError, match=text):
        Machine(**arguments)  # ty: ignore[invalid-argument-type]


def test_missing_user() -> None:
    with pytest.raises(ValidationError, match="is required"):
        Machine(name="box", disk=DISK)  # ty: ignore[missing-argument]


def test_own_root_and_double_state_version() -> None:
    from pynix_os.system.__options__ import System
    from pynix_os.top import Top

    with pytest.raises(ValidationError, match="write disk=None"):
        machine(options=[Top(fileSystems={"/": {"device": "/dev/x"}})])  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValidationError, match="sets the state version twice"):
        machine(state_version="26.05", options=[System(stateVersion="26.05")])


def test_system_ports_need_a_link() -> None:
    with pytest.raises(ValidationError, match='name its link: Port\\(80, interface="NAME"\\)'):
        Port(80)
    assert Port(80, interface="eth0").interface == "eth0"
    with pytest.raises(ValidationError, match="must run from the lower port to the higher"):
        Ports(9000, 8000)


def test_cache_refusals() -> None:
    key = "example.cachix.org-1:" + "A" * 43 + "="
    with pytest.raises(ValidationError, match="must not list https://cache.nixos.org"):
        machine(caches=[Cache("https://cache.nixos.org", key)])
    twice = [Cache("https://example.cachix.org", key), Cache("https://example.cachix.org/", key)]
    with pytest.raises(ValidationError, match="is in the list twice"):
        machine(caches=twice)
    with pytest.raises(ValidationError, match="must start with https://"):
        Cache("ftp://example.org", key)
    with pytest.raises(ValidationError, match="looks like a SECRET key"):
        Cache("https://example.cachix.org", "example.cachix.org-1:" + "A" * 86 + "==")


def remote_login_firewall(compiled: dict) -> dict:
    """The per-link firewall values that `RemoteLogin` emits."""
    out: dict = {}
    for f in compiled["fragments"]:
        if f["source"].endswith("(RemoteLogin)") and tuple(f["path"]) == ("networking", "firewall"):
            out.update(f["value"].get("interfaces", {}))
    return out


class TestRemoteLogin:
    """`RemoteLogin` (BSX-194, BSX-300, 009-D46)."""

    def test_default_keeps_the_password_login(self) -> None:
        compiled = machine(capabilities=[RemoteLogin()], trusted_interfaces=["enp1s0"]).compile()
        assert fragment(compiled, "services", "openssh")["settings"] == {
            "PermitRootLogin": "no",
            "PasswordAuthentication": True,
            "KbdInteractiveAuthentication": False,
            "AllowUsers": ["ada"],
        }
        assert fragment(compiled, "services", "avahi")["allowInterfaces"] == ["enp1s0"]
        assert compiled["modules"] == [
            {"file": "capabilities/remote-login/module.nix", "source": compiled["modules"][0]["source"]}
        ]
        assert compiled["modules"][0]["source"].endswith("(RemoteLogin)")

    def test_no_trusted_link_warns(self) -> None:
        compiled = machine(capabilities=[RemoteLogin()]).compile()
        warnings = [f["value"]["warnings"] for f in compiled["fragments"] if tuple(f["path"]) == ()]
        assert "no interface is trusted" in warnings[0][0]
        assert ("services", "avahi") not in paths(compiled)

    def test_port_22_on_its_own_link_needs_no_trusted_link(self) -> None:
        compiled = machine(capabilities=[RemoteLogin()], user_ports=[Port(22, interface="tailscale0")]).compile()
        assert not [f for f in compiled["fragments"] if tuple(f["path"]) == ()]

    def test_port_22_on_its_own_link_announces_the_machine(self) -> None:
        """010-D45: Avahi and UDP 5353 serve the links of `Port(22, interface=...)`, with no trusted link."""
        compiled = machine(
            capabilities=[RemoteLogin()],
            user_ports=[Port(22, interface="eno1"), Port(80, interface="eno1"), Port(22, interface="wlp2")],
        ).compile()
        avahi = fragment(compiled, "services", "avahi")
        assert avahi["allowInterfaces"] == ["eno1", "wlp2"]
        assert avahi["enable"] is True
        assert avahi["nssmdns4"] is True
        assert avahi["publish"] == {"enable": True, "addresses": True}
        assert avahi["openFirewall"] is False
        remote = remote_login_firewall(compiled)
        assert remote == {"eno1": {"allowedUDPPorts": [5353]}, "wlp2": {"allowedUDPPorts": [5353]}}
        # The learner's own rule keeps port 22: it is the only fragment with TCP 22 on these links.
        own = [
            link
            for f in compiled["fragments"]
            if f["source"].endswith("(Machine)") and tuple(f["path"]) == ("networking", "firewall")
            for link, value in f["value"].get("interfaces", {}).items()
            if 22 in value.get("allowedTCPPorts", [])
        ]
        assert own == ["eno1", "wlp2"]

    def test_trusted_and_port_links_join(self) -> None:
        compiled = machine(
            capabilities=[RemoteLogin()],
            trusted_interfaces=["tail0", "eno1"],
            user_ports=[Port(22, interface="eno1"), Port(22, interface="wlp2")],
        ).compile()
        assert fragment(compiled, "services", "avahi")["allowInterfaces"] == ["tail0", "eno1", "wlp2"]
        assert remote_login_firewall(compiled) == {
            "tail0": {"allowedTCPPorts": [22], "allowedUDPPorts": [5353]},
            "eno1": {"allowedUDPPorts": [5353]},
            "wlp2": {"allowedUDPPorts": [5353]},
        }

    def test_a_port_range_with_22_counts_as_a_port_link(self) -> None:
        compiled = machine(capabilities=[RemoteLogin()], user_ports=[Ports(20, 30, interface="eno1")]).compile()
        assert fragment(compiled, "services", "avahi")["allowInterfaces"] == ["eno1"]

    def test_other_ports_do_not_start_avahi(self) -> None:
        """Only TCP port 22 on a named link counts. A UDP 22, a port 80, and a port with no link do not."""
        user_ports = [
            Port(22, interface="eno1", protocol="udp"),
            Port(80, interface="eno1"),
            Port(2222),
            Ports(8000, 8099, interface="eno1"),
        ]
        compiled = machine(capabilities=[RemoteLogin()], user_ports=user_ports).compile()
        assert ("services", "avahi") not in paths(compiled)
        assert remote_login_firewall(compiled) == {}
        warnings = [f["value"]["warnings"] for f in compiled["fragments"] if tuple(f["path"]) == ()]
        assert "no interface is trusted" in warnings[0][0]

    def test_key_only_login_with_keys(self) -> None:
        compiled = machine(
            capabilities=[RemoteLogin(password_login=False, authorized_keys=[KEY])], trusted_interfaces=["enp1s0"]
        ).compile()
        assert fragment(compiled, "services", "openssh")["settings"]["PasswordAuthentication"] is False
        assert fragment(compiled, "users")["users"]["ada"] == {"openssh": {"authorizedKeys": {"keys": [KEY]}}}
        assert not [f for f in compiled["fragments"] if tuple(f["path"]) == ()]

    def test_key_only_login_with_no_key_warns_and_does_not_refuse(self) -> None:
        """009-D46 says warn. The console login still works, so the machine is not locked out."""
        compiled = machine(capabilities=[RemoteLogin(password_login=False)], trusted_interfaces=["enp1s0"]).compile()
        warnings = [f["value"]["warnings"][0] for f in compiled["fragments"] if tuple(f["path"]) == ()]
        assert len(warnings) == 1
        assert "password login is off and no key is listed" in warnings[0]

    def test_keys_are_checked(self) -> None:
        """The compiler never prints the value of a private key (see `test_compile.py`)."""
        with pytest.raises(ValidationError, match="looks like a PRIVATE key"):
            RemoteLogin(authorized_keys=["-----BEGIN OPENSSH PRIVATE KEY-----\nSECRETSECRET"])
        with pytest.raises(ValidationError, match="must be one public key line"):
            RemoteLogin(authorized_keys=["ssh-ed25519"])


class TestProvenance:
    """The key `provenance` lists the option paths of each field and capability (`basix explain`, 009-D14)."""

    def test_every_field_has_a_key(self) -> None:
        table = machine().compile()["provenance"]
        fields = set(Machine.model_fields) - {"capabilities", "profile"}
        assert set(table) == fields | {"Console"}
        assert table["name"] == ["networking.hostName"]
        assert table["user"] == ["basix.user"]
        assert table["disk"] == ["basix.disk.device"]
        assert table["trusted_interfaces"] == ["networking.firewall.trustedInterfaces"]
        for empty in (
            "state_version",
            "serial_console",
            "programs",
            "user_ports",
            "caches",
            "inputs",
            "options",
            "Console",
        ):
            assert table[empty] == []

    def test_the_fields_that_share_a_fragment_get_their_own_path(self) -> None:
        """`user` and `serial_console` make one `basix` fragment. Each keeps its own path."""
        compiled = machine(serial_console="ttyS0").compile()
        assert fragment(compiled, "basix") == {"user": "ada", "console": ["ttyS0"]}
        assert compiled["provenance"]["user"] == ["basix.user"]
        assert compiled["provenance"]["serial_console"] == ["basix.console"]

    def test_the_other_fields(self) -> None:
        key = "example.cachix.org-1:" + "A" * 43 + "="
        table = machine(
            state_version="26.05",
            programs=[Program(pkgs.htop)],
            user_ports=[Port(8080), Port(22, interface="tailscale0")],
            caches=[Cache("https://example.cachix.org", key)],
            options=[nixos.Networking_Firewall(allowPing=False)],
        ).compile()["provenance"]
        assert table["state_version"] == ["system.stateVersion"]
        assert table["programs"] == ["environment.systemPackages"]
        assert table["user_ports"] == [
            "networking.firewall.allowedTCPPorts",
            "networking.firewall.interfaces.tailscale0.allowedTCPPorts",
        ]
        assert table["caches"] == ["nix.settings.extra-substituters", "nix.settings.extra-trusted-public-keys"]
        assert table["options"] == ["networking.firewall.allowPing"]

    def test_a_capability_has_its_class_name_as_key(self) -> None:
        table = machine(capabilities=[RemoteLogin()], trusted_interfaces=["enp1s0"]).compile()["provenance"]
        assert "services.openssh.enable" in table["RemoteLogin"]
        assert "networking.firewall.interfaces.enp1s0.allowedTCPPorts" in table["RemoteLogin"]
        assert "networking.firewall.trustedInterfaces" not in table["RemoteLogin"]
        assert all(table["RemoteLogin"].count(p) == 1 for p in table["RemoteLogin"])

    def test_the_key_keeps_the_fragments(self) -> None:
        """The fragments and the modules are what `emit` gives, in the same order (the drvPath stays)."""
        m = machine(capabilities=[RemoteLogin()], serial_console="ttyS0", programs=[Program(pkgs.htop)])
        compiled = m.compile()
        emitted = [type(v).__name__ for v in m.emit()]
        assert emitted[:2] == ["Basix", "Disk"]
        assert [tuple(f["path"]) for f in compiled["fragments"]][:3] == [("basix",), ("basix", "disk"), ("networking",)]
        assert {"fragments", "modules", "name", "profile", "provenance"} <= set(compiled)
