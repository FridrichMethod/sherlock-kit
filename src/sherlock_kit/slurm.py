"""shk submit, status, cancel and occupancy, with Slurm as the only record.

Status makes one read (data/remote/query.sh: one squeue and one sacct for all selected names;
details.py for --details and --job-id), as do submit (unless --dry-run or --preflight) and
occupancy (one squeue per partition); submit adds one mutation (submit.sh, the script on
stdin) and cancel is only one mutation (cancel.sh: scancel -u USER --name=NAME). resolve_name() is pure: one name's
rows grouped by base job id give new | live | done | failed | stale. live refuses even with
--resubmit, done and failed need --resubmit REASON, stale (spec files, no rows) submits
like new. Remote calls print next_check_after (now + 60 s); a dispatched mutation without a
confirmed result (a lost reply, sbatch without a job id, a nonzero scancel) is action "unknown"
(exit 5); exit 6 when the newest job of a listed name has an anomaly. A site other than Sherlock
adds sites.reply_fields() after "executor", and its selector to the exit 5 `next` and to the shk
commands that hints name. Partitions are those of the selected site's table. A site with
test_before_submit (Marlowe) runs control.preflight (sbatch --test-only) after the duplicate read
and before staging: when Slurm refuses, submit returns that preflight reply unchanged (exit 3,
nothing staged), else it goes on as usual. status_reader() checks a status selection once and
returns its read, which wait repeats.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shlex
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeAlias

from sherlock_kit import control, details, node_resources, partitions, sites, spec
from sherlock_kit import executor as executors
from sherlock_kit.errors import (
    EXIT_ANOMALY,
    EXIT_LOCAL,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_UNKNOWN,
    Outcome,
    ShkError,
)

if TYPE_CHECKING:
    from sherlock_kit.cli import Context

Subparsers: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"

JOB_ID = re.compile(
    r"([1-9][0-9]*)"  # base job id
    r"(?:_(?:([0-9]+)"  # N_k: one array task
    r"|\[([0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*)(?:%[0-9]+)?\]))?"  # N_[a-b,c%t]: aggregate
)
QUEUE_FIELDS = ("JobID", "State", "Partition", "JobName")  # query.sh squeue -o
ACCT_FIELDS = (
    "JobID",
    "State",
    "Partition",
    "ElapsedRaw",
    "AllocCPUS",
    "AllocTRES",
    "Submit",
    "Restarts",
    "ExitCode",
    "JobName",
)  # query.sh sacct --format; JobName stays last
OCCUPANCY_COLUMNS = (("UserName", 64), ("State", 24), ("tres-alloc", 128))  # squeue -O, live layout
TERMINAL = frozenset({
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "NODE_FAIL",
    "OUT_OF_MEMORY",
    "BOOT_FAIL",
    "DEADLINE",
    "PREEMPTED",
    "REVOKED",
})
RUNNING = frozenset({
    "RUNNING",
    "COMPLETING",
    "SUSPENDED",
    "CONFIGURING",
    "STAGE_OUT",
    "SIGNALING",
    "RESIZING",
    "STOPPED",
})
MAX_ROWS = 50_000
CADENCE = timedelta(seconds=60)
SINCE_DAYS, SINCE_DAYS_ALL = 30, 7
MAX_SINCE_DAYS = 31  # Sherlock's slurmdbd refuses wider sacct windows ("Too wide of a date range")
# Advisory thresholds of a borrowed partition; a budget only where its courtesy text states them.
BUDGET_JOBS, BUDGET_MINUTES = 2, 120


@dataclass(frozen=True)
class Refusal:
    """The error token and hint of a remote marker printed before dispatch: nothing ran.

    {shk} in the hint stands for the site's command prefix (sites.command).
    """

    token: str
    hint: str


REFUSALS = {  # printed before sbatch, scancel or scontrol is dispatched (exit 3)
    "SHK_LIVE": Refusal("live", "wait; check {shk} status at most once per 60 s"),
    "SHK_REFUSED": Refusal("remote_refused", "nothing ran; fix the cause, then retry once"),
}


# ---------------------------------------------------------------- parsing the read


@dataclass(frozen=True)
class SpecFile:
    """The newest <name>.<jobid>.json in a run directory (SHK_SPEC)."""

    job_id: int
    task_count: int | None  # 1 for a single job; None when the file cannot be read
    partitions: tuple[str, ...] = ()  # resources.partitions of a set; () otherwise


@dataclass(frozen=True)
class Read:
    """One complete query.sh reply."""

    user: str | None
    host: str | None
    queue: list[str]  # squeue rows (SHK_Q)
    acct: list[str]  # sacct rows (SHK_A)
    occupancy: dict[str, list[str]]  # partition -> fixed-width squeue -O rows (SHK_O)
    specs: dict[str, SpecFile]  # name -> newest spec file (SHK_SPEC)
    spec_files: dict[str, int]  # name -> spec files in its run directory (SHK_S)


def _frozen_resources(text: str) -> dict[str, Any]:
    """The resources of a frozen spec file; {} for a truncated, hand-edited or foreign one."""
    try:
        found = json.loads(text)["resources"]
    except (ValueError, KeyError, TypeError):
        return {}
    return found if isinstance(found, dict) else {}


def _task_count(resources: dict[str, Any]) -> int | None:
    try:
        array = resources["array"]
        return 1 if array is None else int(array["count"])
    except (ValueError, KeyError, TypeError):
        return None


def _partitions(resources: dict[str, Any]) -> tuple[str, ...]:
    """The members frozen in resources.partitions by a submission to a set, else ()."""
    listed = resources.get("partitions")
    if isinstance(listed, list) and all(isinstance(name, str) for name in listed):
        return tuple(listed)
    return ()


def _grouped(records: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for record in records:
        key, _, rest = record.partition("|")
        grouped.setdefault(key, []).append(rest)
    return grouped


def parse_read(stdout: str, tools: Iterable[str], site: sites.Site = sites.SHERLOCK) -> Read:
    """A query.sh reply; complete only with SHK_END and SHK_RC 0 from every tool (else exit 1)."""
    records: dict[str, list[str]] = {
        "SHK_Q": [],
        "SHK_A": [],
        "SHK_O": [],
        "SHK_S": [],
        "SHK_SPEC": [],
    }
    facts: dict[str, str] = {}
    for line in executors.records(stdout):
        kind, separator, rest = line.partition("|")
        if separator and kind in records:
            records[kind].append(rest)
        elif line.startswith("SHK_"):
            key, _, value = line.rpartition(" ")
            facts[key or value] = value
    codes = {tool: facts.get(f"SHK_RC {tool}") for tool in tools}
    if "SHK_END" not in facts or any(code != "0" for code in codes.values()):
        raise ShkError(
            EXIT_LOCAL,
            "malformed_read",
            f"query incomplete: end={'SHK_END' in facts}, rc={codes}",
            hint=f"retry once after 60 s; {sites.command(site)} doctor --remote checks the "
            "remote tools",
        )
    specs = {}
    for name, found in _grouped(records["SHK_SPEC"]).items():
        job, _, text = found[-1].partition("|")
        resources = _frozen_resources(text)
        specs[name] = SpecFile(int(job), _task_count(resources), _partitions(resources))
    counts = {name: int(found[-1]) for name, found in _grouped(records["SHK_S"]).items()}
    return Read(
        user=facts.get("SHK_USER"),
        host=facts.get("SHK_HOST"),
        queue=records["SHK_Q"],
        acct=records["SHK_A"],
        occupancy=_grouped(records["SHK_O"]),
        specs=specs,
        spec_files=counts,
    )


# ---------------------------------------------------------------- squeue and sacct rows


def _indices(aggregate: str) -> list[int]:
    indices: set[int] = set()
    for part in aggregate.split(","):
        first, _, last = part.partition("-")
        low, high = int(first), int(last or first)
        indices.update(range(low, high + 1))
    return sorted(indices)


def job_id(text: str) -> tuple[int, int | None, list[int] | None]:
    """(base, task, indices) of N, N_k or an aggregate N_[a-b,c%t]; ValueError when malformed."""
    match = JOB_ID.fullmatch(text)
    if match is None:
        raise ValueError(f"job id {text!r}")
    task = None if match[2] is None else int(match[2])
    return int(match[1]), task, None if match[3] is None else _indices(match[3])


def gpus(tres: str) -> int:
    """GPUs in a TRES string: gres/gpu=N is the total and gres/gpu:TYPE=N its typed share."""
    generic, typed = 0, 0
    for key, _, value in (part.partition("=") for part in tres.split(",")):
        if key == "gres/gpu" or key.startswith("gres/gpu:"):
            if not value.isdecimal():
                raise ValueError(f"TRES {tres!r}")
            if key == "gres/gpu":
                generic += int(value)
            else:
                typed += int(value)
    return max(generic, typed)


@dataclass(frozen=True)
class Row:
    """One squeue or sacct line of a shk job."""

    key: tuple[Any, ...]  # from sacct, JobID, Restarts, Cluster, DBIndex, JobIDRaw, Start, End
    base: int  # the base job id (the array id for array tasks)
    tasks: tuple[int, ...]  # task indices covered; an aggregate covers several
    from_acct: bool
    explicit: bool  # N or N_k; False for an aggregate N_[...], which never ran
    array: bool
    state: str
    partition: str
    restart: int
    cpu_seconds: int
    gpu_seconds: int
    exit_code: str  # sacct only ("" for squeue rows)
    submitted: str  # sacct only, UTC
    started: str
    ended: str
    # An explicit row that shows a start: sacct AllocCPUS above 0 unless PENDING, or squeue RUNNING.
    ran: bool = False


def _row(fields: dict[str, str]) -> Row:
    base, task, indices = job_id(fields["JobID"])
    words = fields["State"].split()  # "CANCELLED by 1234", "COMPLETED+"
    if not words:
        raise ValueError(f"row without a state: {fields}")
    elapsed, cpus, restart = (
        int(fields.get(key, "0")) for key in ("ElapsedRaw", "AllocCPUS", "Restarts")
    )
    explicit = indices is None
    state = words[0].rstrip("+")
    # sacct: a pending row has not started, whatever TRES it keeps; squeue: a running state.
    acct_start = cpus > 0 and state != "PENDING"
    shows_start = acct_start if "Restarts" in fields else state in RUNNING
    return Row(
        key=(
            "Restarts" in fields,
            fields["JobID"],
            restart,
            *(fields.get(key) for key in ("Cluster", "DBIndex", "JobIDRaw", "Start", "End")),
        ),
        base=base,
        tasks=tuple(indices) if indices is not None else (task or 0,),
        from_acct="Restarts" in fields,
        explicit=explicit,
        array="_" in fields["JobID"],
        state=state,
        partition=fields["Partition"],
        restart=restart,
        cpu_seconds=elapsed * cpus if explicit else 0,
        gpu_seconds=elapsed * gpus(fields.get("AllocTRES", "")) if explicit else 0,
        exit_code=fields.get("ExitCode", ""),
        submitted=fields.get("Submit", ""),
        started=fields.get("Start", ""),
        ended=fields.get("End", ""),
        ran=explicit and shows_start,
    )


@dataclass(frozen=True)
class Rows:
    """Parsed rows by job name, malformed line counts and whether sacct was cut off."""

    by_name: dict[str, list[Row]]
    malformed: Counter[str | None]  # malformed lines per name; None when no name was readable
    truncated: bool  # sacct printed more than MAX_ROWS rows


def parse_rows(queue: list[str], acct: list[str], keep: Callable[[str], bool]) -> Rows:
    """Rows of the job names keep() accepts; at most MAX_ROWS sacct rows are read."""
    by_name: dict[str, list[Row]] = {}
    malformed: Counter[str | None] = Counter()
    for fields, lines in ((QUEUE_FIELDS, queue), (ACCT_FIELDS, acct[:MAX_ROWS])):
        for line in lines:
            values = line.split("|", len(fields) - 1)  # JobName is last and may contain "|"
            if len(values) != len(fields):
                malformed[None] += 1
                continue
            name = values[-1]
            if not keep(name):
                continue
            found = by_name.setdefault(name, [])  # listed even when none of its rows parse
            try:
                found.append(_row(dict(zip(fields, values, strict=True))))
            except ValueError:
                malformed[name] += 1
    return Rows(by_name, malformed, truncated=len(acct) > MAX_ROWS)


# ---------------------------------------------------------------- resolution (pure)


def _kind(state: str) -> str:
    if state == "COMPLETED":
        return "completed"
    if state in TERMINAL:
        return "failed"
    if state in RUNNING:
        return "running"
    return "pending"  # PENDING, REQUEUED, REQUEUE_HOLD, ...: not running yet


@dataclass(frozen=True)
class Job:
    """One base job id under a name."""

    job_id: str
    state: str  # live | done | failed
    submitted: str | None
    partition: str
    array: dict[str, Any] | None
    tasks: dict[str, Any]
    exit_codes: dict[str, int]
    max_restart: int
    interrupted: frozenset[str]  # partition texts of preempted attempts
    cost: dict[str, Any]
    partition_texts: frozenset[str]  # the partition text of every row: one name or a list

    def current(self, anomalies: list[str], ran_on: list[str] | None) -> dict[str, Any]:
        """The status entry of the newest job of a name; ran_on follows partition when given."""
        entry: dict[str, Any] = {"job_id": self.job_id, "partition": self.partition}
        if ran_on is not None:
            entry["ran_on"] = ran_on
        return {
            **entry,
            "array": self.array,
            "tasks": self.tasks,
            "exit_codes": self.exit_codes,
            "max_restart": self.max_restart,
            "cost": self.cost,
            "anomalies": anomalies,
        }

    def history(self) -> dict[str, Any]:
        """The status entry of an older job of a name."""
        return {"job_id": self.job_id, "state": self.state, "submitted": self.submitted}


def _deciding_rows(rows: list[Row]) -> tuple[dict[int, Row], dict[int, Row], dict[int, set[int]]]:
    """Per task: the row that decides its state, its newest sacct row and the restarts seen.

    squeue wins over sacct, then the later submission, then an explicit row over an aggregate
    (a pending remainder whose Start is Unknown), then the later start, restart and end.
    """
    deciding: dict[int, Row] = {}
    accounted: dict[int, Row] = {}
    restarts: dict[int, set[int]] = {}
    for row in sorted(
        rows,
        key=lambda r: (not r.from_acct, r.submitted, r.explicit, r.started, r.restart, r.ended),
    ):
        for index in row.tasks:
            deciding[index] = row
            if row.from_acct:
                accounted[index] = row
                restarts.setdefault(index, set()).add(row.restart)
    return deciding, accounted, restarts


def _task_summary(
    deciding: dict[int, Row], restarts: dict[int, set[int]], count: int | None
) -> dict[str, Any]:
    kinds = Counter(_kind(row.state) for row in deciding.values())
    gaps = [index for index, seen in sorted(restarts.items()) if seen != set(range(max(seen) + 1))]
    return {
        "count": count,
        "completed": kinds["completed"],
        "failed": kinds["failed"],
        "running": kinds["running"],
        "pending": kinds["pending"],
        "missing": None if count is None else count - len(deciding),
        "by_state": dict(sorted(Counter(row.state for row in deciding.values()).items())),
        "incomplete_history": gaps[:20],
    }


def _final(deciding: dict[int, Row], accounted: dict[int, Row], count: int | None) -> bool:
    """Whether sacct holds every task in the terminal state that decides it.

    Only then is the cost final: squeue can show the end before slurmdbd has the row. An
    unknown task count never proves that every task was seen.
    """
    return count is not None and all(
        index in accounted
        and accounted[index].state in TERMINAL
        and accounted[index].state == deciding[index].state
        for index in range(count)
    )


def _interrupted(rows: list[Row]) -> frozenset[str]:
    """Partitions on which an attempt was preempted: the partition texts of PREEMPTED rows.

    sacct --duplicates keeps each superseded attempt with the partition it ran on, which for a
    job submitted to several partitions can differ from the next attempt's. A NODE_FAIL or
    REQUEUED attempt and a restart whose earlier attempt is not visible do not count.
    """
    return frozenset(row.partition for row in rows if row.state == "PREEMPTED")


def _job(rows: list[Row], known_count: int | None) -> Job:
    """Summary of one base job; known_count is the task count from its spec file, if any.

    Without it an array's count is inferred only when the seen tasks are exactly 0..max (shk's
    own layout). A 1-based or stepped legacy array, or one whose early tasks left the sacct
    window, has an unknown count: its state comes from the seen tasks, nothing is missing
    and the cost is not final.
    """
    deciding, accounted, restarts = _deciding_rows(rows)
    single = not any(row.array for row in rows)
    width = max(deciding) + 1
    if single:
        count: int | None = 1
    elif known_count:
        count = max(known_count, width)
    else:
        count = width if len(deciding) == width else None
    tasks = _task_summary(deciding, restarts, count)
    if tasks["running"] or tasks["pending"]:
        state = "live"
    elif tasks["completed"] == (len(deciding) if count is None else count):
        state = "done"
    else:
        state = "failed"
    exits = Counter(row.exit_code for row in accounted.values() if row.state in TERMINAL)
    submits = [row.submitted for row in rows if row.from_acct and "T" in row.submitted]
    return Job(
        job_id=str(rows[0].base),
        state=state,
        submitted=f"{min(submits)}Z" if submits else None,  # sacct prints UTC (TZ=UTC)
        partition=rows[0].partition,
        array=None if single else {"count": count, "count_known": (known_count or 0) >= width},
        tasks=tasks,
        exit_codes=dict(sorted(exits.items())),
        max_restart=max(row.restart for row in rows),
        interrupted=_interrupted(rows),
        cost={
            "cpu_seconds": sum(row.cpu_seconds for row in rows),
            "gpu_seconds": sum(row.gpu_seconds for row in rows),
            "final": not tasks["incomplete_history"] and _final(deciding, accounted, count),
        },
        partition_texts=frozenset(row.partition for row in rows),
    )


def _ran_on(rows: list[Row]) -> list[str]:
    """Sorted distinct partitions that a task or attempt of one job started in.

    Only a started row whose partition text is one name counts: Slurm prints the partition a
    job runs in once it started, and a list (or one arbitrary member) while it has not.
    """
    return sorted({
        row.partition for row in rows if row.ran and row.partition and "," not in row.partition
    })


def _non_preemptible(text: str, profiles: Mapping[str, partitions.Profile]) -> bool:
    """A partition text, one name or a list, whose members are all packaged and not preemptible."""
    return all(name in profiles and not profiles[name].preemptible for name in text.split(","))


def _anomalies(
    job: Job, live_jobs: int, malformed: int, invariants: bool, site: sites.Site
) -> list[str]:
    """invariants: shk names, where one name is one task on partitions the site's table lists.

    A PREEMPTED attempt is unexpected on a packaged non-preemptible partition, or on a list whose
    members are all such; a NODE_FAIL or REQUEUED attempt and a restart are not anomalies (they
    show in max_restart and tasks). A partition list on any row of the job is split on ","
    before the lookup.
    """
    profiles = partitions.profiles(site)
    found = []
    if invariants and live_jobs > 1:
        found.append("duplicate_live")
    if malformed:
        found.append("malformed_rows")
    if invariants and any(
        name not in profiles for text in job.partition_texts for name in text.split(",")
    ):
        found.append("unknown_partition")
    if any(_non_preemptible(text, profiles) for text in job.interrupted):
        found.append("unexpected_preemption")
    return sorted(found)


def resolve_name(
    rows: list[Row],
    spec_files: int = 0,
    newest_spec: SpecFile | None = None,
    malformed: int = 0,
    invariants: bool = True,
    site: sites.Site = sites.SHERLOCK,
) -> dict[str, Any]:
    """Pure: one name's rows give {state, current, history, spec_files}.

    The largest base id is the current attempt, and any live job makes the name live.
    Without invariants (a legacy name) duplicate_live and unknown_partition do not apply.
    current.ran_on appears only when newest_spec is the current job's and froze a partition set.
    The anomalies read the site's partition table.
    """
    jobs: dict[int, list[Row]] = {}
    for row in {row.key: row for row in rows}.values():
        jobs.setdefault(row.base, []).append(row)
    if not jobs:
        state = "stale" if spec_files else "new"
        return {"state": state, "current": None, "history": [], "spec_files": spec_files}
    summaries = {}
    for base, found in sorted(jobs.items()):
        known = newest_spec.task_count if newest_spec and newest_spec.job_id == base else None
        summaries[base] = _job(found, known)
    base = max(summaries)
    newest = summaries[base]
    live = [job for job in summaries.values() if job.state == "live"]
    in_set = (
        newest_spec is not None and newest_spec.job_id == base and len(newest_spec.partitions) > 1
    )
    ran_on = _ran_on(jobs[base]) if in_set else None
    anomalies = _anomalies(newest, len(live), malformed, invariants, site)
    return {
        "state": "live" if live else newest.state,
        "current": newest.current(anomalies, ran_on),
        "history": [job.history() for job in summaries.values() if job is not newest],
        "spec_files": spec_files,
    }


def _resolve(read: Read, rows: Rows, name: str, site: sites.Site) -> dict[str, Any]:
    """resolve_name() for one listed name, or exit 1 when Slurm has rows of it but none parse.

    Such a name is neither new nor anything shk can show (for example hetjob ids N+k).
    """
    found = rows.by_name.get(name, [])
    if rows.malformed[name] and not found:
        raise ShkError(
            EXIT_LOCAL,
            "malformed_read",
            f"{name}: none of its {rows.malformed[name]} Slurm rows could be read",
            hint="shk cannot tell whether it is new, live or finished; "
            "look once with sacct --name and report it",
        )
    malformed = rows.malformed[name] + rows.malformed[None]
    spec_files, newest = read.spec_files.get(name, 0), read.specs.get(name)
    return resolve_name(found, spec_files, newest, malformed, site=site)


# ---------------------------------------------------------------- occupancy


def _counts() -> dict[str, int]:
    return {"running_jobs": 0, "running_gpus": 0, "pending_jobs": 0}


def _columns(line: str) -> dict[str, str]:
    columns, offset = {}, 0
    for name, width in OCCUPANCY_COLUMNS:
        columns[name] = line[offset : offset + width].strip()
        offset += width
    return columns


def occupancy_summary(rows: list[str]) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """({user: counts}, totals) of running jobs (with their GPUs) and pending jobs.

    The rows are fixed-width because tres-alloc is empty for pending jobs.
    """
    users: dict[str, dict[str, int]] = {}
    totals = _counts()
    for line in rows:
        columns = _columns(line)
        if columns["State"] in ("RUNNING", "COMPLETING"):
            try:
                delta = {"running_jobs": 1, "running_gpus": gpus(columns["tres-alloc"])}
            except ValueError as exc:
                raise ShkError(EXIT_LOCAL, "malformed_read", f"occupancy row: {exc}") from None
        elif columns["State"] == "PENDING":
            delta = {"pending_jobs": 1}
        else:
            continue
        for counts in (users.setdefault(columns["UserName"], _counts()), totals):
            for key, value in delta.items():
                counts[key] += value
    return users, totals


# ---------------------------------------------------------------- command line


def _register_submit(subparsers: Subparsers) -> None:
    parser = subparsers.add_parser(
        "submit", help="submit one logical task (refuses live; done or failed need --resubmit)"
    )
    parser.add_argument("--task", required=True)
    parser.add_argument("--campaign", default="default")
    parser.add_argument("--script", required=True, help="job script, at most 1 MiB")
    parser.add_argument(
        "--partition",
        required=True,
        help="one partition, or compatible partitions as one value P,P; "
        "a repeated flag is not a list (the last one wins)",
    )
    parser.add_argument("--cpus", type=int, required=True, help="CPUs per task")
    parser.add_argument(
        "--cpu-thread-budget",
        type=int,
        nargs="?",
        const=1,
        metavar="N",
        help="opt-in host/container numerical thread defaults per task (default 1, at most --cpus)",
    )
    parser.add_argument("--mem", required=True, help="16G or 16000M")
    parser.add_argument("--time", required=True, help="2h, 90m or H:MM:SS")
    parser.add_argument("--gpus", type=int, default=0)
    parser.add_argument("--tasks", type=int, default=1)
    parser.add_argument("--array", metavar="N[%M]", help="N tasks (2-1000), at most M at once")
    parser.add_argument("--constraint")
    parser.add_argument("--prefer", help="preferred node features (Slurm soft constraint)")
    parser.add_argument("--signal", help="for example B:USR1@60")
    parser.add_argument(
        "--after-ok",
        action="extend",
        nargs="+",
        default=[],
        metavar="JOBID",
        help="start after these jobs complete; repeatable",
    )
    parser.add_argument("--run-dir", help="default <remote_root>/runs/<name>")
    parser.add_argument(
        "--courtesy-ack",
        action="store_true",
        help="required on borrowed partitions, after shk occupancy",
    )
    parser.add_argument("--resubmit", metavar="REASON", help="required after done or failed")
    preview = parser.add_mutually_exclusive_group()
    preview.add_argument(
        "--dry-run", action="store_true", help="offline: print the spec and sbatch argv"
    )
    preview.add_argument(
        "--preflight",
        action="store_true",
        help="read-only sbatch --test-only, without staging provenance",
    )
    parser.add_argument(
        "--submission-token", help="public correlation token stored in Slurm Comment"
    )
    parser.add_argument(
        "--hold",
        action="store_true",
        help="submit with a user hold until exact release (needs --submission-token)",
    )
    requeue = parser.add_mutually_exclusive_group()
    requeue.add_argument(
        "--requeue",
        action="store_true",
        help="default: the partition profile; a set is on when a member allows requeue",
    )
    requeue.add_argument("--no-requeue", dest="requeue", action="store_false")
    parser.set_defaults(handler=submit, requeue=None)


def selection_arguments(parser: argparse.ArgumentParser, *, job_ids: bool = False) -> None:
    """The selection of status and wait: one of --task, --name or --all, then the qualifiers.

    job_ids adds --job-id to the exclusive group and --details after it (status only).
    --campaign, --run-dir and --since mean what _selection() and _since() read.
    """
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--task", action="append", help="repeatable")
    which.add_argument("--name", action="append", help="repeatable")
    which.add_argument("--all", action="store_true", help="every shk job of this project")
    if job_ids:
        which.add_argument(
            "--job-id",
            action="append",
            help="repeatable own-UID N or N_TASK, including legacy jobs",
        )
        parser.add_argument(
            "--details", action="store_true", help="retain complete raw Slurm evidence"
        )
    parser.add_argument(
        "--campaign",
        action="append",
        help="once for every --task, or once per --task (default: default)",
    )
    parser.add_argument(
        "--run-dir", action="append", help="once per --task (default <remote_root>/runs/<name>)"
    )
    parser.add_argument(
        "--since",
        type=int,
        metavar="DAYS",
        help=f"sacct window, 1 to {MAX_SINCE_DAYS} (default {SINCE_DAYS}, {SINCE_DAYS_ALL} with --all)",
    )


def _register_status(subparsers: Subparsers) -> None:
    parser = subparsers.add_parser(
        "status", help="state of logical tasks from one squeue and one sacct call"
    )
    selection_arguments(parser, job_ids=True)
    parser.set_defaults(handler=status)


def _register_cancel(subparsers: Subparsers) -> None:
    parser = subparsers.add_parser("cancel", help="scancel verified own live jobs of one task/name")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--task")
    which.add_argument("--name")
    parser.add_argument("--campaign", default="default")
    parser.set_defaults(handler=cancel)


def _register_occupancy(subparsers: Subparsers) -> None:
    parser = subparsers.add_parser(
        "occupancy", help="running and pending jobs per user on listed partitions"
    )
    parser.add_argument("--partition", action="append", required=True, help="repeatable")
    parser.set_defaults(handler=occupancy)


def register(subparsers: Subparsers) -> None:
    """Add submit, status, cancel and occupancy, then resources, release and requeue."""
    _register_submit(subparsers)
    _register_status(subparsers)
    _register_cancel(subparsers)
    _register_occupancy(subparsers)
    node_resources.register(subparsers)
    control.register(subparsers)


# ---------------------------------------------------------------- remote calls


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cadence(ctx: Context) -> dict[str, str]:
    now = ctx.clock()
    return {"queried_at": _iso(now), "next_check_after": _iso(now + CADENCE)}


def _read(ctx: Context, query: list[str], tools: tuple[str, ...]) -> tuple[Read, dict[str, Any]]:
    """Run query.sh once: the parsed reply and the fields every read prints."""
    result = executors.check(executors.run_program(ctx.executor(), "query", query), "query")
    read = parse_read(result.stdout, tools, result.site)
    meta = {
        "executor": ctx.executor().kind,
        **sites.reply_fields(result.site),
        "user": read.user,
        "host": read.host,
        **_cadence(ctx),
    }
    return read, meta


@dataclass(frozen=True)
class Reply:
    """The terminal marker of a mutation program and the lines it printed."""

    marker: str  # the one terminal marker, or "unknown"
    value: str  # the marker's argument, or why the outcome is unknown
    lines: list[str]


def _refusal(program: str, marker: str, value: str, errors: str, site: sites.Site) -> ShkError:
    refusal = REFUSALS[marker]
    message = f"{program}: {marker} {value} {errors[-2000:]}".strip()
    return ShkError(
        EXIT_REFUSED, refusal.token, message, refusal.hint.format(shk=sites.command(site))
    )


def _mutate(
    ctx: Context, program: str, args: list[str], done: tuple[str, ...], stdin: bytes | None = None
) -> Reply:
    """Run one mutation program: refused before dispatch is exit 3, unconfirmed after is unknown.

    Only the program's own success marker confirms a result. A lost reply, more or fewer than
    one terminal marker and SHK_UNPARSED (the Slurm client failed, or printed no job id) all
    leave the outcome unknown, whatever the client's message says.
    """
    result = executors.run_program(ctx.executor(), program, args, stdin=stdin, mutation=True)
    if result.status == "unknown":
        return Reply("unknown", "no reply", [])
    lines = executors.records(executors.check(result, program).stdout)
    known = (*REFUSALS, *done, "SHK_UNPARSED")
    markers = [line.partition(" ") for line in lines if line.partition(" ")[0] in known]
    if len(markers) != 1:
        return Reply("unknown", f"{len(markers)} terminal markers", lines)
    marker, _, value = markers[0]
    # SHK_ERR| lines carry the Slurm client's own message as evidence, never as a verdict.
    errors = " ".join(
        line.removeprefix("SHK_ERR|") for line in lines if line.startswith("SHK_ERR|")
    ).strip()
    if marker in REFUSALS:
        raise _refusal(program, marker, value, errors, result.site)
    if marker == "SHK_UNPARSED":
        evidence = " ".join(part for part in (value, errors[-2000:]) if part)
        detail = f"{program} outcome not confirmed" + (f": {evidence}" if evidence else "")
        return Reply("unknown", detail, lines)
    return Reply(marker, value, lines)


def _unknown(ctx: Context, name: str, selector: str, detail: str) -> Outcome:
    """Exit 5: the mutation may or may not have happened; only a later status can tell."""
    later = _iso(ctx.clock() + CADENCE)
    project_dir = ctx.config.root if ctx.config else None
    if project_dir is None:
        project_dir = Path(ctx.project_dir or ctx.cwd or Path.cwd()).expanduser().resolve()
    command = shlex.join([
        "shk",
        "--project-dir",
        str(project_dir),
        *sites.selector(ctx.site),
        "--executor",
        ctx.executor().kind,
        "status",
    ])
    return Outcome(
        {
            "ok": False,
            "action": "unknown",
            "name": name,
            "detail": detail,
            "next": f"{command} {selector} (not before {later})",
        },
        EXIT_UNKNOWN,
    )


# ---------------------------------------------------------------- handlers


def _task_name(ctx: Context, campaign: str, task: str) -> str:
    project = ctx.require("project").project
    return spec.job_name(
        project, spec.checked_text("campaign", campaign), spec.checked_text("task", task)
    )


def _task_selector(task: str, campaign: str) -> str:
    return f"--task {shlex.quote(task)} --campaign {shlex.quote(campaign)}"


def _checked_name(name: str) -> str:
    if not spec.NAME.fullmatch(name):
        raise ShkError(EXIT_LOCAL, "spec_invalid", f"--name {name!r} is not a shk job name")
    return name


def _refuse_existing(
    name: str, found: dict[str, Any], reason: str | None, site: sites.Site
) -> None:
    """Live always refuses; done and failed refuse without --resubmit REASON."""
    state = found["state"]
    if state == "live":
        hint = REFUSALS["SHK_LIVE"].hint.format(shk=sites.command(site))
    elif state in ("done", "failed") and not reason:
        hint = "find the cause, then add --resubmit REASON"
    else:
        return
    message = f"{name} is {state} (newest job {found['current']['job_id']})"
    raise ShkError(EXIT_REFUSED, state, message, hint)


def _budget_terms(profile: partitions.Profile) -> str:
    """The thresholds as the partition's budget only where its own courtesy text states them."""
    hours = BUDGET_MINUTES // 60
    phrase = f"about {BUDGET_JOBS} jobs x {hours} h"
    if phrase in profile.courtesy:
        return f"while others are active the courtesy budget is {phrase}"
    return (
        f"this advisory starts at {BUDGET_JOBS} jobs of yours there or more than {hours} h "
        f"requested; the terms of {profile.name} are the courtesy text in this reply"
    )


