"""`shk doctor [--remote]`: read-only checks; without --remote nothing is dialled.

Python, ssh and rsync, the configuration, the site, the executor and `ssh -O check` of both hosts
(once where they are one host); --remote adds data/remote/doctor.sh (uname -n, id -un, the Slurm
tools, sha256sum, /etc/agents/AGENTS.md), through the site's Slurm environment. The checks use the
site of the command (cli.Context.site); with an invalid configuration, the defaults of the site
--site names. A --site that differs from the configuration's site is a problem, checked no
further. Only a site with an instructions file reports site_instructions and warns when it is not
readable. A site other than Sherlock adds its "site" key after "executor". Exit 0 healthy,
1 problem, 4 unreachable (also: no usable control master with the ssh executor; a missing data
master is only a warning).
"""

from __future__ import annotations

import argparse
import platform
import shutil
from typing import TYPE_CHECKING, Any

from sherlock_kit import executor as executors
from sherlock_kit import sites, version_line
from sherlock_kit.config import ProjectConfig, defaults
from sherlock_kit.errors import EXIT_LOCAL, EXIT_OK, EXIT_UNREACHABLE, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context

REMOTE_TOOLS = ("sbatch", "squeue", "sacct", "sha256sum")


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add the doctor command."""
    parser = subparsers.add_parser(
        "doctor", help="read-only checks; --remote runs one read-only program"
    )
    parser.add_argument(
        "--remote", action="store_true", help="also run one read-only program on Sherlock"
    )
    parser.set_defaults(handler=handle)


def _config(ctx: Context, problems: list[str]) -> tuple[ProjectConfig | None, dict[str, Any]]:
    try:
        found = ctx.config
    except ShkError as exc:
        problems.append(exc.message)
        return None, {"status": exc.token}
    if found is None:
        return None, {"status": "none"}
    report = {  # an unset project or remote_root ("") is reported as null
        "path": str(found.path),
        "project": found.project or None,
        "remote_root": found.remote_root or None,
    }
    return found, {"status": "found", **report}


def _site(
    ctx: Context, found: ProjectConfig | None, problems: list[str]
) -> tuple[sites.Site, bool]:
    """(the site to check, whether it is checked); a site mismatch is a problem."""
    if found is None:  # none, or invalid: the site --site names
        return ctx.flag_site, True
    try:
        return ctx.site, True
    except ShkError as exc:
        problems.append(exc.message)
        return ctx.flag_site, False


def _masters(
    settings: ProjectConfig, site: sites.Site, problems: list[str], warnings: list[str]
) -> dict[str, str]:
    """`ssh -O check` of both hosts: a control problem is fatal, a data problem a warning."""
    states: dict[str, str] = {}
    for role, host, sink in (
        ("control", settings.control_host, problems),
        ("data", settings.data_host, warnings),
    ):
        if role == "data" and host == settings.control_host:  # one host: one master, one check
            states[role] = states["control"]
            continue
        state, detail = executors.master_status(host)
        states[role] = state
        if state != "running":
            hint = executors.master_hint(state, host, data=role == "data", site=site)
            if state in {"none", "not_configured"}:
                hint = f"ask the user to: {hint}"
            sink.append(f"no usable SSH master for {host} ({role}, {state}: {detail}); {hint}")
    return states


def _remote(ctx: Context, problems: list[str], warnings: list[str]) -> tuple[dict[str, Any], int]:
    """Run doctor.sh once: its report and the exit code it implies."""
    try:
        result = executors.run_program(ctx.executor(), "doctor")
    except ShkError as exc:
        return {"status": exc.token, "message": exc.message, "hint": exc.hint}, exc.code
    if result.status != "complete":
        code = EXIT_LOCAL if result.status == "failed" else EXIT_UNREACHABLE
        return {"status": result.status, "stderr": result.stderr.strip()[-1000:]}, code
    lines = executors.records(result.stdout)
    words = [line.split(" ", 2) for line in lines if line.startswith("SHK_")]
    facts = {w[0]: " ".join(w[1:]) for w in words if w[0] != "SHK_TOOL"}
    tools = {
        w[1]: None if w[2] == "-" else w[2] for w in words if w[0] == "SHK_TOOL" and len(w) == 3
    }
    if "SHK_END" not in facts or set(tools) != set(REMOTE_TOOLS):
        return {"status": "malformed", "stdout": result.stdout[-1000:]}, EXIT_LOCAL
    problems.extend(
        f"{tool} is not on the remote PATH" for tool, path in tools.items() if path is None
    )
    site = result.site
    report = {
        "status": "complete",
        "executor": ctx.executor().kind,
        **sites.reply_fields(site),
        "host": facts.get("SHK_HOST"),
        "user": facts.get("SHK_USER"),
        "tools": tools,
    }
    if site.instructions is not None:
        if facts.get("SHK_SITE") != "readable":
            warnings.append(f"{site.instructions} is not readable where shk runs")
        report["site_instructions"] = facts.get("SHK_SITE")
    return report, EXIT_OK


def handle(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk doctor`: every check, then exit 0, 1 or 4."""
    problems: list[str] = []
    warnings: list[str] = []
    found, config_report = _config(ctx, problems)
    site, checked = _site(ctx, found, problems)
    settings = found or defaults(site)
    report: dict[str, Any] = {
        "version": version_line(),
        "python": platform.python_version(),
        "host": ctx.hostname,
        "tools": {tool: shutil.which(tool) for tool in ("ssh", "rsync")},
        "config": config_report,
        "executor": None,
        **sites.reply_fields(site),
        "masters": {},
    }
    if checked:
        try:
            executor = executors.detect(settings, ctx.executor_override, ctx.env, ctx.hostname)
            report["executor"] = executor.kind
        except ShkError as exc:
            problems.append(exc.message)
    if report["executor"] == "ssh":  # a missing ssh shows up as the ssh_missing master state
        report["masters"] = _masters(settings, site, problems, warnings)
    code = EXIT_UNREACHABLE if report["masters"].get("control", "running") != "running" else EXIT_OK
    if args.remote and report["executor"]:
        report["remote"], remote_code = _remote(ctx, problems, warnings)
        code = code or remote_code
    code = code or (EXIT_LOCAL if problems else EXIT_OK)
    return Outcome(
        {"ok": code == EXIT_OK, **report, "problems": problems, "warnings": warnings}, code
    )
