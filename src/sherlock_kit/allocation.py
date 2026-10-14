"""Generic, local-only worker allocation evidence with strict scheduler parsing."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sherlock_kit import control, details, executor
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context
    from sherlock_kit.slurm import Subparsers

HOST = re.compile(r"[A-Za-z0-9_.-]+")
HOSTLIST = re.compile(r"[A-Za-z0-9_.\[\],-]+")
NULL = {"", "(null)", "N/A", "None", "Unknown"}
ACTUAL_NULL = NULL - {"Unknown"}


def invalid(message: str) -> ShkError:
    """A fail-closed evidence error, never a successful partial proof."""
    return ShkError(EXIT_LOCAL, "allocation_invalid", message)


def register(subparsers: Subparsers) -> None:
    """Register a read-only explicit worker PID command."""
    parser = subparsers.add_parser("allocation", help="verify a local worker's allocation")
    parser.add_argument("--pid", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--step-id", help="numeric step within the selected physical job")
    parser.add_argument("--hostlist", action="append", default=[])
    parser.set_defaults(handler=allocation)


def process_identity(pid: int) -> tuple[int, int]:
    """Read owner UID and Linux process start ticks, refusing a dead worker."""
    try:
        directory = Path(f"/proc/{pid}")
        uid = directory.stat().st_uid
        if uid != os.getuid():
            raise invalid("worker process belongs to another UID")
        text = (directory / "stat").read_text()
        # comm can contain spaces and parentheses; fields after its final ')' start at 3.
        fields = text[text.rindex(")") + 2 :].split()
        if fields[0] in {"Z", "X", "x"}:
            raise invalid("worker process exited")
        return uid, int(fields[19])
    except (OSError, ValueError, IndexError) as exc:
        raise invalid(f"cannot verify worker process {pid}: {exc}") from exc


def strict_fields(raw: str) -> dict[str, str]:
    """Parse one scontrol record, rejecting duplicate field names and extra rows."""
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise invalid("expected exactly one scheduler record")
    matches = list(details.FIELD.finditer(raw))
    if not matches or matches[0].start() != 0:
        raise invalid("malformed scheduler fields")
    result = {}
    for i, match in enumerate(matches):
        if match[1] in result:
            raise invalid(f"duplicate scheduler field {match[1]}")
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        result[match[1]] = raw[match.end() : end].strip()
    return result


def number(value: str | None, label: str, *, positive: bool = False) -> int | None:
    """Normalize an optional actual numeric fact without inferring missing values."""
    if value is None or value in NULL:
        return None
    if not re.fullmatch(r"[0-9]+", value) or (positive and int(value) == 0):
        raise invalid(f"invalid {label}")
    return int(value)


def owner(value: str | None) -> int:
    """Accept Slurm's numeric step owner or name(uid) job owner."""
    match = re.fullmatch(r"(?:[^()\s]+\(([0-9]+)\)|([0-9]+))", value or "")
    if not match:
        raise invalid("missing or invalid scheduler owner")
    return int(match[1] or match[2])


def hashed(raw: str) -> dict[str, str]:
    """Preserve stripped raw scheduler evidence and its UTF-8 digest."""
    return {"raw": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()}


def tres_counts(value: str | None) -> tuple[int | None, int | None]:
    """Normalize actual CPU/GPU TRES, including totals plus typed GPU subsets."""
    if value is not None and value.lower() == "unknown":
        raise invalid("unknown actual allocation TRES")
    if value is None or value in ACTUAL_NULL:
        return None, None
    parsed: dict[str, str] = {}
    for entry in value.split(","):
        key, separator, count = entry.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Za-z0-9_/:.-]+", key)
            or not count
            or key in parsed
        ):
            raise invalid("malformed or duplicated allocated TRES")
        if (key == "cpu" or key == "gres/gpu" or key.startswith("gres/gpu:")) and not re.fullmatch(
            r"[0-9]+", count
        ):
            raise invalid("missing or invalid actual numeric TRES count")
        parsed[key] = count
    cpu = number(parsed.get("cpu"), "allocated CPU TRES")
    typed = [
        number(count, "allocated GPU TRES")
        for key, count in parsed.items()
        if key.startswith("gres/gpu:")
    ]
    total = number(parsed.get("gres/gpu"), "allocated GPU TRES")
    typed_total = sum(count for count in typed if count is not None)
    if total is not None and typed_total > total:
        raise invalid("typed GPU TRES exceed total")
    # A populated actual allocation without GPU entries establishes CPU-only use.
    return cpu, total if total is not None else typed_total


