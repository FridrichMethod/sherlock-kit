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

TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


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


def preflight(built: spec.Spec, ctx: Context) -> Outcome:
    """Use sbatch --test-only as a read; a quota refusal is exit 3, never unknown."""
    from sherlock_kit.slurm import _cadence

    result = executor.check(
        executor.run_program(ctx.executor(), "preflight", built.argv, stdin=built.script),
        "preflight",
    )
    lines = result.stdout.splitlines()
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
            "executor": ctx.executor().kind,
            **_cadence(ctx),
        },
        EXIT_REFUSED if code else 0,
    )


def _change(action: str, job: str, args: argparse.Namespace, ctx: Context, *extra: str) -> Outcome:
    from sherlock_kit.slurm import _cadence, _mutate, _unknown

    token = args.expect_token
    if not args.expect_name or (token is not None and not TOKEN.fullmatch(token)):
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
        "job_id": job,
        "name": args.expect_name,
        "submission_token": token,
        "executor": ctx.executor().kind,
        **_cadence(ctx),
    })


def release(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Release one exact own job only after verifying name, token and user hold."""
    return _change("release", checked_id(args.job_id), args, ctx)


def requeue(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Requeue the current allocation only; the shell verifies Slurm PID membership.

    An array element is addressed as ArrayJobId_ArrayTaskId: the bare id of the element that
    keeps the array's meta record would show and requeue every element of the array.
    """
    if ctx.executor().kind != "local":
        raise ShkError(
            EXIT_REFUSED,
            "self_only",
            "self requeue requires --executor local inside the allocation",
        )
    physical = os.environ.get("SLURM_JOB_ID", "")
    array, task = os.environ.get("SLURM_ARRAY_JOB_ID"), os.environ.get("SLURM_ARRAY_TASK_ID")
    target = physical if array is None and task is None else f"{array}_{task}"
    if "_" in physical or not spec.JOB_ID.fullmatch(physical) or not spec.JOB_ID.fullmatch(target):
        raise ShkError(EXIT_REFUSED, "self_only", "no physical current allocation id")
    allowed = ",".join(name for name, found in partitions.profiles().items() if found.requeue)
    return _change("requeue", target, args, ctx, allowed)
