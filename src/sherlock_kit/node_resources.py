"""Current Slurm configured/allocated node capacity, without OS FreeMem guesses."""

from __future__ import annotations

import argparse
import re
from typing import TYPE_CHECKING, Any

from sherlock_kit import details, executor, partitions, slurm
from sherlock_kit.errors import EXIT_LOCAL, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context
    from sherlock_kit.slurm import Subparsers


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


def register(subparsers: Subparsers) -> None:
    """Register a read-only repeated partition capacity query."""
    parser = subparsers.add_parser(
        "resources", help="Slurm configured minus allocated node capacity"
    )
    parser.add_argument("--partition", action="append", required=True)
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
        "partitions": data.get("Partitions", "").split(","),
        "features": [
            v for v in data.get("ActiveFeatures", "").split(",") if v not in details.MISSING
        ],
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


def resources(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Read the partitions' Slurm node facts once; a failed read is exit 1, missing facts null."""
    wanted = {partitions.profile(name).name for name in args.partition}
    program = executor.run_program(ctx.executor(), "resources", sorted(wanted))
    result = executor.check(program, "resources")
    lines = result.stdout.splitlines()
    rc = next((line.split()[-1] for line in lines if line.startswith("SHK_RC scontrol ")), None)
    if "SHK_END" not in lines or rc != "0":
        raise ShkError(
            EXIT_LOCAL,
            "malformed_read",
            f"resources incomplete: end={'SHK_END' in lines}, scontrol rc={rc}. "
            f"{result.stderr.strip()[-2000:]}".strip(),
            hint="retry once after 60 s; shk doctor --remote checks the remote tools",
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
    return Outcome({
        "complete": bool(complete and nodes),
        "malformed": malformed,
        "executor": ctx.executor().kind,
        **slurm._cadence(ctx),
        "provenance": PROVENANCE,
        "nodes": nodes,
    })
