"""Packaged partition profiles: data/partitions.json, schema_version 1 (frozen 0.4.0).

A profile is exactly preemptible, requeue, borrowed, gpus_allowed (booleans) and a
one-line courtesy text (may be empty). Listing is the authorization fact; no caps and no
clock gate (the possu window and btrippe budget are courtesy text only).
profiles() -> read-only {name: Profile} in table order; profile(name) raises
ShkError(3, "unknown_partition") when unlisted (profiles().get does not); parse(text)
raises ShkError(1, "partitions_invalid"); markdown() is the "### Partition profiles"
block pasted byte-identically into both rules files.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
import json
import re
from types import MappingProxyType

from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, ShkError

FLAGS = ("preemptible", "requeue", "borrowed", "gpus_allowed")
INTRO = ("`shk` refuses partitions not listed here. Requeue only where `requeue=yes`. "
         "Borrowed partitions need `shk occupancy` first and `--courtesy-ack`.")


@dataclass(frozen=True)
class Profile:
    name: str
    preemptible: bool
    requeue: bool
    borrowed: bool
    gpus_allowed: bool
    courtesy: str


def _invalid(message: str) -> ShkError:
    return ShkError(EXIT_LOCAL, "partitions_invalid", f"partition table: {message}")


def _unique(pairs):
    if len({key for key, _ in pairs}) != len(pairs):
        raise _invalid("duplicate key")
    return dict(pairs)


def _profile(name, entry) -> Profile:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not isinstance(entry, dict) or set(entry) != {*FLAGS, "courtesy"}:
        raise _invalid(f"{name!r}: needs a valid name and exactly the keys {', '.join(FLAGS)}, courtesy")
    courtesy = entry["courtesy"]
    if (any(type(entry[flag]) is not bool for flag in FLAGS) or not isinstance(courtesy, str)
            or any(ord(c) < 32 or ord(c) == 127 for c in courtesy)):
        raise _invalid(f"{name}: flags must be booleans and courtesy one line of text")
    return Profile(name, *(entry[flag] for flag in FLAGS), courtesy)


def parse(text: str) -> MappingProxyType:
    try:
        payload = json.loads(text, object_pairs_hook=_unique)
    except ValueError as exc:
        raise _invalid(f"not JSON: {exc}") from None
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "partitions"}:
        raise _invalid("top level must be exactly schema_version and partitions")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise _invalid("schema_version must be 1")
    if not isinstance(payload["partitions"], dict) or not payload["partitions"]:
        raise _invalid("partitions must be a non-empty mapping")
    return MappingProxyType({name: _profile(name, entry) for name, entry in payload["partitions"].items()})


@lru_cache(maxsize=1)
def profiles() -> MappingProxyType:
    return parse((resources.files("sherlock_kit") / "data" / "partitions.json").read_text(encoding="utf-8"))


def profile(name: str) -> Profile:
    found = profiles().get(name)
    if found is None:
        raise ShkError(EXIT_REFUSED, "unknown_partition", f"partition {name!r} is not listed; listed: "
                       f"{', '.join(profiles())}", hint="scheduler visibility is never authorization")
    return found


def _bullet(p: Profile) -> str:
    flags = zip(("preemptible", "requeue", "borrowed", "gpus"), (p.preemptible, p.requeue, p.borrowed, p.gpus_allowed))
    text = ", ".join(f"{label}={'yes' if value else 'no'}" for label, value in flags)
    return f"- `{p.name}`: {text}." + (f" {p.courtesy}" if p.courtesy else "")


def markdown() -> str:
    return "\n".join(["### Partition profiles", INTRO, *map(_bullet, profiles().values())]) + "\n"
