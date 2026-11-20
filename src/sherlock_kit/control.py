"""Bounded preflight, exact held-job release, allocation-local self requeue and job update.

update changes one field of a pending job or of a whole array through data/remote/update.sh:
the program checks identity and policy on every record, dispatches one scontrol update and
re-reads the records once; only that re-read, never the exit code of scontrol, confirms it.
"""

from __future__ import annotations

import argparse
import os
import re
from typing import TYPE_CHECKING, Any

from sherlock_kit import details, executor, partitions, spec
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context
    from sherlock_kit.slurm import Subparsers

# Slurm client messages for a controller that did not answer: the read failed, nothing decided.
TRANSIENT = re.compile(
    r"Unable to contact slurm controller|Socket timed out on send/recv operation"
    r"|Zero Bytes were transmitted or received"
)
# The will-run line of sbatch --test-only, matched against a whole line (Slurm 25.11
# src/api/allocate.c; 24.11 and older print no " a" and no cluster clause).
ESTIMATE = re.compile(
    r"(?:sbatch: )?Job [0-9]+ to start at (?P<start>\S+)(?: on cluster \S+)?(?: a)?"
    r" using [0-9]+ processors on nodes (?P<nodes>\S+) in partition (?P<partition>\S+)"
)
# The one start text that is read: Slurm's default %FT%T, local time without a zone.
ESTIMATE_START = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")
# A --constraint or --prefer value: no whitespace (it never matches a feature and would split
# the record), no "," (Slurm stores it as "&") and no "=" (it would forge a field). 1024 bytes
# is the longest feature string slurmctld accepts.
FEATURES = re.compile(r"[A-Za-z0-9_.:+*&|()\[\]-]{1,1024}")
THROTTLE = re.compile(r"[1-9][0-9]{0,3}")
# The [D-]HH:MM:SS of a Slurm time limit (mins2time_str); UNLIMITED and Partition_Limit are not.
LIMIT = re.compile(r"(?:([1-9][0-9]*)-)?([0-9]{2}):([0-5][0-9]):([0-5][0-9])")
# The Slurm keys each update reports per record, besides job_id, array_task_id and state.
UPDATED_KEYS = {
    "time": ("TimeLimit", "TimeMin"),
    "partition": ("Partition", "Requeue"),
    "features": ("Features", "Prefer"),
    "throttle": ("ArrayTaskThrottle",),
}


def transient(action: str, stderr: str) -> ShkError | None:
    """A retryable read failure when Slurm reports a controller transport error, else None."""
    if not TRANSIENT.search(stderr):
        return None
    return ShkError(
        EXIT_LOCAL,
        "malformed_read",
        f"{action}: Slurm controller unavailable: {stderr.strip()[-2000:]}",
        hint="retry once after 60 s",
    )


def checked_id(value: str) -> str:
    """An exact physical or array element id, never a Slurm expression or list."""
    if not spec.JOB_ID.fullmatch(value):
        raise ShkError(EXIT_LOCAL, "spec_invalid", f"job id {value!r}: use N or N_TASK")
    return value


def register(subparsers: Subparsers) -> None:
    """Register narrow mutation commands with explicit identity guards."""
    for command, handler in (("release", release), ("requeue", requeue)):
        parser = subparsers.add_parser(command, help=f"identity-verified {command}")
        if command == "release":
            parser.add_argument("--job-id", required=True)
        else:
            parser.add_argument("--self", required=True, action="store_true")
        parser.add_argument("--expect-name", required=True)
        parser.add_argument(
            "--expect-token",
            required=command == "release",
            help="Slurm Comment to match (optional for requeue: checked only when given)",
        )
        parser.set_defaults(handler=handler)
    parser = subparsers.add_parser(
        "update", help="identity-verified change of one field of a pending job or a whole array"
    )
    parser.add_argument("--job-id", required=True, help="a job id or an ArrayJobId (N only)")
    parser.add_argument("--expect-name", required=True)
    parser.add_argument(
        "--expect-token", help="Slurm Comment to match on every record (checked only when given)"
    )
    parser.add_argument("--time", help="a lower time limit: 2h, 90m or H:MM:SS")
    parser.add_argument(
        "--partition", help="P or P,P of the same requeue and preemptible class as the job"
    )
    parser.add_argument("--constraint", help="Slurm Features, sent together with --prefer")
    parser.add_argument("--prefer", help="Slurm Prefer, sent together with --constraint")
    parser.add_argument("--throttle", help="ArrayTaskThrottle of an array: 1 to 1000")
    parser.set_defaults(handler=update)


