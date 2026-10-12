"""Which profile, period and source an object in the bucket belongs to, from its key.

    [prefix]<profile>/<YYYY-MM>/<source>[-anything].csv

``<profile>`` is the profile's name in lower case with every run of other characters turned into
one hyphen (``Spanish ERP and bank`` is ``spanish-erp-and-bank``). ``<source>`` is a source of the
profile, ``invoices`` or ``payments``. What follows the source name up to ``.csv`` is free, so
``payments-2026-02-03.csv`` and a re-export under a new name both work.
"""

import re
from dataclasses import dataclass

_SLUG = re.compile(r"[^a-z0-9]+")
_KEY = re.compile(
    r"^(?P<profile>[a-z0-9][a-z0-9-]*)/(?P<period>\d{4}-(?:0[1-9]|1[0-2]))/"
    r"(?P<source>[a-z]+)(?:[-_.][^/]*)?\.csv$"
)


class BadKeyError(ValueError):
    """The key does not follow the naming convention. The message says how."""


@dataclass(frozen=True)
class ParsedKey:
    profile: str
    period: str
    source: str


def slugify(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-")


def parse_key(key: str, prefix: str = "") -> ParsedKey:
    if prefix and not key.startswith(prefix):
        raise BadKeyError(f"the key is not under the prefix {prefix!r}")
    relative = key[len(prefix) :]
    match = _KEY.match(relative)
    if match is None:
        raise BadKeyError(
            "the key does not look like <profile>/<YYYY-MM>/<source>.csv "
            "(lower case, with the profile's name written with hyphens)"
        )
    return ParsedKey(match["profile"], match["period"], match["source"])