def gres_count(value: str) -> int:
    """Count legacy actual GRES; annotations containing commas are device metadata."""
    if value.lower() == "unknown":
        raise invalid("unknown actual allocation GRES")
    if value in ACTUAL_NULL:
        return 0
    if value.count("(") != value.count(")"):
        raise invalid("malformed allocated GRES annotation")
    cleaned = re.sub(r"\([^()]*\)", "", value)
    total = 0
    for entry in cleaned.split(","):
        parts = entry.split(":")
        if not parts[0] or any(not part for part in parts):
            raise invalid("malformed allocated GRES")
        if parts[0] == "gpu":
            count = number(parts[-1] if len(parts) > 1 else "1", "allocated GPU GRES")
            if count is None:
                raise invalid("missing allocated GPU GRES count")
            total += count
    return total


def resource_counts(fields: dict[str, str], *, step: bool) -> dict[str, int | None]:
    """Keep actual TRES and legacy resource counts separate; requests are ignored."""
    cpu_tres, gpu_tres = tres_counts(fields.get("TRES" if step else "AllocTRES"))
    cpus = number(fields.get("CPUs" if step else "NumCPUs"), "allocated CPUs")
    if cpus is not None and cpu_tres is not None and cpus != cpu_tres:
        raise invalid("allocated CPU fields disagree")
    legacy = [fields[key] for key in ("Gres", "GRES", "JOB_GRES") if key in fields]
    counts = [gres_count(value) for value in legacy if value not in ACTUAL_NULL]
    if len(set(counts)) > 1 or (counts and gpu_tres is not None and counts[0] != gpu_tres):
        raise invalid("allocated GPU fields disagree")
    gpus = gpu_tres if gpu_tres is not None else (counts[0] if counts else (0 if legacy else None))
    if gpus is None:
        raise invalid("no actual GPU or CPU-only allocation evidence")
    return {
        "allocated_cpus": cpus if cpus is not None else cpu_tres,
        "allocated_cpu_tres": cpu_tres,
        "allocated_gpus": gpus,
        **({} if step else {"allocated_gpu_tres": gpu_tres}),
    }


