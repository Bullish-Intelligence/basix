"""The profile `Desktop` (L66, 009-D25): Hyprland under UWSM, Noctalia, greetd with tuigreet, foot, and Firefox.

Python emits every option. The native part is `profiles/desktop/module.nix`: it holds what Python cannot name, the
default look in `/etc/xdg` (009-D30) and the session wrapper `basix-session` (L67). The profile installs the
programs. The minix desktop capabilities (`Hyprland`, `Noctalia`, `Foot`) write config files only.

A value that a learner or a native module may want to change is `Default(...)` (L42): a normal value wins with no
`mkForce`. The learner API does not export `Default`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Annotated

from nixdantic import Default, Expect, Model, Package, Text
from pydantic import ConfigDict, StringConstraints
from pynix_native import NativeModule
from pynix_os.console.__options__ import Console
from pynix_os.environment.__options__ import Environment
from pynix_os.i18n.__options__ import I18n
from pynix_os.programs.hyprland import Hyprland
from pynix_os.programs.noctalia import Noctalia, Noctalia_RecommendedServices, Noctalia_Systemd
from pynix_os.services.greetd import Greetd
from pynix_os.systemd.user import User, User_Tmpfiles

from basix_config.component import Profile
from basix_config.machine import Program

if TYPE_CHECKING:
    from basix_config.machine import Machine

__all__ = ["Desktop", "SESSION"]

#: The name of the session wrapper (L67). The login screen shows this name, not a store path.
SESSION = "basix-session"

#: Letters, digits, spaces, and `, . ! ? -`. No quote, no backslash, and no `%`: the text sits inside one command line.
Greeting = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9 ,.!?-]{1,60}$"),
    Expect("a greeting (1 to 60 letters, digits, spaces, or , . ! ? -)"),
]
#: A console keyboard layout name: `us`, `de`, `de-latin1`, `uk`.
KeyMap = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z]{2,3}(-[a-z0-9]+)*$"),
    Expect("a keyboard layout name such as us, de, or de-latin1 (lower case letters and digits)"),
]
#: A locale: `C.UTF-8`, `en_US.UTF-8`, `de_DE.UTF-8`.
Locale = Annotated[
    str,
    StringConstraints(pattern=r"^([A-Za-z]{1,3}(_[A-Z]{2})?)\.(UTF-8|utf8)$"),
    Expect("a UTF-8 locale such as C.UTF-8 or en_US.UTF-8"),
]


class Desktop(Profile):
    """The graphical machine: Hyprland, the Noctalia bar, a text login screen, the terminal foot, and Firefox.

    Log in on the login screen. Then press SUPER+Q for the terminal and SUPER+B for the browser.
    Basix installs the programs and the default config files. Your `home.py` changes the config of your account.
    """

    model_config = ConfigDict(use_attribute_docstrings=True)

    browser: bool = True
    """Install Firefox. Set `False` to leave it out (it is the largest part of the desktop, about 1.5 GiB)."""
    greeting: Greeting | None = None
    """A line of text on the login screen. The default shows none."""
    keyboard_layout: KeyMap | None = None
    """The keyboard layout of the login screen and the text console, for example "de". The default keeps `us`."""
    locale: Locale = "C.UTF-8"
    """The language and character set. foot and Firefox warn when it is not UTF-8."""
    extra_programs: list[Annotated[Program, Expect("a program: Program(pkgs.NAME)")]] = []
    """More programs for every account, for example `Program(pkgs.mpv)`. They add to `programs` of the machine."""

    if TYPE_CHECKING:  # help while you type `Desktop(...)`; the editor reads it, Python never runs it

        def __init__(
            self,
            *,
            browser: bool = True,
            greeting: Greeting | None = None,
            keyboard_layout: KeyMap | None = None,
            locale: Locale = "C.UTF-8",
            extra_programs: list[Program] = [],  # noqa: B006 (a typed stub; Python never runs it)
        ) -> None:
            """The graphical machine: Hyprland, the Noctalia bar, a text login screen, the terminal foot, and Firefox.

            Args:
                browser: Install Firefox. Set `False` to leave it out (it is the largest part of the desktop, about 1.5 GiB).
                greeting: A line of text on the login screen. The default shows none.
                keyboard_layout: The keyboard layout of the login screen and the text console, for example "de". The default keeps `us`.
                locale: The language and character set. foot and Firefox warn when it is not UTF-8.
                extra_programs: More programs for every account, for example `Program(pkgs.mpv)`. They add to `programs` of the machine.
            """
            ...

    def emit(self, machine: Machine) -> Iterator[Model | NativeModule]:
        # The session. UWSM starts `graphical-session.target`, so user units with that target run.
        yield Hyprland(enable=True, withUWSM=True)
        # The shell (bar, lock screen). The recommended services are NetworkManager, Bluetooth, UPower, power profiles.
        yield Noctalia(
            enable=True,
            systemd=Noctalia_Systemd(enable=True),
            recommendedServices=Noctalia_RecommendedServices(enable=True),
        )
        # The login: greetd with tuigreet, no autologin. `--cmd` stands in for the session list: only the UWSM session
        # gives a shell (007 S8, step 2). The session is the wrapper `basix-session` (`module.nix`): the footer of the
        # login screen shows its name, not a store path (L67). A native module may set its own command (L42).
        greeting = [] if self.greeting is None else [f"--greeting '{self.greeting}' "]
        yield Greetd(
            enable=True,
            useTextGreeter=True,
            settings={
                "default_session": {
                    "command": Default(
                        Text(
                            Package(attr="tuigreet"), "/bin/tuigreet --time --remember ", *greeting, f"--cmd {SESSION}"
                        )
                    )
                }
            },
        )
        # 010-D24: the core seeds `/etc/locale.conf` at the first boot only (imperative locale). A learner who sets the
        # locale or the layout wants a later change of the field to apply, so the imperative locale goes off. With
        # neither field set, the core keeps it.
        if self.model_fields_set & {"locale", "keyboard_layout"}:
            yield I18n(defaultLocale=Default(self.locale), imperativeLocale=Default(False))
        else:
            yield I18n(defaultLocale=Default(self.locale))
        if self.keyboard_layout is not None:
            yield Console(keyMap=Default(self.keyboard_layout))
        packages = [Package(attr="foot")] + ([Package(attr="firefox")] if self.browser else [])
        yield Environment(systemPackages=[*packages, *(p.package for p in self.extra_programs)])
        # Noctalia reads only `~/.config/noctalia/*.toml`, so a user rule links the Basix file into the home.
        yield User(
            tmpfiles=User_Tmpfiles(
                rules=[
                    "d %h/.config/noctalia 0755 - - -",
                    "L %h/.config/noctalia/00-basix.toml - - - - /etc/xdg/noctalia/00-basix.toml",
                ]
            )
        )
        # The default look in `/etc/xdg` and the session wrapper are files: `profiles/desktop/module.nix` writes them.
        # `lib.machine` imports that file for every machine with this profile, so the component names no module.
