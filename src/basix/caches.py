"""The caches of a build (L134, BSX-400).

A cache in `system.py` reaches `nix.conf` only after a switch. So the first build after the edit would not use it.
When the compiled machine names a cache that the running system does not have, root builds the machine with the new
caches as `--option` values. A user that Nix does not trust has its cache options ignored (SG43).

The command reads the running caches from the `nix.conf` of the running system (`Host.nix_conf`). It does not ask
`nix config show`: that command reads the files of the account too, and it merges more sources than the system.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SETTING = re.compile(r"^\s*(?P<key>[A-Za-z][\w-]*)\s*=\s*(?P<value>.*?)\s*$")
URL_KEYS = ("substituters", "extra-substituters")
KEY_KEYS = ("trusted-public-keys", "extra-trusted-public-keys")


def same_url(url: str) -> str:
    """The form that two addresses are compared in: no final slash, a lower-case scheme and host."""
    scheme, sep, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}{sep}{host.lower()}{slash}{path}".rstrip("/")


def read_conf(path: Path) -> dict[str, list[str]]:
    """The settings of a `nix.conf`, each as a list of words. A missing file gives no setting."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = SETTING.match(line.split("#", 1)[0])
        if match:
            out.setdefault(match["key"], []).extend(match["value"].split())
    return out


def machine_caches(compiled: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """The addresses and keys that the compiled machine adds: the `extra-*` settings of the `nix` fragments."""
    urls: list[str] = []
    keys: list[str] = []
    for fragment in compiled.get("fragments", []):
        path, value = fragment.get("path"), fragment.get("value")
        if path == ["nix"] and isinstance(value, dict):
            settings = value.get("settings")
        elif path == ["nix", "settings"]:
            settings = value
        else:
            continue
        if isinstance(settings, dict):
            urls += [str(url) for url in settings.get("extra-substituters", [])]
            keys += [str(key) for key in settings.get("extra-trusted-public-keys", [])]
    return urls, keys


def new_caches(compiled: Mapping[str, Any], conf: Path) -> tuple[list[str], list[str]] | None:
    """The addresses and keys to pass to the root build, or None when the running system has them all.

    A cache that the new machine drops needs no root build, so only an added address or key counts.
    """
    urls, keys = machine_caches(compiled)
    running = read_conf(conf)
    have_urls = {same_url(url) for key in URL_KEYS for url in running.get(key, [])}
    have_keys = {key for name in KEY_KEYS for key in running.get(name, [])}
    if all(same_url(url) in have_urls for url in urls) and all(key in have_keys for key in keys):
        return None
    return urls, keys
