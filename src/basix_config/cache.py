"""`Cache(url, key)`: a binary cache that the machine may use (L131, 009-D46).

`Machine(caches=[Cache(...)])` writes `nix.settings.extra-substituters` and `nix.settings.extra-trusted-public-keys`.
The compiler checks the address and the key at once, so a typing mistake never reaches `nix.conf`.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

from nixdantic import Expect, Plain
from pydantic import AfterValidator, ConfigDict, PrivateAttr
from pydantic_core import PydanticCustomError

from basix_config.component import learner_site

#: Caches that Basix already uses. A learner entry for one of them is a mistake that costs nothing, so it is refused.
BUILT_IN = ("https://cache.nixos.org",)

#: `(file:line, argument index, keyword, text, value)` of each warning; `value` finds the call when a line holds several. The loader (`compile.py`) prints them. The exit code stays 0.
WARNINGS: list[tuple[str, int, str, str, str]] = []


def same_url(url: str) -> str:
    """The form that two addresses are compared in: lower-case host, no final slash."""
    parts = urlsplit(url)
    return f"{parts.scheme.lower()}://{(parts.netloc or '').lower()}{parts.path.rstrip('/')}{'?' + parts.query if parts.query else ''}"


def _url(value: str) -> str:
    """One plain message for each way an address is wrong."""
    if re.search(r"\s", value):
        raise PydanticCustomError("cache_url", "must not hold a space")
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"}:
        raise PydanticCustomError("cache_url", 'must start with https://, for example "https://example.cachix.org"')
    if not parts.hostname:
        raise PydanticCustomError("cache_url", "must name a host after {scheme}://", {"scheme": parts.scheme})
    if "@" in parts.netloc:
        raise PydanticCustomError(
            "cache_url", "must not hold a user name or a password: /etc/nix/nix.conf is readable by every account"
        )
    if parts.fragment or (parts.query and not re.fullmatch(r"priority=[0-9]{1,4}", parts.query)):
        raise PydanticCustomError("cache_url", "may hold only the query ?priority=NUMBER")
    return value


def _key(value: str) -> str:
    """A cache key is `NAME:BASE64`. A public key holds 32 bytes. A secret key holds 64 bytes and must never get here."""
    name, colon, text = value.partition(":")
    if not colon or not name or re.search(r"\s", value):
        raise PydanticCustomError(
            "cache_key", 'must be NAME:BASE64, the public key of the cache, for example "example.cachix.org-1:AAAA...="'
        )
    try:
        size = len(base64.b64decode(text, validate=True))
    except (binascii.Error, ValueError):
        size = -1
    if size == 64:  # never echo the text (the loader does not print `found` for `cache_secret`)
        raise PydanticCustomError(
            "cache_secret",
            "looks like a SECRET key (it holds 64 bytes). Never write it here. Give the public key of the cache",
        )
    if size != 32:
        raise PydanticCustomError(
            "cache_key",
            'must be NAME:BASE64 with a 32 byte key, the public key of the cache, for example "example.cachix.org-1:AAAA...="',
        )
    return value


Url = Annotated[
    str,
    Expect('a cache address that starts with https://, for example "https://example.cachix.org"'),
    AfterValidator(_url),
]
Key = Annotated[str, Expect("the public key of the cache, NAME:BASE64"), AfterValidator(_key)]


def _warn(where: str, index: int, keyword: str, text: str, value: str) -> None:
    WARNINGS.append((where, index, keyword, text, value))


class Cache(Plain):
    """A binary cache, for example Cachix: `Cache("https://example.cachix.org", "example.cachix.org-1:AAAA...=")`."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, use_attribute_docstrings=True)
    _where: str = PrivateAttr(default="")
    url: Url
    """The address of the cache. It starts with https://. Cachix shows it as https://NAME.cachix.org."""
    key: Key
    """The public key of the cache, `NAME:BASE64`. Cachix shows it next to the address. Never a secret key."""

    if TYPE_CHECKING:

        def __init__(self, url: str, key: str, /) -> None:
            """A binary cache that this machine may use.

            Args:
                url: The address of the cache. It starts with https://. Cachix shows it as https://NAME.cachix.org.
                key: The public key of the cache, `NAME:BASE64`. Cachix shows it next to the address. Never a secret key.
            """
            ...

    else:

        def __init__(self, url: str, key: str, **data) -> None:
            super().__init__(url=url, key=key, **data)

        __init__.__pydantic_base_init__ = True

    def model_post_init(self, context: object, /) -> None:
        self._where = learner_site()
        parts = urlsplit(self.url)
        host = parts.hostname or ""
        if parts.scheme == "http":
            _warn(
                self._where,
                0,
                "url",
                "starts with http://, so the traffic is not encrypted. Nix still checks the signature of every file. Use http only on your own network.",
                self.url,
            )
        name = self.key.partition(":")[0]
        dotted = "." in host and not re.fullmatch(
            r"[0-9.]+|[0-9a-fA-F:]+", host
        )  # a host name, not an address and not a bare label
        if dotted and not re.fullmatch(re.escape(host) + r"(-[0-9]+)?", name):
            _warn(
                self._where,
                1,
                "key",
                f"holds the name `{name}`, but the host of the cache is `{host}`. "
                "That is normal for some caches. Check that you copied the key of this cache.",
                self.key,
            )
