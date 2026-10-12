"""The shk command line: global options, subcommands, JSON output, exit codes (frozen 0.4.0).

Each command module has register(subparsers), whose subcommands set_defaults(handler=fn) with
fn(args, ctx) -> Outcome. stdout is the payload as JSON, or its "text" verbatim. A ShkError
prints its payload with its exit code; any other exception prints a traceback on stderr and an
internal_error payload (exit 1); usage errors stay argparse's exit 2.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from types import ModuleType
from typing import Any

from sherlock_kit import config as project_config
from sherlock_kit import doctor, install, slurm, transfer, version_line
from sherlock_kit import executor as executors
from sherlock_kit.errors import EXIT_ANOMALY, EXIT_LOCAL, EXIT_OK, Outcome, ShkError

MODULES: tuple[ModuleType, ...] = (doctor, slurm, transfer, install)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Context:
    """What a handler needs besides its arguments: configuration, executors, clock, host."""

    def __init__(
        self,
        *,
        project_dir: str | Path | None = None,
        executor: str = "auto",
        env: Mapping[str, str] | None = None,
        hostname: str | None = None,
        clock: Callable[[], datetime] | None = None,
        cwd: str | Path | None = None,
    ) -> None:
        """Keep the overrides; the configuration and the executors are resolved on first use."""
        self.project_dir = project_dir
        self.executor_override = executor
        self.env = os.environ if env is None else env
        self.hostname = socket.gethostname() if hostname is None else hostname
        self.clock = clock or _utc_now  # aware UTC now; tests inject a fixed clock
        self.cwd = cwd
        self._executors: dict[bool, executors.Executor] = {}

    @cached_property
    def config(self) -> project_config.ProjectConfig | None:
        """The project configuration (discovered once), or None when there is none."""
        return project_config.discover(self.project_dir, self.cwd)

    def require(self, *keys: str) -> project_config.ProjectConfig:
        """The configuration with every key set; else ShkError(1, "config_required")."""
        found = self.config
        missing = list(keys) if found is None else [key for key in keys if not getattr(found, key)]
        if found is None or missing:
            start = self.project_dir or self.cwd or os.getcwd()
            where = found.path if found else f"pyproject.toml/sherlock-kit.toml at or above {start}"
            raise ShkError(
                EXIT_LOCAL,
                "config_required",
                f"{where}: missing {', '.join(missing)}",
                hint=f"add {', '.join(missing)} to [tool.sherlock-kit] (or pass --project-dir)",
            )
        return found

    def _executor(self, *, data: bool) -> executors.Executor:
        if data not in self._executors:
            self._executors[data] = executors.detect(
                self.config, self.executor_override, self.env, self.hostname, data=data
            )
        return self._executors[data]

    def executor(self) -> executors.Executor:
        """The control-host executor, detected once."""
        return self._executor(data=False)

    def data_executor(self) -> executors.Executor:
        """The data-host (DTN) executor, detected once; on Sherlock it refuses transfers."""
        return self._executor(data=True)


def build_parser(modules: Sequence[ModuleType] = MODULES) -> argparse.ArgumentParser:
    """The shk parser: the global options and the commands of modules."""
    parser = argparse.ArgumentParser(
        prog="shk",
        description="Submit and track Slurm jobs and move data on Sherlock. JSON on stdout; "
        "exit 0 ok, 1 local error, 2 usage, 3 refused, 4 unreachable, 5 outcome unknown, "
        "6 anomaly.",
    )
    parser.add_argument(
        "--version", action="store_true", help="print the version and where it came from"
    )
    parser.add_argument(
        "--project-dir", metavar="DIR", help="read the config from DIR (default: search upward)"
    )
    parser.add_argument(
        "--executor",
        choices=("auto", "ssh", "local"),
        default="auto",
        help="ssh from a workstation or local on Sherlock (default: detect; env SHK_EXECUTOR)",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for module in modules:
        module.register(subparsers)
    return parser


def _render(payload: dict[str, Any], code: int) -> str:
    if "text" in payload:
        return str(payload["text"])
    body = payload if "ok" in payload else {"ok": code in (EXIT_OK, EXIT_ANOMALY), **payload}
    return (
        json.dumps({"schema_version": 1, **body}, indent=2, ensure_ascii=False, default=str) + "\n"
    )


def _run(
    argv: Sequence[str] | None, modules: Sequence[ModuleType], context: Context | None
) -> tuple[str, int]:
    parser = build_parser(modules)
    args = parser.parse_args(argv)
    if args.version:
        return version_line() + "\n", EXIT_OK
    if args.command is None:
        parser.error("a command is required")
    ctx = context or Context(project_dir=args.project_dir, executor=args.executor)
    outcome: Outcome = args.handler(args, ctx)
    return _render(outcome.payload, outcome.code), outcome.code


def main(
    argv: Sequence[str] | None = None,
    *,
    modules: Sequence[ModuleType] = MODULES,
    context: Context | None = None,
) -> int:
    """Run one shk command line, print its result and return the exit code."""
    try:
        text, code = _run(argv, modules, context)
    except ShkError as exc:
        text, code = _render(exc.payload(), exc.code), exc.code
    except Exception as exc:  # never swallowed: traceback on stderr, JSON error on stdout
        traceback.print_exc()
        error = {
            "ok": False,
            "error": "internal_error",
            "message": f"{type(exc).__name__}: {exc}",
            "hint": "report this with the traceback printed on stderr",
        }
        text, code = _render(error, EXIT_LOCAL), EXIT_LOCAL
    sys.stdout.write(text)
    sys.stdout.flush()
    return code
