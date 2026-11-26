"""Packaged partition profiles per site, schema_version 1 (frozen 0.4.0).

Each site names its own table (sites.Site.partitions): data/partitions.json for Sherlock,
data/partitions-marlowe.json for Marlowe. The tables never merge, and every function takes the
site, Sherlock by default. A profile is preemptible, requeue, borrowed, gpus_allowed and batch
(booleans) plus a one-line courtesy text, which may be empty. batch is false where Sherlock
rejects every sbatch job (dev, interactive sessions only); the rules bullet leaves it to the
courtesy text. Listing is the authorization fact; there are no caps and no clock gate (the possu
window, the btrippe budget and the bioe and stat department terms are courtesy text only).
profile(name, site) raises ShkError(3, "unknown_partition") for a name the site's table does not
list, naming the ones it lists, where profiles(site).get() returns None. markdown(site) is the
"### Partition profiles" block that the site's rules files end with, byte for byte; its
introduction speaks of borrowed partitions only for a table that has one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from types import MappingProxyType

from sherlock_kit import sites
from sherlock_kit.errors import EXIT_REFUSED, ShkError

INTRO = "`shk` refuses partitions not listed here. Requeue only where `requeue=yes`."
BORROWED = (
    "Borrowed partitions need `shk occupancy` first and `--courtesy-ack`; releasing a job held "
    "there counts as submitting it (same occupancy check, budget and time window)."
)


@dataclass(frozen=True)
class Profile:
    """One listed partition: its flags and the courtesy text agents must follow."""

    name: str
    preemptible: bool
    requeue: bool
    borrowed: bool
    gpus_allowed: bool
    batch: bool
    courtesy: str


@cache
def _table(file: str) -> Mapping[str, Profile]:
    path = resources.files("sherlock_kit") / "data" / file
    table = json.loads(path.read_text(encoding="utf-8"))["partitions"]
    return MappingProxyType({name: Profile(name, **entry) for name, entry in table.items()})


def profiles(site: sites.Site = sites.SHERLOCK) -> Mapping[str, Profile]:
    """The site's packaged table, read-only and in file order."""
    return _table(site.partitions)


def profile(name: str, site: sites.Site = sites.SHERLOCK) -> Profile:
    """The profile of a partition the site lists; ShkError(3, "unknown_partition") otherwise."""
    table = profiles(site)
    found = table.get(name)
    if found is None:
        raise ShkError(
            EXIT_REFUSED,
            "unknown_partition",
            f"partition {name!r} is not listed; listed: {', '.join(table)}",
            hint="scheduler visibility is never authorization",
        )
    return found


def _bullet(p: Profile) -> str:
    flags = zip(
        ("preemptible", "requeue", "borrowed", "gpus"),
        (p.preemptible, p.requeue, p.borrowed, p.gpus_allowed),
    )
    text = ", ".join(f"{label}={'yes' if value else 'no'}" for label, value in flags)
    return f"- `{p.name}`: {text}." + (f" {p.courtesy}" if p.courtesy else "")


def markdown(site: sites.Site = sites.SHERLOCK) -> str:
    """The generated "### Partition profiles" block of the site's rules files."""
    table = profiles(site)
    intro = INTRO + (f" {BORROWED}" if any(p.borrowed for p in table.values()) else "")
    return "\n".join(["### Partition profiles", intro, *map(_bullet, table.values())]) + "\n"
