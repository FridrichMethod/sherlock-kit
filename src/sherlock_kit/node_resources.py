"""Current Slurm configured/allocated node capacity, without OS FreeMem guesses.

With --summary the same single read is printed as sums instead of node records: the union of
the shipped nodes and one group per requested partition, each optionally split by GPU_SKU: or
CLASS: features (--by) and counting one-node slots of named shapes (--shape). A sum with
nothing known behind it is null, never 0. The kit ranks and recommends nothing.
"""

from __future__ import annotations

import argparse
import re
from typing import TYPE_CHECKING, Any

from sherlock_kit import details, executor, partitions, sites, slurm, spec
from sherlock_kit.errors import EXIT_LOCAL, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context
    from sherlock_kit.slurm import Subparsers

Shapes = dict[str, dict[str, int]]  # NAME -> {"cpus", "memory_mb", "gpus"}, in the order given


# scontrol 25.11 joins the base state and its flags with "+" (IDLE+DRAIN); any of these, in
# scontrol or short sinfo spelling, means the node takes no new work.
UNSCHEDULABLE = frozenset([
    "DOWN",
    "DRAIN",
    "DRAINED",
    "DRAINING",
    "FAIL",
    "FAILING",
    "MAINT",
    "MAINTENANCE",
    "NOT_RESPONDING",
    "RESERVED",
    "INVAL",
    "INVALID_REG",
    "FUTURE",
    "UNKNOWN",
    "REBOOT_ISSUED",
    "REBOOT_REQUESTED",
    "POWER_DOWN",
    "POWERED_DOWN",
    "POWERING_DOWN",
])
PROVENANCE = {
    "cpus": "CPUEfctv or CPUTot minus CPUAlloc",
    "memory": "CfgTRES minus AllocTRES",
    "gpus": "CfgTRES minus AllocTRES",
    "schedulable": f"State has none of {', '.join(sorted(UNSCHEDULABLE))}",
}
RULES = {
    "snapshot": "Snapshot heuristic at queried_at: configured minus allocated capacity. It does "
    "not model fairshare, QOS or partition limits, reservations, preemption or pending work, "
    "reserves nothing and predicts no start time.",
    "countable": "A node is countable when its State has none of the flags listed in "
    "provenance.schedulable and its configured and allocated CPUs, memory and GPUs are all "
    "known. free and slots count countable nodes only; configured and allocated also count "
    "unschedulable nodes. A node with a missing fact is in unknown_nodes and in no sum: beside "
    "a nonzero unknown_nodes the numbers are lower bounds. A value with nothing known behind it "
    "is null, never 0: configured and allocated in a group without a known node, free and "
    "slots in a group without a countable node unless all its nodes are known unschedulable "
    "nodes.",
    "slots": "Per countable node: the smallest of free CPUs, free memory and, when the shape "
    "asks for GPUs, free GPUs, each divided by the shape's amount and rounded down; summed over "
    "the group.",
    "overlap": "A node that lists several partitions is in each of their groups, and by_sku and "
    "by_class split the same nodes in two ways: never add partition groups or the two "
    "breakdowns together. union counts every node once. Within one breakdown a node is in "
    "exactly one entry, the one for its exact set of feature values.",
}
# A node is known when all of these are non-null, so that its three free_* values are too.
KNOWN = (
    "name",
    "state",
    "configured_cpus",
    "allocated_cpus",
    "configured_memory_mb",
    "allocated_memory_mb",
    "configured_gpus",
    "allocated_gpus",
)
CAPACITY = ("cpus", "memory_mb", "gpus")
FEATURE_PREFIX = {"sku": "GPU_SKU:", "class": "CLASS:"}  # --by value -> ActiveFeatures prefix
SHAPE = "NAME:cpus=N,mem=SIZE[,gpus=N]"
SHAPE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")
CPU_COUNT = re.compile(r"[1-9][0-9]*")
GPU_COUNT = re.compile(r"0|[1-9][0-9]*")


def register(subparsers: Subparsers) -> None:
    """Register a read-only repeated partition capacity query."""
    parser = subparsers.add_parser(
        "resources", help="Slurm configured minus allocated node capacity"
    )
    parser.add_argument("--partition", action="append", required=True, help="repeatable")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print per-partition and union sums of the same read instead of node records",
    )
    parser.add_argument(
        "--by",
        action="append",
        choices=tuple(FEATURE_PREFIX),
        help="repeatable; with --summary, also split each group by GPU_SKU: or CLASS: features",
    )
    parser.add_argument(
        "--shape",
        action="append",
        metavar=SHAPE,
        help="repeatable; with --summary, count one-node slots of this shape "
        "(mem as for submit --mem: 16G or 16000M)",
    )
    parser.set_defaults(handler=resources)


def _number(value: str | None) -> int | None:
    return int(value) if value is not None and value.isdecimal() else None


def _memory(value: str | None) -> int | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([KMGT]?)", value or "", re.I)
    if not match:
        return None
    return int(
        float(match[1]) * {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024**2}[match[2].upper()]
    )