def _courtesy(read: Read, built: spec.Spec) -> dict[str, Any]:
    """Occupancy, request size and courtesy text of a borrowed partition; warnings never refuse."""
    partition = built.profile.name
    users, totals = occupancy_summary(read.occupancy.get(partition, []))
    mine = users.get(read.user or "", _counts())
    my_jobs = mine["running_jobs"] + mine["pending_jobs"]
    others = any(user != read.user and counts["running_jobs"] for user, counts in users.items())
    resources = built.document["resources"]
    array = resources["array"] or {"count": 1, "throttle": None}
    request = {
        "tasks": array["count"],
        "throttle": array["throttle"],
        "minutes": resources["minutes"],
    }
    warnings = []
    if others and (my_jobs >= BUDGET_JOBS or request["minutes"] > BUDGET_MINUTES):
        warnings.append(
            f"others are running on {partition} and you have {my_jobs} jobs there; "
            + _budget_terms(built.profile)
        )
    if others and request["tasks"] > BUDGET_JOBS:
        throttle = request["throttle"]
        pace = f"at most {throttle} at a time" if throttle else "no throttle"
        warnings.append(
            f"others are running on {partition} and this array has {request['tasks']} tasks "
            f"({pace}), each requesting {request['minutes']} min; "
            "weigh the whole array against the courtesy text in this reply"
        )
    return {
        "occupancy": {"users": users, "totals": totals},
        "request": request,
        "courtesy": built.profile.courtesy,
        "warnings": warnings,
    }