def parse_estimate(stderr: str, stdout: str) -> dict[str, str | None]:
    """Slurm's start estimate in a --test-only reply, read from one whole estimate line only.

    The lines are stderr's, then stdout's, each split on LF. Exactly one estimate line with an
    ISO start is parsed. Without any line holding " to start at " the estimate is absent.
    Anything else is unparsed: it names the first estimate line, else the first line holding
    those words, and takes no field from it. The start stays the text Slurm printed, in the
    local zone of the sbatch process; the job id in the line names a temporary record and
    fills no field.
    """
    lines = executor.records(stderr) + executor.records(stdout)
    matches = [found for text in lines if (found := ESTIMATE.fullmatch(text))]
    mentions = [text for text in lines if " to start at " in text]  # every estimate line too
    fields: tuple[str | None, ...] = (None, None, None)
    line: str | None = None
    if len(matches) == 1 and ESTIMATE_START.fullmatch(matches[0]["start"]):
        status, line = "parsed", matches[0].string
        fields = matches[0].group("start", "nodes", "partition")
    elif mentions:
        status, line = "unparsed", matches[0].string if matches else mentions[0]
    else:
        status = "absent"
    start, nodes, partition = fields
    return {
        "status": status,
        "start": start,
        "start_zone": "sbatch-local",
        "nodes": nodes,
        "partition": partition,
        "line": line,
    }


def preflight(built: spec.Spec, ctx: Context) -> Outcome:
    """Use sbatch --test-only as a read: a Slurm refusal is exit 3, a controller failure exit 1.

    The estimate beside the raw evidence is advisory: no reservation, no promise, no conversion.
    """
    from sherlock_kit.slurm import _cadence

    result = executor.check(
        executor.run_program(ctx.executor(), "preflight", built.argv, stdin=built.script),
        "preflight",
    )
    lines = executor.records(result.stdout)
    markers = [
        line.removeprefix("SHK_RC sbatch ") for line in lines if line.startswith("SHK_RC sbatch ")
    ]
    if "SHK_END" not in lines or len(markers) != 1 or not markers[0].isdecimal():
        raise ShkError(EXIT_LOCAL, "malformed_read", "incomplete sbatch preflight reply")
    code = int(markers[0])
    evidence: dict[str, Any] = {"returncode": code}
    for key, prefix in (("stdout", "SHK_OUTHEX|"), ("stderr", "SHK_ERRHEX|")):
        encoded = "".join(line.removeprefix(prefix) for line in lines if line.startswith(prefix))
        try:
            evidence[key] = bytes.fromhex(encoded).decode("utf-8", "replace")
        except ValueError:
            raise ShkError(
                EXIT_LOCAL, "malformed_read", "invalid preflight output encoding"
            ) from None
    if code and (failure := transient("preflight", evidence["stderr"])):
        raise failure
    cadence = _cadence(ctx)
    return Outcome(
        {
            "ok": code == 0,
            "action": "preflight",
            "job_submitted": False,
            "name": built.name,
            "run_dir": built.run_dir,
            "argv": built.argv,
            "script_sha256": built.sha256,
            "spec": built.document,
            "preflight": evidence,
            "estimate": {
                **parse_estimate(evidence["stderr"], evidence["stdout"]),
                "observed_at": cadence["queried_at"],
            },
            "executor": ctx.executor().kind,
            **cadence,
        },
        EXIT_REFUSED if code else 0,
    )


def _expected_token(args: argparse.Namespace) -> str | None:
    """The given --expect-token, after checking it and a nonempty --expect-name."""
    token: str | None = args.expect_token
    if not args.expect_name or (token is not None and not spec.TOKEN.fullmatch(token)):
        raise ShkError(
            EXIT_LOCAL,
            "spec_invalid",
            "expected name must be nonempty and a given public token valid",
        )
    return token


def _change(action: str, job: str, args: argparse.Namespace, ctx: Context, *extra: str) -> Outcome:
    from sherlock_kit.slurm import _cadence, _mutate, _unknown

    token = _expected_token(args)
    reply = _mutate(
        ctx, "control", [action, job, args.expect_name, token or "", *extra], ("SHK_CHANGED",)
    )
    if reply.marker == "unknown":
        return _unknown(ctx, args.expect_name, f"--job-id {job} --details", reply.value)
    return Outcome({
        "action": "released" if action == "release" else "requeued",
        "job_id": reply.value,
        "name": args.expect_name,
        "submission_token": token,
        "executor": ctx.executor().kind,
        **_cadence(ctx),
    })