def _difference(configured: int | None, allocated: int | None) -> int | None:
    return (
        max(0, configured - allocated) if configured is not None and allocated is not None else None
    )


def _partitions(data: dict[str, str]) -> list[str]:
    return data.get("Partitions", "").split(",")


def _features(data: dict[str, str]) -> list[str]:
    return [v for v in data.get("ActiveFeatures", "").split(",") if v not in details.MISSING]


def _node(raw: str) -> dict[str, Any]:
    data = details.fields(raw)
    configured = dict(
        part.split("=", 1) for part in data.get("CfgTRES", "").split(",") if "=" in part
    )
    allocated = dict(
        part.split("=", 1) for part in data.get("AllocTRES", "").split(",") if "=" in part
    )
    cpus = _number(data.get("CPUEfctv", data.get("CPUTot")))
    used_cpus = _number(data.get("CPUAlloc"))
    mem = _memory(configured.get("mem"))
    used_mem = _memory(allocated.get("mem"))
    if "AllocTRES" in data and data["AllocTRES"] == "":
        used_mem = 0
    gpu = slurm.gpus(data["CfgTRES"]) if data.get("CfgTRES") else None
    used_gpu = slurm.gpus(data["AllocTRES"]) if "AllocTRES" in data else None
    state = data.get("State") or None
    return {
        "name": data.get("NodeName"),
        "state": state,
        "schedulable": UNSCHEDULABLE.isdisjoint(state.split("+")) if state else None,
        "partitions": _partitions(data),
        "features": _features(data),
        "free_cpus": _difference(cpus, used_cpus),
        "free_memory_mb": _difference(mem, used_mem),
        "free_gpus": _difference(gpu, used_gpu),
        "configured_cpus": cpus,
        "allocated_cpus": used_cpus,
        "configured_memory_mb": mem,
        "allocated_memory_mb": used_mem,
        "configured_gpus": gpu,
        "allocated_gpus": used_gpu,
        "configured_tres": data.get("CfgTRES"),
        "allocated_tres": data.get("AllocTRES"),
        "raw": raw,
    }


def _unknown(raw: str) -> dict[str, Any]:
    """A malformed line in summary mode: its partitions and features, every other fact null."""
    data = details.fields(raw)
    return {
        **dict.fromkeys(KNOWN),
        "schedulable": None,
        "partitions": _partitions(data),
        "features": _features(data),
    }


def _shape(text: str) -> tuple[str, dict[str, int]]:
    """One --shape NAME:cpus=N,mem=SIZE[,gpus=N], keys in any order; gpus defaults to 0."""

    def invalid(problem: str) -> ShkError:
        return ShkError(
            EXIT_LOCAL,
            "spec_invalid",
            f"--shape {text!r}: {problem}",
            hint=f"use {SHAPE}, for example gpu1:cpus=8,mem=64G,gpus=1",
        )

    name, colon, body = text.partition(":")
    if not colon or not SHAPE_NAME.fullmatch(name):
        raise invalid(f"NAME must match {SHAPE_NAME.pattern} and end at the first ':'")
    values: dict[str, str] = {}
    for item in body.split(","):
        key, equals, value = item.partition("=")
        if not equals or key not in ("cpus", "mem", "gpus"):
            raise invalid(f"{item!r} is not cpus=N, mem=SIZE or gpus=N")
        if key in values:
            raise invalid(f"repeated key {key!r}")
        values[key] = value
    if missing := [key for key in ("cpus", "mem") if key not in values]:
        raise invalid(f"missing {' and '.join(missing)}")
    if not CPU_COUNT.fullmatch(values["cpus"]):
        raise invalid(f"cpus {values['cpus']!r}: use a whole number above 0")
    try:
        memory = spec.memory_mb(values["mem"])
    except ShkError:
        raise invalid(f"mem {values['mem']!r}: use 16G or 16000M (G = 1024 M)") from None
    if not GPU_COUNT.fullmatch(gpus := values.get("gpus", "0")):
        raise invalid(f"gpus {gpus!r}: use 0 or a whole number above 0")
    return name, {"cpus": int(values["cpus"]), "memory_mb": memory, "gpus": int(gpus)}


def _shapes(args: argparse.Namespace) -> Shapes:
    """The checked --shape values, offline; --by and --shape need --summary (exit 1)."""
    given = [flag for flag, value in (("--by", args.by), ("--shape", args.shape)) if value]
    if given and not args.summary:
        raise ShkError(
            EXIT_LOCAL,
            "spec_invalid",
            f"{' and '.join(given)}: only with --summary",
            hint="add --summary, or drop the option for the node records",
        )
    shapes: Shapes = {}
    for text in args.shape or ():
        name, shape = _shape(text)
        if name in shapes:
            raise ShkError(
                EXIT_LOCAL,
                "spec_invalid",
                f"--shape {text!r}: NAME {name!r} repeated",
                hint="give each --shape its own NAME",
            )
        shapes[name] = shape
    return shapes