class Collector:
    """Bound all read-only queries to one overall deadline and validate framing."""

    def __init__(self, ctx: Context) -> None:
        """Use the already verified local executor."""
        self.ex = ctx.executor()
        self.deadline = time.monotonic() + 60
        self.hostlists: list[dict[str, Any]] = []

    def query(self, action: str, target: str) -> str:
        """Run only a fixed program operation, refusing tool failure or missing proof."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise invalid("allocation evidence deadline exceeded")
        result = executor.run_program(
            self.ex, "allocation", [action, target], timeout=min(20, remaining)
        )
        if result.status != "complete":
            raise invalid(f"allocation {action} read failed: {result.status}")
        lines = result.stdout.splitlines()
        if (
            len(lines) < 3
            or lines[-2:] != ["SHK_RC 0", "SHK_END"]
            or any(not line.startswith("SHK_DATA|") for line in lines[:-2])
        ):
            raise invalid(f"invalid or failed {action} scheduler reply")
        raw = "\n".join(line.removeprefix("SHK_DATA|") for line in lines[:-2]).strip()
        if not raw:
            raise invalid(f"missing {action} evidence")
        return raw

    def hosts(self, expression: str) -> list[str]:
        """Expand generic caller hostlists, rejecting malformed or duplicate hosts."""
        if not HOSTLIST.fullmatch(expression) or len(expression) > 4096:
            raise invalid("invalid hostlist expression")
        for record in self.hostlists:
            if record["expression"] == expression:
                return list(record["nodes"])
        nodes = self.query("hosts", expression).splitlines()
        if (
            len(nodes) > 10000
            or len(set(nodes)) != len(nodes)
            or any(not HOST.fullmatch(node) for node in nodes)
        ):
            raise invalid("malformed expanded hostlist")
        self.hostlists.append({"expression": expression, "nodes": nodes})
        return nodes


def local_node(nodes: list[str], hostname: str) -> str:
    """Require exactly one allocated node matching this host without DNS inference."""
    matches = [node for node in nodes if node.split(".")[0] == hostname.split(".")[0]]
    if len(matches) != 1:
        raise invalid("current host is outside or ambiguous in allocation")
    return matches[0]


def scheduler_record(collector: Collector, action: str, query_id: str, uid: int) -> dict[str, Any]:
    """Collect a live own allocation/step with expanded actual nodes and resources."""
    raw = collector.query(action, query_id)
    fields = strict_fields(raw)
    if owner(fields.get("UserId")) != uid:
        raise invalid("allocation belongs to another UID")
    state = fields.get("State" if action == "step" else "JobState")
    if state not in {"RUNNING", "COMPLETING"}:
        raise invalid("allocation is not live")
    partition = fields.get("Partition")
    if not partition or partition in NULL:
        raise invalid("missing allocated partition")
    expression = fields.get("NodeList") or (fields.get("Nodes") if action == "step" else None)
    if not expression:
        raise invalid("missing allocated nodes")
    nodes = collector.hosts(expression)
    node_count = number(
        fields.get("NumNodes", fields.get("Nodes") if fields.get("NodeList") else None),
        "allocated node count",
        positive=True,
    )
    if node_count is not None and node_count != len(nodes):
        raise invalid("allocated node count disagrees with hostlist")
    return {
        "query_id": query_id,
        "uid": uid,
        "state": state,
        "partition": partition,
        "nodes": nodes,
        "num_nodes": node_count,
        "fields": fields,
        **hashed(raw),
        **resource_counts(fields, step=action == "step"),
    }


def job_identity(record: dict[str, Any], query_id: str) -> None:
    """Bind logical array selection to a distinct physical allocation ID."""
    fields = record["fields"]
    physical = fields.get("JobId")
    if number(physical, "physical JobId", positive=True) is None:
        raise invalid("missing physical JobId")
    array, task = fields.get("ArrayJobId"), fields.get("ArrayTaskId")
    if (array is None) != (task is None):
        raise invalid("incomplete array identity")
    if array is not None and (
        number(array, "ArrayJobId", positive=True) is None or number(task, "ArrayTaskId") is None
    ):
        raise invalid("missing array identity")
    if query_id != physical and (array is None or query_id != f"{array}_{task}"):
        raise invalid("scheduler job identity differs from selection")
    record.update(job_id=physical, array_job_id=array, array_task_id=task)


def membership(collector: Collector, pid: int, job: str, step: str | None) -> dict[str, Any]:
    """Verify actual PID-to-job and optional numeric PID-to-step associations."""
    raw = collector.query("pidinfo", str(pid))
    lines = raw.splitlines()
    match = re.fullmatch(
        r"Slurm (?:JobId=|job id )([0-9]+)(?:\.(?:batch|extern|[0-9]+))? ends at .+", lines[0]
    )
    valid_lines = len(lines) == 1 or (
        len(lines) == 2
        and re.fullmatch(
            r"(?:slurm_get_rem_time is [0-9]+|Job remaining time is [0-9]+ seconds)", lines[1]
        )
    )
    if not valid_lines or not match or match[1] != job:
        raise invalid("worker PID is not verified in selected allocation")
    evidence: dict[str, Any] = {"job": {"source": "scontrol pidinfo", **hashed(raw)}, "step": None}
    if step is not None:
        raw = collector.query("listpids", f"{job}.{step}")
        rows = [line.split() for line in raw.splitlines()]
        if not rows or rows[0] != ["PID", "JOBID", "STEPID", "LOCALID", "GLOBALID"]:
            raise invalid("unknown process membership table")
        if any(
            len(row) != 5 or not all(re.fullmatch(r"[0-9]+", cell) for cell in row[:3])
            for row in rows[1:]
        ):
            raise invalid("malformed process membership rows")
        matches = [row for row in rows[1:] if row[0] == str(pid)]
        if len(matches) != 1 or matches[0][1:3] != [job, step]:
            raise invalid("worker PID is not verified in selected step")
        evidence["step"] = {"source": "scontrol listpids", **hashed(raw)}
    return evidence


def step_record(
    collector: Collector, job: dict[str, Any], step: str, uid: int, hostname: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Collect selected step and local node topology without SMT/resource policy."""
    identity = f"{job['job_id']}.{step}"
    record = scheduler_record(collector, "step", identity, uid)
    if record["fields"].get("StepId") != identity or record["partition"] != job["partition"]:
        raise invalid("step identity or partition differs from allocation")
    if not set(record["nodes"]).issubset(job["nodes"]):
        raise invalid("step nodes are outside parent allocation")
    for key in ("allocated_cpus", "allocated_gpus"):
        actual, parent = record[key], job[key]
        if actual is not None and parent is not None and actual > parent:
            raise invalid(f"step {key} exceeds parent allocation")
    record.update(
        step_id=step,
        tasks=number(
            record["fields"].get("Tasks", record["fields"].get("NumTasks")),
            "allocated tasks",
            positive=True,
        ),
    )
    node = local_node(record["nodes"], hostname)
    return record, [node_record(collector, node, topology=True)]


