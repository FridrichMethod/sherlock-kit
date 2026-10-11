"""`shk doctor [--remote]`: read-only checks; without --remote nothing is dialled.

Python, ssh/rsync, config, executor, leftover SHERLOCK_KIT_* variables and `ssh -O check`
of both hosts; --remote adds data/remote/doctor.sh (uname -n, id -un, Slurm tools,
sha256sum, /etc/agents/AGENTS.md). Exit 0 healthy, 1 problem, 4 unreachable (also: no
usable control master with the ssh executor; a missing data master is a warning).
"""

from __future__ import annotations

import platform
import shutil

from sherlock_kit import executor as executors
from sherlock_kit import version_line
from sherlock_kit.config import DEFAULTS
from sherlock_kit.errors import EXIT_LOCAL, EXIT_OK, EXIT_UNREACHABLE, Outcome, ShkError

REMOTE_TOOLS = ("sbatch", "squeue", "sacct", "sha256sum")


def register(subparsers) -> None:
    parser = subparsers.add_parser(
        "doctor", help="read-only checks; --remote runs one read-only program"
    )
    parser.add_argument(
        "--remote", action="store_true", help="also run one read-only program on Sherlock"
    )
    parser.set_defaults(handler=handle)


def _config(ctx, problems):
    try:
        found = ctx.config
    except ShkError as exc:
        problems.append(exc.message)
        return None, {"status": exc.token}
    if found is None:
        return None, {"status": "none"}
    return found, {
        "status": "found",
        "path": str(found.path),
        "project": found.project,
        "remote_root": found.remote_root,
    }


def _masters(found, problems, warnings) -> dict:
    states = {}
    for role, host, sink in (
        ("control", (found or DEFAULTS).control_host, problems),
        ("data", (found or DEFAULTS).data_host, warnings),
    ):
        states[role], detail = executors.master_status(host)
        if states[role] != "running":
            hint = executors.master_hint(states[role], host, data=role == "data")
            sink.append(
                f"no usable SSH master for {host} ({role}, {states[role]}: {detail}); ask the user to: {hint}"
            )
    return states


def _remote(ctx, problems, warnings):
    try:
        result = executors.run_program(ctx.executor(), "doctor")
    except ShkError as exc:
        return {"status": exc.token, "message": exc.message, "hint": exc.hint}, exc.code
    if result.status != "complete":
        code = EXIT_LOCAL if result.status == "failed" else EXIT_UNREACHABLE
        return {"status": result.status, "stderr": result.stderr.strip()[-1000:]}, code
    words = [line.split(" ", 2) for line in result.stdout.splitlines() if line.startswith("SHK_")]
    facts = {w[0]: " ".join(w[1:]) for w in words if w[0] != "SHK_TOOL"}
    tools = {
        w[1]: None if w[2] == "-" else w[2] for w in words if w[0] == "SHK_TOOL" and len(w) == 3
    }
    if "SHK_END" not in facts or set(tools) != set(REMOTE_TOOLS):
        return {"status": "malformed", "stdout": result.stdout[-1000:]}, EXIT_LOCAL
    problems.extend(
        f"{tool} is not on the remote PATH" for tool, path in tools.items() if path is None
    )
    if facts.get("SHK_SITE") != "readable":
        warnings.append("/etc/agents/AGENTS.md is not readable where shk runs")
    return {
        "status": "complete",
        "executor": ctx.executor().kind,
        "host": facts.get("SHK_HOST"),
        "user": facts.get("SHK_USER"),
        "tools": tools,
        "site_instructions": facts.get("SHK_SITE"),
    }, EXIT_OK


def handle(args, ctx) -> Outcome:
    problems, warnings = [], []
    found, config_report = _config(ctx, problems)
    report = {
        "version": version_line(),
        "python": platform.python_version(),
        "host": ctx.hostname,
        "tools": {tool: shutil.which(tool) for tool in ("ssh", "rsync")},
        "config": config_report,
        "executor": None,
        "masters": {},
    }
    try:
        report["executor"] = executors.detect(
            found, ctx.executor_override, ctx.env, ctx.hostname
        ).kind
    except ShkError as exc:
        problems.append(exc.message)
    leftovers = sorted(name for name in ctx.env if name.startswith("SHERLOCK_KIT_"))
    if leftovers:
        warnings.append(
            f"obsolete 0.3 variables set: {', '.join(leftovers)}; remove them from profiles/launchers"
        )
    if report["executor"] == "ssh":  # a missing ssh shows up as the ssh_missing master state
        report["masters"] = _masters(found, problems, warnings)
    code = EXIT_UNREACHABLE if report["masters"].get("control", "running") != "running" else EXIT_OK
    if args.remote and report["executor"]:
        report["remote"], remote_code = _remote(ctx, problems, warnings)
        code = code or remote_code
    code = code or (EXIT_LOCAL if problems else EXIT_OK)
    return Outcome(
        {"ok": code == EXIT_OK, **report, "problems": problems, "warnings": warnings}, code
    )