def submit(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk submit`: offline checks, one read, the local decision, then one mutation.

    On a site with test_before_submit the decision includes Slurm's test of the request.
    """
    site = ctx.site
    built = spec.build(args, ctx.require("project", "remote_root"), ctx.clock(), site)
    report = {
        "name": built.name,
        "run_dir": built.run_dir,
        "argv": built.argv,
        "script_sha256": built.sha256,
    }
    if args.dry_run:
        return Outcome({"action": "dry_run", **report, "spec": built.document})
    if args.preflight:
        return control.preflight(built, ctx)
    borrowed = built.profile.borrowed
    query = [
        "names",
        str(SINCE_DAYS),
        built.profile.name if borrowed else "-",
        built.name,
        built.run_dir,
    ]
    tools = ("squeue", "sacct", "occupancy") if borrowed else ("squeue", "sacct")
    read, meta = _read(ctx, query, tools)
    rows = parse_rows(read.queue, read.acct, lambda name: name == built.name)
    found = _resolve(read, rows, built.name, site)
    _refuse_existing(built.name, found, args.resubmit, site)
    if borrowed:
        report.update(_courtesy(read, built))
    if site.test_before_submit:
        tested = control.preflight(built, ctx)
        if tested.code:  # Slurm refused the request: nothing is staged or dispatched
            return tested
    stamp = f"{ctx.clock():%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"
    remote = [built.name, built.run_dir, stamp, built.sha256, built.text, "--", *built.argv]
    reply = _mutate(ctx, "submit", remote, ("SHK_SUBMITTED",), stdin=built.script)
    if reply.marker == "unknown":
        selector = _task_selector(args.task, args.campaign)
        if args.run_dir:
            selector += f" --run-dir {built.run_dir}"
        return _unknown(ctx, built.name, selector, reply.value)
    return Outcome({
        "action": "submitted",
        "job_id": reply.value,
        "state_before": found["state"],
        **report,
        **meta,
    })


@dataclass(frozen=True)
class Selected:
    """One job name that status reports, with its run directory and --task labels."""

    name: str
    run_dir: str | None = None
    labels: dict[str, str] = field(default_factory=dict)  # campaign and task, with --task


def _selection(args: argparse.Namespace, ctx: Context) -> list[Selected]:
    selected = [Selected(_checked_name(name)) for name in args.name or ()]
    tasks = args.task or []
    campaigns = args.campaign or ["default"]
    if len(campaigns) == 1:  # one --campaign serves every --task
        campaigns = campaigns * len(tasks)
    run_dirs = args.run_dir or [None] * len(tasks)
    if len(campaigns) != len(tasks) or len(run_dirs) != len(tasks):
        raise ShkError(
            EXIT_LOCAL,
            "spec_invalid",
            "give --campaign once or once per --task, and --run-dir once per --task or not at all",
        )
    for task, campaign, given in zip(tasks, campaigns, run_dirs, strict=True):
        config, name = ctx.require("project"), _task_name(ctx, campaign, task)
        run_dir = spec.run_directory(given, config, name) if given or config.remote_root else None
        selected.append(Selected(name, run_dir, {"campaign": campaign, "task": task}))
    return selected


def _since(args: argparse.Namespace, site: sites.Site = sites.SHERLOCK) -> int:
    """The sacct window in days; the bound holds on every site, and Sherlock's slurmdbd sets it."""
    if args.since is None:
        return SINCE_DAYS_ALL if args.all else SINCE_DAYS
    since: int = args.since
    if not 1 <= since <= MAX_SINCE_DAYS:
        reason = (
            f"Sherlock's slurmdbd refuses sacct windows wider than {MAX_SINCE_DAYS} days"
            if site.name == sites.SHERLOCK.name
            else f"shk reads at most {MAX_SINCE_DAYS} days of accounting on every site"
        )
        raise ShkError(
            EXIT_LOCAL, "spec_invalid", f"--since must be 1 to {MAX_SINCE_DAYS} days", hint=reason
        )
    return since


def _all_prefix(ctx: Context) -> str:
    """--all keeps shk.<project>. names, or every shk. name without a project config."""
    project = ctx.config.project if ctx.config else None
    return f"shk.{project}." if project else "shk."


def _all_names(ctx: Context) -> Callable[[str], bool]:
    prefix = _all_prefix(ctx)
    return lambda name: name.startswith(prefix)


def _status_read(ctx: Context, since: int, selected: list[Selected], every: bool) -> Outcome:
    """One read of the selected names, or with every (--all) of every shk name of the project."""
    query = ["all" if every else "names", str(since), "-"]
    for item in selected:
        query += [item.name, item.run_dir or "-"]
    read, meta = _read(ctx, query, ("squeue", "sacct"))
    wanted = {item.name for item in selected}
    rows = parse_rows(
        read.queue, read.acct, _all_names(ctx) if every else lambda name: name in wanted
    )
    listed = selected or [Selected(name) for name in sorted(rows.by_name)]
    names = [
        {"name": item.name, **item.labels, **_resolve(read, rows, item.name, ctx.site)}
        for item in listed
    ]
    anomalous = any(entry["current"] and entry["current"]["anomalies"] for entry in names)
    return Outcome(
        {**meta, "names": names, "truncated": rows.truncated},
        EXIT_ANOMALY if anomalous else EXIT_OK,
    )


def status_reader(args: argparse.Namespace, ctx: Context) -> Callable[[], Outcome]:
    """Check the --task/--name/--all selection and --since once; each call is one status read.

    The call returns what `shk status` prints for the selection: wait repeats it.
    """
    since, selected = _since(args, ctx.site), _selection(args, ctx)
    return lambda: _status_read(ctx, since, selected, args.all)


def status(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk status`: one read for every selected name; exit 6 on an anomaly."""
    if args.job_id or args.details:
        return details.status(args, ctx)
    return status_reader(args, ctx)()


def cancel(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk cancel`: scancel the live jobs of one name (one mutation)."""
    if args.task is not None:
        name = _task_name(ctx, args.campaign, args.task)
        selector = _task_selector(args.task, args.campaign)
    else:
        name = _checked_name(args.name)
        selector = f"--name {name}"
    reply = _mutate(ctx, "cancel", [name], ("SHK_CANCELLED", "SHK_NONE"))
    if reply.marker == "unknown":
        return _unknown(ctx, name, selector, reply.value)
    jobs = [line.removeprefix("SHK_JOB|") for line in reply.lines if line.startswith("SHK_JOB|")]
    action = "cancelled" if reply.marker == "SHK_CANCELLED" else "nothing_live"
    return Outcome({
        "action": action,
        "name": name,
        "jobs": jobs,
        "executor": ctx.executor().kind,
        **sites.reply_fields(ctx.site),
        **_cadence(ctx),
    })


def occupancy(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk occupancy`: running and pending jobs per user on listed partitions."""
    profiles = {name: partitions.profile(name, ctx.site) for name in args.partition}
    read, meta = _read(ctx, ["occupancy", "-", "-", *profiles], ("occupancy",))
    report = {}
    for name, profile in profiles.items():
        users, totals = occupancy_summary(read.occupancy.get(name, []))
        report[name] = {"profile": asdict(profile), "users": users, "totals": totals}
    return Outcome({**meta, "partitions": report})
