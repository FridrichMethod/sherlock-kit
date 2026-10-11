"""The shk command line: global options, subcommands, JSON output, exit codes (frozen 0.4.0).

`shk [--project-dir DIR] [--executor auto|ssh|local] [--version] COMMAND ...`.
Each sherlock_kit.<name> in COMMAND_MODULES has register(subparsers); each of its
subcommands calls set_defaults(handler=fn), fn(args, ctx) -> Outcome. doctor is
registered here. A module not written yet is skipped until STRICT_COMMAND_IMPORTS.
ctx (Context): .config (ProjectConfig | None, discovered lazily; a broken config
raises when touched); .require(*keys) -> ProjectConfig or
ShkError(1, "config_required"); .executor() (control host) and .data_executor()
(DTN; on Sherlock a LocalExecutor that refuses transfers), each detected once;
.clock() (aware UTC now, injectable); .env, .hostname, .executor_override.
Output: payload JSON on stdout (non-JSON values str()-ed) or "text" verbatim;
ShkError -> its payload and code; any other exception -> traceback on stderr,
internal_error JSON, exit 1.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib
import json
import os
import socket
import sys
import traceback

from sherlock_kit import config as project_config, doctor, executor as executors, version_line
from sherlock_kit.errors import EXIT_ANOMALY, EXIT_LOCAL, EXIT_OK, Outcome, ShkError

STRICT_COMMAND_IMPORTS = False  # SK-INT: set True once slurm, transfer and install are merged.
COMMAND_MODULES = ("slurm", "transfer", "install")
_UNSET = object()


class Context:
    def __init__(self, *, project_dir=None, executor="auto", env=None, hostname=None, clock=None, cwd=None):
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
        missing = list(keys) if found is None else [key for key in keys if getattr(found, key) in (None, "")]
        if found is None or missing:
            start = self.project_dir or self.cwd or os.getcwd()
            where = found.path if found else f"pyproject.toml/sherlock-kit.toml at or above {start}"
            raise ShkError(EXIT_LOCAL, "config_required", f"{where}: missing {', '.join(missing)}",
                           hint=f"add {', '.join(missing)} to [tool.sherlock-kit] (or pass --project-dir)")
        return found

    def _executor(self, data: bool):
        if data not in self._executors:
            self._executors[data] = executors.detect(self.config, self.executor_override, self.env,
                                                     self.hostname, data=data)
        return self._executors[data]

    def executor(self):
        return self._executor(False)

    def data_executor(self):
        return self._executor(True)

    def clock(self) -> datetime:
        return self._clock() if self._clock else datetime.now(timezone.utc)


def load_modules(names=COMMAND_MODULES) -> list:
    loaded = []
    for name in names:
        try:
            loaded.append(importlib.import_module(f"sherlock_kit.{name}"))
        except ModuleNotFoundError as exc:
            if STRICT_COMMAND_IMPORTS or exc.name != f"sherlock_kit.{name}":
                raise
    return loaded


def build_parser(modules) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shk", description="Submit and track Slurm jobs and move data on "
                                     "Sherlock. JSON on stdout; exit 0 ok, 1 local error, 2 usage, 3 refused, "
                                     "4 unreachable, 5 outcome unknown, 6 anomaly.")
    parser.add_argument("--version", action="store_true", help="print the version and where it came from")
    parser.add_argument("--project-dir", metavar="DIR", help="read the config from DIR (default: search upward)")
    parser.add_argument("--executor", choices=("auto", "ssh", "local"), default="auto",
                        help="ssh from a workstation or local on Sherlock (default: detect; env SHK_EXECUTOR)")
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")
    for module in (doctor, *modules):
        module.register(subparsers)
    return parser


def _emit(payload: dict, code: int) -> int:
    if "text" in payload:
        sys.stdout.write(payload["text"])
    else:
        body = payload if "ok" in payload else {"ok": code in (EXIT_OK, EXIT_ANOMALY), **payload}
        print(json.dumps(body, indent=2, ensure_ascii=False, default=str))
    sys.stdout.flush()
    return code


def main(argv=None, *, modules=None, context=None) -> int:
    parser = build_parser(load_modules() if modules is None else modules)
    args = parser.parse_args(argv)
    if args.version:
        print(version_line())
        return EXIT_OK
    if args.command is None:
        parser.error("a command is required")
    try:
        outcome = args.handler(args, context or Context(project_dir=args.project_dir, executor=args.executor))
        if not isinstance(outcome, Outcome):
            raise TypeError(f"{args.command} handler returned {type(outcome).__name__}, not Outcome")
    except ShkError as exc:
        return _emit(exc.payload(), exc.code)
    except Exception as exc:  # never swallowed: traceback on stderr, JSON error on stdout
        traceback.print_exc()
        return _emit({"ok": False, "error": "internal_error", "message": f"{type(exc).__name__}: {exc}",
                      "hint": "report this with the traceback printed on stderr"}, EXIT_LOCAL)
    return _emit(outcome.payload, outcome.code)