def release(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Release one exact own job only after verifying name, token and user hold."""
    return _change("release", checked_id(args.job_id), args, ctx)


def requeue(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Requeue the current physical allocation only; the shell verifies Slurm PID membership.

    The shell reads Slurm's record of SLURM_JOB_ID and requeues an array element as its
    ArrayJobId_ArrayTaskId: the bare id of the element that keeps the array's meta record would
    requeue every element, and inherited SLURM_ARRAY_* variables can name another job.
    """
    if ctx.executor().kind != "local":
        raise ShkError(
            EXIT_REFUSED,
            "self_only",
            "self requeue requires --executor local inside the allocation",
        )
    physical = os.environ.get("SLURM_JOB_ID", "")
    if "_" in physical or not spec.JOB_ID.fullmatch(physical):
        raise ShkError(EXIT_REFUSED, "self_only", "no physical current allocation id")
    allowed = ",".join(name for name, found in partitions.profiles().items() if found.requeue)
    return _change("requeue", physical, args, ctx, allowed)


def _invalid(message: str, hint: str | None = None) -> ShkError:
    return ShkError(EXIT_LOCAL, "spec_invalid", message, hint)


def _bit(flag: bool) -> str:
    return "1" if flag else "0"


def _request(args: argparse.Namespace) -> tuple[str, list[str], dict[str, str]]:
    """The field, its update.sh arguments and the Slurm KEY=VALUE pairs, checked offline."""
    given = {
        "time": args.time is not None,
        "partition": args.partition is not None,
        "features": args.constraint is not None or args.prefer is not None,
        "throttle": args.throttle is not None,
    }
    chosen = [field for field, present in given.items() if present]
    if len(chosen) != 1:
        raise _invalid(
            "give exactly one of --time, --partition, --constraint/--prefer or --throttle",
            hint="run one shk update per field: Slurm applies the fields of one update one by "
            "one, without rollback",
        )
    field = chosen[0]
    if field == "time":
        minutes = str(spec.minutes(args.time))
        return field, [minutes], {"TimeLimit": minutes}
    if field == "partition":
        profile, _members = spec.partition_request(args.partition)
        if profile.borrowed:
            raise ShkError(
                EXIT_REFUSED,
                "borrowed_not_allowed",
                f"partition {profile.name} is borrowed: shk update never moves a job there",
                hint="cancel, run shk occupancy, then resubmit with --courtesy-ack",
            )
        same = [
            name
            for name, found in partitions.profiles().items()
            if found.preemptible == profile.preemptible
        ]
        remote = [
            args.partition,
            _bit(profile.requeue),
            _bit(profile.gpus_allowed),
            ",".join(same),
        ]
        return field, remote, {"Partition": args.partition}
    if field == "features":
        requested = {}
        for label, key, value in (
            ("constraint", "Features", args.constraint),
            ("prefer", "Prefer", args.prefer),
        ):
            if value is None:
                continue
            if not FEATURES.fullmatch(value):
                raise _invalid(
                    f"--{label} {value!r}: give 1 to 1024 characters of A-Z a-z 0-9 _ . : + * & "
                    "| ( ) [ ] - (no spaces, commas or '='); clearing a value is not supported"
                )
            if value == "(null)":
                raise _invalid(
                    f"--{label} '(null)': Slurm prints (null) for an unset value; clearing a "
                    "value is not supported"
                )
            requested[key] = value
        return field, [args.constraint or "", args.prefer or ""], requested
    if not THROTTLE.fullmatch(args.throttle) or int(args.throttle) > spec.ARRAY_MAX_TASKS:
        raise _invalid(
            f"--throttle {args.throttle!r}: a whole number from 1 to {spec.ARRAY_MAX_TASKS} "
            "(0, no limit, is not accepted)"
        )
    borrowed = [name for name, found in partitions.profiles().items() if found.borrowed]
    return field, [args.throttle, ",".join(borrowed)], {"ArrayTaskThrottle": args.throttle}


def _record(line: str, name: str, field: str) -> dict[str, str | None]:
    """One scontrol show job --oneliner record as a reply RECORD.

    RECORD is job_id, array_task_id, state and the keys of the field as Slurm printed them,
    None when absent. The leading fields are read up to the first JobName, the others only
    between the expected JobName and Command, before the free text (Command, SubmitLine,
    WorkDir, Comment, paths) that could otherwise shadow them. A record without the expected
    name gives only its leading fields.
    """
    lead = line.partition(" JobName=")[0]
    prefix = f"{lead} JobName={name} "
    head = line.removeprefix(prefix).partition(" Command=")[0] if line.startswith(prefix) else ""
    found = {**details.fields(head), **details.fields(lead)}
    record = {
        "job_id": found.get("JobId"),
        "array_task_id": found.get("ArrayTaskId"),
        "state": found.get("JobState"),
    }
    return record | {key: found.get(key) for key in UPDATED_KEYS[field]}


def _limit_minutes(text: str | None) -> int | None:
    match = LIMIT.fullmatch(text or "")
    if match is None:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 1440 + hours * 60 + minutes + (seconds > 0)


def _shows(field: str, record: dict[str, str | None], requested: dict[str, str]) -> bool:
    """Whether a re-read record carries the requested value (a partition set in any order)."""
    if field == "time":
        return _limit_minutes(record["TimeLimit"]) == int(requested["TimeLimit"])
    if field == "partition":
        return set((record["Partition"] or "").split(",")) == set(requested["Partition"].split(","))
    return all(record[key] == value for key, value in requested.items())


def _unconfirmed(
    field: str, lines: list[str], after: list[dict[str, str | None]], requested: dict[str, str]
) -> str | None:
    """Why the re-read after a dispatch does not confirm the update, or None when it does."""
    if any(line.startswith("SHK_AFTER_FAILED") for line in lines):
        return "the re-read failed"
    if not after:
        return "the re-read found no record"
    missing = [str(record["job_id"]) for record in after if not _shows(field, record, requested)]
    if missing:
        wanted = " ".join(f"{key}={value}" for key, value in requested.items())
        return f"records {', '.join(missing)} do not show {wanted}"
    return None


def update(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Change one field of one own job or whole array after offline and remote checks.

    The remote program refuses before dispatch (exit 3) or reports the request as already
    shown (unchanged); after its one scontrol update, the re-read alone decides: every record
    showing the requested value is updated, anything else is unknown (exit 5).
    """
    from sherlock_kit.slurm import _cadence, _mutate, _unknown

    job = args.job_id
    if not re.fullmatch(r"[1-9][0-9]*", job):
        raise _invalid(
            f"--job-id {job!r}: use N, a job id or the ArrayJobId of a whole array",
            hint="an update addresses the whole job or the whole array, never one task",
        )
    token = _expected_token(args)
    field, remote, requested = _request(args)
    reply = _mutate(
        ctx,
        "update",
        [job, args.expect_name, token or "", field, *remote],
        ("SHK_UNCHANGED", "SHK_DISPATCHED"),
    )
    selector = f"--job-id {job} --details"
    if reply.marker == "unknown":
        return _unknown(ctx, args.expect_name, selector, reply.value)

    def records(prefix: str) -> list[dict[str, str | None]]:
        return [
            _record(line.removeprefix(prefix), args.expect_name, field)
            for line in reply.lines
            if line.startswith(prefix)
        ]

    before, after = records("SHK_BEFORE|"), records("SHK_AFTER|")
    # Slurm's own text (stderr, then stdout) is evidence, never a verdict.
    messages = [
        line.partition("|")[2] for line in reply.lines if line.startswith(("SHK_ERR|", "SHK_OUT|"))
    ]
    if reply.marker == "SHK_DISPATCHED" and (
        problem := _unconfirmed(field, reply.lines, after, requested)
    ):
        text = " ".join(messages).strip()[-2000:]
        detail = f"update outcome not confirmed: scontrol update exit {reply.value}"
        detail += (f": {text}" if text else "") + f"; {problem}"
        return _unknown(ctx, args.expect_name, selector, detail)
    return Outcome({
        "action": "unchanged" if reply.marker == "SHK_UNCHANGED" else "updated",
        "job_id": job,
        "name": args.expect_name,
        "submission_token": token,
        "field": field,
        "requested": requested,
        "before": before,
        "after": after,
        "slurm_messages": "\n".join(messages) or None,
        "executor": ctx.executor().kind,
        **_cadence(ctx),
    })
