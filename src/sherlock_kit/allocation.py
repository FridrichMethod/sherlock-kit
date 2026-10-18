"""Generic, local-only worker allocation evidence with strict scheduler parsing."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sherlock_kit import control, details, executor
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, EXIT_UNREACHABLE, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context
    from sherlock_kit.slurm import Subparsers

HOST = re.compile(r"[A-Za-z0-9_.-]+")
HOSTLIST = re.compile(r"[A-Za-z0-9_.\[\],-]+")
NULL = {"", "(null)", "N/A", "None", "Unknown"}
ACTUAL_NULL = NULL - {"Unknown"}
# The ids Slurm prints for a record: a job is selected by its physical JobId or by
# ArrayJobId_ArrayTaskId, a step by StepId (A_T.S for an array element), a node by NodeName.
IDENTITIES: dict[str, Callable[[dict[str, str]], tuple[str | None, ...]]] = {
    "job": lambda f: (f.get("JobId"), f"{f.get('ArrayJobId')}_{f.get('ArrayTaskId')}"),
    "step": lambda f: (f.get("StepId"),),
    "node": lambda f: (f.get("NodeName"),),
}


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
    """Parse one scontrol record line, rejecting control characters and duplicate fields."""
    if not raw or any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise invalid("malformed scheduler record")
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
        if not separator or not key or not count or key in parsed:
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


def resource_counts(fields: dict[str, str], *, step: bool) -> dict[str, int | None]:
    """Actual job AllocTRES or step TRES and CPU counts; requests are ignored."""
    cpu_tres, gpus = tres_counts(fields.get("TRES" if step else "AllocTRES"))
    cpus = number(fields.get("CPUs" if step else "NumCPUs"), "allocated CPUs")
    if cpus is not None and cpu_tres is not None and cpus != cpu_tres:
        raise invalid("allocated CPU fields disagree")
    if gpus is None:
        raise invalid("no actual allocated TRES")
    return {
        "allocated_cpus": cpus if cpus is not None else cpu_tres,
        "allocated_cpu_tres": cpu_tres,
        "allocated_gpus": gpus,
        **({} if step else {"allocated_gpu_tres": gpus}),
    }


class Collector:
    """Bound all read-only queries to one overall deadline and validate framing."""

    def __init__(self, ctx: Context) -> None:
        """Use the already verified local executor and its configured command timeout."""
        self.ex = ctx.executor()
        self.deadline = time.monotonic() + self.ex.command_timeout
        self.hostlists: list[dict[str, Any]] = []

    def query(self, action: str, target: str) -> str:
        """Run one fixed read; a slow or unreachable scheduler is never non-membership."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ShkError(
                EXIT_UNREACHABLE,
                "timeout",
                "allocation: evidence deadline exceeded",
                hint="run shk doctor",
            )
        result = executor.run_program(self.ex, "allocation", [action, target], timeout=remaining)
        lines = executor.check(result, f"allocation {action}").stdout.splitlines()
        if (
            len(lines) < 2
            or lines[-1] != "SHK_END"
            or not lines[-2].startswith("SHK_RC ")
            or any(not line.startswith("SHK_DATA|") for line in lines[:-2])
        ):
            raise invalid(f"malformed {action} scheduler reply")
        code = lines[-2].removeprefix("SHK_RC ")
        if code != "0":
            stderr = result.stderr.strip()[-2000:]
            raise control.transient(f"allocation {action}", stderr) or invalid(
                f"{action} scheduler query failed (exit {code}): {stderr}"
            )
        raw = "\n".join(line.removeprefix("SHK_DATA|") for line in lines[:-2]).strip()
        if not raw:
            raise invalid(f"missing {action} evidence")
        return raw

    def hosts(self, expression: str) -> list[str]:
        """Expand a hostlist once, exactly as Slurm prints it (overlaps repeat hosts)."""
        if not HOSTLIST.fullmatch(expression):
            raise invalid("invalid hostlist expression")
        for known in self.hostlists:
            if known["expression"] == expression:
                return list(known["nodes"])
        nodes = self.query("hosts", expression).splitlines()
        if any(not HOST.fullmatch(node) for node in nodes):
            raise invalid("malformed expanded hostlist")
        self.hostlists.append({"expression": expression, "nodes": nodes})
        return nodes


def local_node(nodes: list[str], hostname: str) -> str:
    """Require exactly one allocated node matching this host without DNS inference."""
    matches = [node for node in nodes if node.split(".")[0] == hostname.split(".")[0]]
    if len(matches) != 1:
        raise invalid("current host is outside or ambiguous in allocation")
    return matches[0]