def node_record(collector: Collector, node: str, *, topology: bool) -> dict[str, Any]:
    """Retain the local node identity and require topology for a selected step."""
    raw = collector.query("node", node)
    fields = strict_fields(raw)
    if fields.get("NodeName") != node:
        raise invalid("node topology identity differs")
    if topology:
        for key in ("CPUTot", "ThreadsPerCore", "CoresPerSocket", "Sockets"):
            if number(fields.get(key), key, positive=True) is None:
                raise invalid(f"missing node topology {key}")
    return {"node": node, "fields": fields, **hashed(raw)}


def allocation(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Produce complete evidence only after stable own worker identity is verified."""
    if ctx.executor().kind != "local":
        raise ShkError(EXIT_REFUSED, "local_only", "allocation requires --executor local")
    pid = number(args.pid, "worker PID", positive=True)
    if pid is None or pid == os.getpid():
        raise invalid("an explicit worker PID is required")
    query_id = control.checked_id(args.job_id)
    step = args.step_id
    if step is not None and number(step, "numeric step id") is None:
        raise invalid("missing numeric step id")
    if len(args.hostlist) > 32:
        raise invalid("at most 32 caller hostlists")
    before = process_identity(pid)
    if before[0] != os.getuid():
        raise invalid("worker process belongs to another UID")
    try:
        initial_affinity = sorted(os.sched_getaffinity(pid))
    except (AttributeError, OSError) as exc:
        raise invalid("cannot verify worker CPU affinity") from exc
    collector = Collector(ctx)
    job = scheduler_record(collector, "job", query_id, before[0])
    job_identity(job, query_id)
    node = local_node(job["nodes"], ctx.hostname)
    record, nodes = (
        (None, [node_record(collector, node, topology=False)])
        if step is None
        else step_record(collector, job, step, before[0], ctx.hostname)
    )
    for expression in args.hostlist:
        collector.hosts(expression)
    evidence = membership(collector, pid, job["job_id"], step)
    try:
        affinity = sorted(os.sched_getaffinity(pid))
    except (AttributeError, OSError) as exc:
        raise invalid("cannot verify worker CPU affinity") from exc
    if process_identity(pid) != before or not affinity or affinity != initial_affinity:
        raise invalid("worker process identity changed during evidence collection")
    return Outcome({
        "action": "allocation",
        "executor": "local",
        "complete": True,
        "queried_at": ctx.clock().isoformat().replace("+00:00", "Z"),
        "process": {
            "pid": pid,
            "uid": before[0],
            "hostname": ctx.hostname,
            "cpu_affinity": affinity,
            "start_ticks": before[1],
            "job_membership_verified": True,
            "step_membership_verified": True if step is not None else None,
        },
        "job": job,
        "step": record,
        "node_records": nodes,
        "hostlists": collector.hostlists,
        "membership_evidence": evidence,
    })
