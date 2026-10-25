"""`Machine`, the root of `system.py` (CONCEPT 3.1), and the profile `Console`.

A learner writes one `Machine` in `system.py`. `Machine.compile` turns it into the JSON that `lib.machine` reads.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from nixdantic import Expect, JsonValue, Model, Plain, RootOnly, Unset, fragment
from pydantic import AfterValidator, ConfigDict, PrivateAttr, model_validator
from pydantic_core import PydanticCustomError
from pynix_native import Input, NativeModule, input_json, registered_nixpkgs
from pynix_os.environment.__options__ import Environment
from pynix_os.networking.__options__ import Networking
from pynix_os.networking.firewall import Firewall
from pynix_os.nix.__options__ import Nix, Nix_Settings
from pynix_os.system.__options__ import System
from pynix_os.top import Top
from pynix_pkgs import Pkg

from basix_config.cache import BUILT_IN, Cache, same_url
from basix_config.component import Capability, ComponentError, Profile, learner_frame
from basix_config.fields import Account, ConsoleDevice, DiskId, Interface, Name, StateVersion
from basix_config.options import Basix, Disk
from basix_config.ports import Port, Ports, open_ports
from basix_config.provenance import add as add_provenance

__all__ = ["Console", "Machine", "Program"]


class Console(Profile):
    """The text console machine. It adds nothing to the core."""


class Program(Plain):
    """A program for every account of the machine: `Program(pkgs.htop)`."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, use_attribute_docstrings=True)
    package: Annotated[Pkg, Expect("a package: pkgs.NAME (search for the name at https://search.nixos.org/packages)")]
    """The package: `pkgs.NAME`. Search for the name at https://search.nixos.org/packages."""

    if TYPE_CHECKING:

        def __init__(self, package: Pkg, /) -> None:
            """A program for every account of the machine, for example `Program(pkgs.htop)`.

            Args:
                package: The package: `pkgs.NAME`. Search for the name at https://search.nixos.org/packages.
            """
            ...

    else:

        def __init__(self, package: Pkg) -> None:
            super().__init__(package=package)

        __init__.__pydantic_base_init__ = True


def _called(call: ast.Call) -> str:
    """The name that a call uses: `Machine` for `Machine(...)` and for `basix_config.Machine(...)`."""
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _one_each(caches: list[Cache]) -> list[Cache]:
    """Two entries with the same address are a mistake. Basix already uses cache.nixos.org."""
    built_in = {same_url(u) for u in BUILT_IN}
    seen = set(built_in)
    for index, cache in enumerate(caches):
        if same_url(cache.url) in built_in:
            raise PydanticCustomError(
                "cache_duplicate", "must not list {url}: Basix always uses it", {"url": cache.url, "item": index}
            )
        if same_url(cache.url) in seen:
            raise PydanticCustomError(
                "cache_duplicate",
                "must name each cache once, but {url} is in the list twice",
                {"url": cache.url, "item": index},
            )
        seen.add(same_url(cache.url))
    return caches


