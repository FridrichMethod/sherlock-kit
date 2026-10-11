"""The shk command line: global options, subcommands, JSON output, exit codes (frozen 0.4.0).

Each sherlock_kit.<name> in COMMAND_MODULES has register(subparsers); its subcommands
set_defaults(handler=fn), fn(args, ctx) -> Outcome. Context: .config (lazy ProjectConfig | None), .require(*keys)
(else ShkError(1, "config_required")), .executor() / .data_executor() (control host /
DTN, detected once), .clock() (aware UTC, injectable), .env, .hostname. stdout is the
payload as JSON or its "text" verbatim; ShkError -> its payload and code; any other
exception (module loading, handler, rendering) -> traceback on stderr, internal_error
JSON, exit 1; usage errors stay argparse's exit 2.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import socket
import sys
import traceback
from datetime import UTC, datetime

from sherlock_kit import config as project_config
from sherlock_kit import doctor, version_line
from sherlock_kit import executor as executors
from sherlock_kit.errors import EXIT_ANOMALY, EXIT_LOCAL, EXIT_OK, Outcome, ShkError

COMMAND_MODULES = ("slurm", "transfer", "install")
_UNSET = object()


class Context:
    def __init__(
        self, *, project_dir=None, executor="auto", env=None, hostname=None, clock=None, cwd=None
    ):
        self.project_dir, self.executor_override, self.cwd = project_dir, executor, cwd
        self.env = os.environ if env is None else env
        self.hostname = socket.gethostname() if hostname is None else hostname
        self._clock, self._config, self._executors = clock, _UNSET, {}

    @property
    def config(self):
        if self._config is _UNSET:
            self._config = project_config.discover(self.project_dir, self.cwd)
        return self._config

    def require(self, *keys):
        found = self.config
        missing = (
            list(keys)
            if found is None
            else [key for key in keys if getattr(found, key) in (None, "")]
        )
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

    def _executor(self, data: bool):
        if data not in self._executors:
            self._executors[data] = executors.detect(
                self.config, self.executor_override, self.env, self.hostname, data=data
            )
        return self._executors[data]

    def executor(self):
        return self._executor(False)

    def data_executor(self):
        return self._executor(True)

    def clock(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)


def load_modules(names=COMMAND_MODULES) -> list:
    return [importlib.import_module(f"sherlock_kit.{name}") for name in names]


def build_parser(modules) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shk",
        description="Submit and track Slurm jobs and move data on "
        "Sherlock. JSON on stdout; exit 0 ok, 1 local error, 2 usage, 3 refused, "
        "4 unreachable, 5 outcome unknown, 6 anomaly.",
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
    for module in (doctor, *modules):
        module.register(subparsers)
    return parser


def _render(payload: dict, code: int) -> str:
    if "text" in payload:
        return payload["text"]
    body = payload if "ok" in payload else {"ok": code in (EXIT_OK, EXIT_ANOMALY), **payload}
    return json.dumps(body, indent=2, ensure_ascii=False, default=str) + "\n"


def _run(argv, modules, context) -> tuple[str, int]:
    parser = build_parser(load_modules() if modules is None else modules)
    args = parser.parse_args(argv)
    if args.version:
        return version_line() + "\n", EXIT_OK
    if args.command is None:
        parser.error("a command is required")
    outcome = args.handler(
        args, context or Context(project_dir=args.project_dir, executor=args.executor)
    )
    if not isinstance(outcome, Outcome):
        raise TypeError(f"{args.command} handler returned {type(outcome).__name__}, not Outcome")
    return _render(outcome.payload, outcome.code), outcome.code


def main(argv=None, *, modules=None, context=None) -> int:
    try:
        text, code = _run(argv, modules, context)
    except ShkError as exc:
        text, code = _render(exc.payload(), exc.code), exc.code
    except Exception as exc:  # never swallowed: traceback on stderr, JSON error on stdout
        traceback.print_exc()
        text, code = (
            _render(
                {
                    "ok": False,
                    "error": "internal_error",
                    "message": f"{type(exc).__name__}: {exc}",
                    "hint": "report this with the traceback printed on stderr",
                },
                EXIT_LOCAL,
            ),
            EXIT_LOCAL,
        )
    sys.stdout.write(text)
    sys.stdout.flush()
    return code
