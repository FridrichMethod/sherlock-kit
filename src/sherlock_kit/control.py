"""Bounded preflight, exact held-job release and allocation-local self requeue."""

from __future__ import annotations

import argparse
import os
import re
from typing import TYPE_CHECKING, Any

from sherlock_kit import executor, partitions, spec
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


def _change(action: str, job: str, args: argparse.Namespace, ctx: Context, *extra: str) -> Outcome:
    from sherlock_kit.slurm import _cadence, _mutate, _unknown

    token = args.expect_token
    if not args.expect_name or (token is not None and not spec.TOKEN.fullmatch(token)):
        raise ShkError(
            EXIT_LOCAL,
            "spec_invalid",
            "expected name must be nonempty and a given public token valid",
        )
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
