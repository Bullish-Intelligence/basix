"""The options of the Basix core, as typed classes (the hand-written twin of the generated `pynix_os` classes).

`modules/core.nix` declares `basix.user`, `basix.console`, and `basix.disk.device`. The `Machine` fragments set them.
The classes have the shape of a generated root class, so the encoder treats them like one.
"""

from __future__ import annotations

from typing import Annotated, ClassVar

from nixdantic import Model, Unset, UnsetType, matching

__all__ = ["Basix", "Disk"]


class Basix(Model):
    """Options below `basix`."""

    __nix_path__: ClassVar[tuple[str, ...]] = ("basix",)
    console: list[str] | UnsetType = Unset
    """Kernel console devices, in `console=` order. The default is empty, so the machine uses the screen."""
    user: Annotated[str, matching("[a-z][a-z0-9-]{0,31}")] | None | UnsetType = Unset
    """The account name of the learner, chosen at install."""


class Disk(Model):
    """Options below `basix.disk`."""

    __nix_path__: ClassVar[tuple[str, ...]] = ("basix", "disk")
    device: str | UnsetType = Unset
    """Target disk, a /dev/disk/by-id path."""