class Machine(Plain):
    """One Basix machine."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, use_attribute_docstrings=True)
    _where: str = PrivateAttr(default="")
    _origin: tuple[Path, int] | None = PrivateAttr(default=None)

    name: Name
    """The machine name. It is the host name, and other computers reach the machine as NAME.local."""
    user: Account
    """Your account name: lower-case letters, digits, and '-', at most 32 characters."""
    disk: DiskId | None
    """The disk of the machine, as a /dev/disk/by-id/ path. The install writes it. `None` keeps the file systems of an existing machine."""
    state_version: StateVersion | None = None
    """The release of NixOS that first installed this machine, for example "26.05". The install writes it. Never change it to a newer release: it protects the data of programs on the disk."""
    profile: Profile = Console()
    """The base of the machine. `Console()` is a text console. `Desktop()` is a graphical desktop."""
    capabilities: list[Capability] = []
    """The capabilities of the machine, for example `RemoteLogin()`."""
    programs: list[Annotated[Program, Expect("a program: Program(pkgs.NAME)")]] = []
    """Programs for every account of the machine, for example `Program(pkgs.htop)`."""
    trusted_interfaces: list[Interface] = []
    """Network interfaces that you trust, for example "enp1s0". Run `ip link` to see the names."""
    serial_console: ConsoleDevice | None = None
    """The serial console of the machine, for example "ttyS0". The install writes it when the live system booted with one."""
    user_ports: list[
        Annotated[Port | Ports, Expect('a port: Port(8080), Ports(8000, 8099), or Port(22, interface="tailscale0")')]
    ] = []
    """Ports that you open, for example `Port(8080)` (every link), `Ports(8000, 8099)`, or `Port(22, interface="tailscale0")` (one link)."""
    caches: Annotated[
        list[Annotated[Cache, Expect('a cache: Cache("https://example.cachix.org", "example.cachix.org-1:AAAA...=")')]],
        AfterValidator(_one_each),
    ] = []
    """Binary caches that the machine may use besides cache.nixos.org, for example `Cache("https://example.cachix.org", "example.cachix.org-1:AAAA...=")`."""
    inputs: list[Input] = []
    """Extra flake inputs, for example `Input("nirinit", "github:amaanq/nirinit", modules=["nirinit"])`."""
    options: list[
        Annotated[
            Model,
            Expect("a NixOS option class of nixos, for example nixos.Networking_Firewall(allowPing=False)"),
            RootOnly,
        ]
    ] = []
    """NixOS options that no capability covers, written with a class of `nixos`."""
    native_modules: list[NativeModule] = []
    """Native NixOS module files of the workspace folder, for example `NativeModule("fans.nix")`."""
    values: dict[str, JsonValue] = {}
    """Values that a native module reads as `machine.values.NAME`, for example `{"gpu_pci": ["0000:19:00.0"]}`."""

    if TYPE_CHECKING:  # help while you type `Machine(...)`; the editor reads it, Python never runs it

        def __init__(
            self,
            *,
            name: Name,
            user: Account,
            disk: DiskId | None,
            state_version: StateVersion | None = None,
            profile: Profile = Console(),
            capabilities: list[Capability] = [],
            programs: list[Program] = [],
            trusted_interfaces: list[Interface] = [],
            serial_console: ConsoleDevice | None = None,
            user_ports: list[Port | Ports] = [],
            caches: list[Cache] = [],
            inputs: list[Input] = [],
            options: list[Model] = [],
            native_modules: list[NativeModule] = [],
            values: dict[str, JsonValue] = {},
        ) -> None:
            """One Basix machine.

            Args:
                name: The machine name. It is the host name, and other computers reach the machine as NAME.local.
                user: Your account name: lower-case letters, digits, and '-', at most 32 characters.
                disk: The disk of the machine, as a /dev/disk/by-id/ path. The install writes it. `None` keeps the file systems of an existing machine.
                state_version: The release of NixOS that first installed this machine, for example "26.05". The install writes it.
                profile: The base of the machine. `Console()` is a text console. `Desktop()` is a graphical desktop.
                capabilities: The capabilities of the machine, for example `RemoteLogin()`.
                programs: Programs for every account of the machine, for example `Program(pkgs.htop)`.
                trusted_interfaces: Network interfaces that you trust, for example "enp1s0". Run `ip link` to see the names.
                serial_console: The serial console of the machine, for example "ttyS0". The install writes it when the live system booted with one.
                user_ports: Ports that you open, for example `Port(8080)` (every link), `Ports(8000, 8099)`, or `Port(22, interface="tailscale0")` (one link).
                caches: Binary caches that the machine may use besides cache.nixos.org, for example `Cache("https://example.cachix.org", "example.cachix.org-1:AAAA...=")`.
                inputs: Extra flake inputs, for example `Input("nirinit", "github:amaanq/nirinit", modules=["nirinit"])`.
                options: NixOS options that no capability covers, written with a class of `nixos`.
                native_modules: Native NixOS module files of the workspace folder, for example `NativeModule("fans.nix")`.
                values: Values that a native module reads as `machine.values.NAME`.
            """
            ...

    @model_validator(mode="after")
    def _own_root(self) -> Machine:
        """The Basix disk layout writes the file system `/`. A machine with a disk must not define its own."""
        if self.state_version is not None and any(
            isinstance(i, System) and i.stateVersion is not Unset for i in self.options
        ):
            raise PydanticCustomError(
                "state_twice", "sets the state version twice: keep state_version, and remove stateVersion from options"
            )
        if self.disk is not None:
            for item in self.options:
                files = item.fileSystems if isinstance(item, Top) else None
                if isinstance(files, dict) and "/" in files:
                    raise PydanticCustomError(
                        "own_root",
                        'has a disk, and Basix writes the file system "/" there. '
                        "To keep the file systems of this machine, write disk=None",
                    )
        return self

    def model_post_init(self, context: object, /) -> None:
        self._origin = learner_frame()
        self._where = f"{self._origin[0].name}:{self._origin[1]}" if self._origin else "unknown"

    def option_sites(self) -> list[str]:
        """The learner line of each item of `options=[...]`, so a conflict names the line of the item (`BSX-385`).

        The line of an item is a fact of the syntax tree of the entry. When the tree has no `Machine(...)` call at the
        recorded line, or its `options` is not a list written in place, every item gets the line of `Machine(...)`.
        """
        fallback = [self._where] * len(self.options)
        if self._origin is None or not self.options:
            return fallback
        path, line = self._origin
        try:
            tree = ast.parse(path.read_text(), path.name)
        except (OSError, SyntaxError, ValueError):
            return fallback
        for call in ast.walk(tree):
            if not (isinstance(call, ast.Call) and call.lineno == line and _called(call) == "Machine"):
                continue
            for keyword in call.keywords:
                if keyword.arg == "options" and isinstance(keyword.value, ast.List | ast.Tuple):
                    items = keyword.value.elts
                    if len(items) == len(self.options):
                        return [f"{path.name}:{item.lineno}" for item in items]
        return fallback

    def emit(self) -> Iterator[Model | NativeModule]:
        """The values of the machine itself. The profile and the capabilities emit their own."""
        for _owner, value in self.emit_fields():
            yield value

    def emit_fields(self) -> Iterator[tuple[str | dict[str, str], Model | NativeModule]]:
        """The values of the machine itself, each with the field that made it (`basix explain`).

        The owner is a field name. The `basix` values of `user` and `serial_console` are one fragment, so their owner is
        a map from the name in the value to the field.
        """
        if self.serial_console is None:
            yield "user", Basix(user=self.user)
        else:
            yield {"user": "user", "console": "serial_console"}, Basix(user=self.user, console=[self.serial_console])
        if self.disk is not None:  # `disk=None` is the migration mode: the machine keeps its own file systems (009-D39)
            yield "disk", Disk(device=self.disk)
        if self.state_version is not None:
            yield "state_version", System(stateVersion=self.state_version)
        yield "name", Networking(hostName=self.name)
        # A trusted link admits all traffic (L125). The field maps straight to the NixOS firewall option. The fragment
        # keeps the place and the empty list of the spike value `vendomat.sharing.trustedInterfaces` that it replaces.
        yield "trusted_interfaces", Firewall(trustedInterfaces=list(self.trusted_interfaces))
        if self.programs:
            yield "programs", Environment(systemPackages=[p.package for p in self.programs])
        for value in open_ports(self.user_ports):
            yield "user_ports", value
        # `extra-*` adds to the list of the core and of NixOS. Nothing here replaces cache.nixos.org (L131).
        if self.caches:
            yield (
                "caches",
                Nix(
                    settings=Nix_Settings(
                        **{
                            "extra-substituters": [c.url for c in self.caches],
                            "extra-trusted-public-keys": [c.key for c in self.caches],
                        }
                    )
                ),
            )
        for value in self.options:
            yield "options", value
        for module in self.native_modules:
            yield "native_modules", module

    def compile(self) -> dict[str, Any]:
        """The compiled machine: the fragments and the native modules, with the learner line of each.

        The key `provenance` lists, for each field of the machine and each capability, the option paths that it sets.
        """
        fragments: list[dict[str, Any]] = []
        modules: list[dict[str, str]] = []
        # A field that sets no option has an empty list, so `basix explain` knows the name. A component has a key
        # when the machine uses it. `capabilities` and `profile` are lists of components: their classes are the keys.
        provenance: dict[str, list[str]] = {
            name: [] for name in type(self).model_fields if name not in {"capabilities", "profile"}
        }
        components = (self.profile, *self.capabilities)
        parts: list[tuple[str, str, Iterator[tuple[str | dict[str, str], Model | NativeModule]]]] = [
            (self._where, "Machine", self.emit_fields())
        ]
        parts += [(c._where, type(c).__name__, ((type(c).__name__, v) for v in c.emit(self))) for c in components]
        sites = iter(self.option_sites())
        for where, label, values in parts:
            try:
                for owner, value in values:
                    # An item of `options=[...]` has its own line; every other value has the line of its owner.
                    here = next(sites) if label == "Machine" and owner == "options" else where
                    if isinstance(value, NativeModule):
                        # A module that the machine names is a file of the workspace; a module of a capability is a
                        # file of the Basix flake.
                        root = {"root": "workspace"} if label == "Machine" else {}
                        modules.append({"file": value.file, "source": f"{here} ({label})", **root})
                    else:
                        made = fragment(value, source=here, label=label)
                        fragments.append(made)
                        add_provenance(provenance, owner, made)
            except Exception as error:  # the component failed: name the learner line of the component
                raise ComponentError(where, label, error) from error
        for component in components:
            provenance.setdefault(type(component).__name__, [])
        out: dict[str, Any] = {
            "name": self.name,
            "profile": type(self.profile).__name__.lower(),
            "fragments": fragments,
            "modules": modules,
            "provenance": provenance,
        }
        if self.inputs:  # the key is absent when the list is empty, so a machine with no input keeps its JSON
            out["inputs"] = [input_json(i, self._where) for i in self.inputs]
        if nixpkgs := registered_nixpkgs():
            out["nixpkgs"] = nixpkgs
        if self.values:
            out["values"] = dict(self.values)
        return out
