"""Packaged partition profiles: data/partitions.json, schema_version 1 (frozen 0.4.0).

A profile is preemptible, requeue, borrowed, gpus_allowed and batch (booleans) plus a one-line
courtesy text, which may be empty. batch is false where Sherlock rejects every sbatch job (dev,
interactive sessions only); the rules bullet leaves it to the courtesy text. Listing is the
authorization fact; there are no caps and no clock gate (the possu window and the btrippe budget
are courtesy text only). profile(name) raises ShkError(3, "unknown_partition") for an unlisted
name, where profiles().get() returns None. markdown() is the "### Partition profiles" block that
both rules files end with, byte for byte.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources
from types import MappingProxyType

from sherlock_kit.errors import EXIT_REFUSED, ShkError

INTRO = (
    "`shk` refuses partitions not listed here. Requeue only where `requeue=yes`. "
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
def profiles() -> Mapping[str, Profile]:
    """The packaged table, read-only and in file order."""
    path = resources.files("sherlock_kit") / "data" / "partitions.json"
    table = json.loads(path.read_text(encoding="utf-8"))["partitions"]
    return MappingProxyType({name: Profile(name, **entry) for name, entry in table.items()})


def profile(name: str) -> Profile:
    """The profile of a listed partition; ShkError(3, "unknown_partition") otherwise."""
    found = profiles().get(name)
    if found is None:
        raise ShkError(
            EXIT_REFUSED,
            "unknown_partition",
            f"partition {name!r} is not listed; listed: {', '.join(profiles())}",
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


def markdown() -> str:
    """The generated "### Partition profiles" block of the rules files."""
    return "\n".join(["### Partition profiles", INTRO, *map(_bullet, profiles().values())]) + "\n"