def matching_record(collector: Collector, action: str, query_id: str) -> tuple[str, dict[str, str]]:
    """The one record of a reply that Slurm identifies as query_id.

    An array's meta physical id also returns every sibling element (and their steps), so a
    reply may hold several lines; each is parsed strictly and exactly one must match.
    """
    matches = []
    for line in collector.query(action, query_id).splitlines():
        fields = strict_fields(line)
        if query_id in IDENTITIES[action](fields):
            matches.append((line, fields))
    if len(matches) != 1:
        raise invalid(f"expected exactly one matching scheduler record for {action} {query_id}")
    return matches[0]


def scheduler_record(collector: Collector, action: str, query_id: str, uid: int) -> dict[str, Any]:
    """Collect a live own allocation/step with expanded actual nodes and resources."""
    raw, fields = matching_record(collector, action, query_id)
    if owner(fields.get("UserId")) != uid:
        raise invalid("allocation belongs to another UID")
    state = fields.get("State" if action == "step" else "JobState")
    if state not in {"RUNNING", "COMPLETING"}:
        raise invalid("allocation is not live")
    partition = fields.get("Partition")
    if not partition or partition in NULL:
        raise invalid("missing allocated partition")
    expression = fields.get("NodeList")
    if expression is None or expression in NULL:
        raise invalid("missing allocated nodes")
    nodes = collector.hosts(expression)
    node_count = number(
        fields.get("Nodes" if action == "step" else "NumNodes"),
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


def job_identity(job: dict[str, Any]) -> None:
    """Keep the physical allocation ID and, for an array element, its logical identity."""
    fields = job["fields"]
    physical = fields.get("JobId")
    if number(physical, "physical JobId", positive=True) is None:
        raise invalid("missing physical JobId")
    # Slurm prints the array identity right after JobId; the same keys later on the line are
    # free text (a Comment such as "rerun of ArrayJobId=40 ArrayTaskId=7"), not this job's.
    leading = list(fields)[1:3] == ["ArrayJobId", "ArrayTaskId"]
    array, task = (fields.get("ArrayJobId"), fields.get("ArrayTaskId")) if leading else (None, None)
    if (array is None) != (task is None):
        raise invalid("incomplete array identity")
    if array is not None and (
        number(array, "ArrayJobId", positive=True) is None or number(task, "ArrayTaskId") is None
    ):
        raise invalid("missing array identity")
    job.update(job_id=physical, array_job_id=array, array_task_id=task)


def membership(collector: Collector, pid: int, job: str, step: str | None) -> dict[str, Any]:
    """Verify actual PID-to-job and optional numeric PID-to-step associations."""
    raw = collector.query("pidinfo", str(pid))
    lines = raw.splitlines()
    # Only the job id counts; a step suffix (.batch, .interactive, .0+1, ...) is not checked.
    match = re.fullmatch(r"Slurm (?:JobId=|job id )([0-9]+)(?:\.\S+)? ends at .+", lines[0])
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
    """Collect the selected step and its local node without SMT/resource policy."""
    # Slurm names an array element's step ArrayJobId_ArrayTaskId.STEP, and the meta physical
    # id's N.STEP would also return every sibling's step.
    array, task = job["array_job_id"], job["array_task_id"]
    identity = f"{array}_{task}.{step}" if array is not None else f"{job['job_id']}.{step}"
    found = scheduler_record(collector, "step", identity, uid)
    if not set(found["nodes"]).issubset(job["nodes"]):
        raise invalid("step nodes are outside parent allocation")
    for key in ("allocated_cpus", "allocated_gpus"):
        actual, parent = found[key], job[key]
        if actual is not None and parent is not None and actual > parent:
            raise invalid(f"step {key} exceeds parent allocation")
    found.update(
        step_id=step, tasks=number(found["fields"].get("Tasks"), "allocated tasks", positive=True)
    )
    node = local_node(found["nodes"], hostname)
    return found, [node_record(collector, node)]


def node_record(collector: Collector, node: str) -> dict[str, Any]:
    """Retain the local node record as Slurm prints it."""
    raw, fields = matching_record(collector, "node", node)
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
    if not all(HOSTLIST.fullmatch(expression) for expression in args.hostlist):
        raise invalid("invalid hostlist expression")
    before = process_identity(pid)
    collector = Collector(ctx)
    job = scheduler_record(collector, "job", query_id, before[0])
    job_identity(job)
    node = local_node(job["nodes"], ctx.hostname)
    step_found, nodes = (
        (None, [node_record(collector, node)])
        if step is None
        else step_record(collector, job, step, before[0], ctx.hostname)
    )
    for expression in args.hostlist:
        collector.hosts(expression)
    evidence = membership(collector, pid, job["job_id"], step)
    try:
        affinity = sorted(os.sched_getaffinity(pid))
    except OSError as exc:
        raise invalid(f"cannot read worker CPU affinity: {exc}") from exc
    # Start ticks, not the affinity a worker may change, establish that it is the same process.
    if process_identity(pid) != before:
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
        "step": step_found,
        "node_records": nodes,
        "hostlists": collector.hostlists,
        "membership_evidence": evidence,
    })