def _slots(node: dict[str, Any], shape: dict[str, int]) -> int:
    fits: list[int] = [
        node["free_cpus"] // shape["cpus"],
        node["free_memory_mb"] // shape["memory_mb"],
    ]
    if shape["gpus"]:
        fits.append(node["free_gpus"] // shape["gpus"])
    return min(fits)


def _group(nodes: list[dict[str, Any]], shapes: Shapes) -> dict[str, Any]:
    """Node counts, capacity sums and slots of some nodes (see RULES["countable"])."""
    known = [node for node in nodes if all(node[key] is not None for key in KNOWN)]
    countable = [node for node in known if node["schedulable"]]
    # Without a countable node, free and slots are a known 0 only when every node is known.
    free_known = bool(countable) or (bool(nodes) and len(known) == len(nodes))
    return {
        "nodes": len(nodes),
        "schedulable_nodes": sum(1 for node in nodes if node["schedulable"]),
        "countable_nodes": len(countable),
        "unschedulable_nodes": len(known) - len(countable),
        "unknown_nodes": len(nodes) - len(known),
        **{
            kind: {
                "configured": sum(node[f"configured_{kind}"] for node in known) if known else None,
                "allocated": sum(node[f"allocated_{kind}"] for node in known) if known else None,
                "free": sum(node[f"free_{kind}"] for node in countable) if free_known else None,
            }
            for kind in CAPACITY
        },
        "slots": {
            name: sum(_slots(node, shape) for node in countable) if free_known else None
            for name, shape in shapes.items()
        },
    }


def _breakdown(nodes: list[dict[str, Any]], kind: str, shapes: Shapes) -> list[dict[str, Any]]:
    """One entry per exact sorted set of feature values after the prefix, sorted by that set."""
    prefix = FEATURE_PREFIX[kind]
    split: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for node in nodes:
        values = {f.removeprefix(prefix) for f in node["features"] if f.startswith(prefix)}
        split.setdefault(tuple(sorted(values)), []).append(node)
    return [{kind: list(key), **_group(split[key], shapes)} for key in sorted(split)]


def _grouped(nodes: list[dict[str, Any]], by: list[str], shapes: Shapes) -> dict[str, Any]:
    by_kind = {f"by_{kind}": _breakdown(nodes, kind, shapes) for kind in by}
    return {**_group(nodes, shapes), **by_kind}


def _summary(
    nodes: list[dict[str, Any]], requested: list[str], by: list[str], shapes: Shapes
) -> dict[str, Any]:
    """The summary object: every shipped node once in union, and one group per partition."""
    return {
        "rules": RULES,
        "shapes": shapes,
        "by": by,
        "overlapping_nodes": sum(
            1 for node in nodes if len(set(requested).intersection(node["partitions"])) > 1
        ),
        "union": _grouped(nodes, by, shapes),
        "partitions": {
            name: _grouped([node for node in nodes if name in node["partitions"]], by, shapes)
            for name in requested
        },
    }


def resources(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Read the partitions' Slurm node facts once; a failed read is exit 1, missing facts null.

    Each --partition must be listed in the selected site's table.
    """
    wanted = {partitions.profile(name, ctx.site).name for name in args.partition}
    shapes = _shapes(args)
    program = executor.run_program(ctx.executor(), "resources", sorted(wanted))
    result = executor.check(program, "resources")
    lines = executor.records(result.stdout)
    rc = next((line.split()[-1] for line in lines if line.startswith("SHK_RC scontrol ")), None)
    if "SHK_END" not in lines or rc != "0":
        raise ShkError(
            EXIT_LOCAL,
            "malformed_read",
            f"resources incomplete: end={'SHK_END' in lines}, scontrol rc={rc}. "
            f"{result.stderr.strip()[-2000:]}".strip(),
            hint=f"retry once after 60 s; {sites.command(result.site)} doctor --remote checks "
            "the remote tools",
        )
    complete = True
    nodes, malformed = [], []
    for line in lines:
        if not line.startswith("SHK_N|"):
            continue
        raw = line.removeprefix("SHK_N|")
        try:
            node = _node(raw)
            nodes.append(node)
            complete = complete and all(
                node[key] is not None
                for key in ("name", "state", "free_cpus", "free_memory_mb", "free_gpus")
            )
        except ValueError:
            malformed.append(raw)
            complete = False
    reply: dict[str, Any] = {
        "complete": bool(complete and nodes),
        "malformed": malformed,
        "executor": ctx.executor().kind,
        **sites.reply_fields(result.site),
        **slurm._cadence(ctx),
        "provenance": PROVENANCE,
    }
    if not args.summary:
        return Outcome({**reply, "nodes": nodes})
    shipped = nodes + [_unknown(raw) for raw in malformed]  # a malformed line is an unknown node
    requested = list(dict.fromkeys(args.partition))
    by = list(dict.fromkeys(args.by or ()))
    return Outcome({**reply, "summary": _summary(shipped, requested, by, shapes)})
