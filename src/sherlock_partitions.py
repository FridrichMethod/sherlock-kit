"""Packaged Slurm partition profiles: what each consumer partition permits.

The table is toolkit data shipped as ``sherlock_kit_data/partitions.json``, not a
scheduler query. A profile states only booleans (preemptible, requeue, borrowed,
gpus_allowed) and a courtesy sentence; it never carries caps. Unknown partitions
and malformed tables are refused. This module imports no sibling module.
"""
from __future__ import annotations

import hashlib
from importlib import resources
import json
import re
from types import MappingProxyType

SCHEMA_VERSION = 1
RESOURCE = "partitions.json"
PARTITION_NAME = re.compile(r"[A-Za-z0-9_.-]+")
FLAG_KEYS = ("preemptible", "requeue", "borrowed", "gpus_allowed")
PROFILE_KEYS = frozenset({*FLAG_KEYS, "courtesy"})
_TABLE_KEYS = frozenset({"schema_version", "partitions"})
_SUMMARY_FLAGS = (("preemptible", "preemptible"), ("requeue", "requeue"),
                  ("borrowed", "borrowed"), ("gpus", "gpus_allowed"))


class PartitionError(ValueError):
    """The partition table or a requested partition cannot be trusted."""


def _partitions_bytes() -> bytes:
    return resources.files("sherlock_kit_data").joinpath(RESOURCE).read_bytes()


def partitions_text() -> str:
    """Packaged table text; identical for PYTHONPATH=src and frozen installs."""
    return _partitions_bytes().decode("utf-8")


def partitions_sha256() -> str:
    return hashlib.sha256(_partitions_bytes()).hexdigest()


def _unique_pairs(pairs):
    mapping = {}
    for key, value in pairs:
        if key in mapping:
            raise PartitionError(f"duplicate key {key!r} in partition table")
        mapping[key] = value
    return mapping


def _control_characters(text):
    return any(ord(character) < 32 or ord(character) == 127 for character in text)


def _checked_profile(name, entry):
    if not isinstance(name, str) or not PARTITION_NAME.fullmatch(name):
        raise PartitionError(f"partition name {name!r} must match {PARTITION_NAME.pattern}")
    if not isinstance(entry, dict):
        raise PartitionError(f"partition {name!r} profile must be a mapping")
    if set(entry) != PROFILE_KEYS:
        unexpected = sorted(set(entry) - PROFILE_KEYS)
        missing = sorted(PROFILE_KEYS - set(entry))
        raise PartitionError(f"partition {name!r} profile keys: unexpected {unexpected}, missing {missing}; "
                             "profiles carry only boolean flags and a courtesy sentence")
    for key in FLAG_KEYS:
        if type(entry[key]) is not bool:
            raise PartitionError(f"partition {name!r} flag {key} must be boolean")
    courtesy = entry["courtesy"]
    if not isinstance(courtesy, str) or _control_characters(courtesy):
        raise PartitionError(f"partition {name!r} courtesy must be a single line of text")
    return MappingProxyType({key: entry[key] for key in (*FLAG_KEYS, "courtesy")})


def parse_partitions(text: str) -> MappingProxyType:
    """Validate a partition table and return read-only profiles keyed by name."""
    if not isinstance(text, str):
        raise PartitionError("partition table must be text")
    try:
        payload = json.loads(text, object_pairs_hook=_unique_pairs)
    except ValueError as exc:
        raise PartitionError(f"partition table is not valid JSON: {exc}") from None
    if not isinstance(payload, dict) or set(payload) != _TABLE_KEYS:
        raise PartitionError(f"partition table must contain exactly {sorted(_TABLE_KEYS)}")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise PartitionError(f"partition table schema_version must be {SCHEMA_VERSION}")
    table = payload["partitions"]
    if not isinstance(table, dict) or not table:
        raise PartitionError("partition table must map at least one partition name to a profile")
    return MappingProxyType({name: _checked_profile(name, entry) for name, entry in table.items()})


def partition_profiles() -> MappingProxyType:
    """Read-only profiles from the packaged table, re-validated on every call."""
    return parse_partitions(partitions_text())


def partition_profile(name) -> MappingProxyType:
    """One packaged profile; any partition outside the table is refused."""
    profiles = partition_profiles()
    if not isinstance(name, str) or name not in profiles:
        raise PartitionError(f"unknown partition {name!r}; packaged profiles: {', '.join(sorted(profiles))}")
    return profiles[name]


def _summary_line(name, profile):
    flags = ", ".join(f"{label}={'yes' if profile[key] else 'no'}" for label, key in _SUMMARY_FLAGS)
    courtesy = profile["courtesy"]
    return f"`{name}`: {flags}." + (f" {courtesy}" if courtesy else "")


def partition_summary_lines() -> list[str]:
    """One deterministic line per profile, sorted by partition name."""
    return [_summary_line(name, profile) for name, profile in sorted(partition_profiles().items())]
