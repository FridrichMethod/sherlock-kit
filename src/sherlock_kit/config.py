"""Project configuration: [tool.sherlock-kit] in pyproject.toml, else sherlock-kit.toml (frozen 0.4.0).

discover(project_dir, cwd) reads exactly project_dir when it is given, else the nearest
directory at or above cwd that has a configuration; None when there is none. parse(table,
path) validates: bad values and unknown keys raise ShkError(1, "config_invalid").
ProjectConfig is frozen and DEFAULTS is the no-config value. Keys (no secrets):
  project      ^[a-z0-9][a-z0-9-]{0,23}$ (submit, status, cancel)
  remote_root  absolute [A-Za-z0-9_./-]+ without "..", trailing "/" dropped (submit and its
               default run dirs, push unless --anywhere)
  control_host "sherlock-plain", data_host "sherlock-dtn": distinct OpenSSH aliases
  connect_timeout 15, command_timeout 60, transfer_timeout 3600: positive integer seconds
  [validators] NAME = ["argv", ...]: `pull --validate NAME` runs argv + [stage dir]
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from sherlock_kit.errors import EXIT_LOCAL, ShkError

HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*")
PATTERNS = {
    "project": re.compile(r"[a-z0-9][a-z0-9-]{0,23}"),
    "remote_root": re.compile(r"/[A-Za-z0-9_./-]+"),
    "control_host": HOST,
    "data_host": HOST,
}
TIMEOUTS = ("connect_timeout", "command_timeout", "transfer_timeout")
KEYS = frozenset({*PATTERNS, *TIMEOUTS, "validators"})

Invalid = Callable[[str], ShkError]


@dataclass(frozen=True)
class ProjectConfig:
    """A validated configuration; project and remote_root are "" when they are not set."""

    path: Path | None = None
    project: str = ""
    remote_root: str = ""
    control_host: str = "sherlock-plain"
    data_host: str = "sherlock-dtn"
    connect_timeout: int = 15
    command_timeout: int = 60
    transfer_timeout: int = 3600
    validators: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def root(self) -> Path | None:
        """The project directory: the one holding the configuration file."""
        return self.path.parent if self.path else None


DEFAULTS = ProjectConfig()


def _validators(bad: Invalid, table: object) -> Mapping[str, tuple[str, ...]]:
    argv_lists = isinstance(table, dict) and all(
        isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv)
        for argv in table.values()
    )
    if not isinstance(table, dict) or not argv_lists:
        raise bad('validators must be a table of NAME = ["argv", ...]')
    return MappingProxyType({name: tuple(argv) for name, argv in table.items()})


def _value(bad: Invalid, key: str, value: object) -> Any:
    if key in TIMEOUTS:
        if type(value) is not int or value <= 0:
            raise bad(f"{key} must be a positive integer")
        return value
    if key == "validators":
        return _validators(bad, value)
    if not isinstance(value, str) or not PATTERNS[key].fullmatch(value):
        raise bad(f"{key} = {value!r} must match {PATTERNS[key].pattern}")
    if key == "remote_root":
        value = value.rstrip("/")
        if not value or ".." in value.split("/"):
            raise bad("remote_root must be below / and contain no '..'")
    return value


def parse(table: object, path: Path | None = None) -> ProjectConfig:
    """Validate a [tool.sherlock-kit] table read from path."""

    def bad(message: str, hint: str | None = None) -> ShkError:
        where = path or "sherlock-kit configuration"
        return ShkError(EXIT_LOCAL, "config_invalid", f"{where}: {message}", hint)

    if not isinstance(table, dict):
        raise bad("[tool.sherlock-kit] must be a table")
    unknown = set(table) - KEYS
    if unknown:
        raise bad(f"unknown keys {sorted(unknown)}", hint=f"allowed: {', '.join(sorted(KEYS))}")
    values = {key: _value(bad, key, value) for key, value in table.items()}
    found = ProjectConfig(path=path, **values)
    if found.control_host == found.data_host:
        raise bad("control_host and data_host must differ")
    return found


def _in_directory(directory: Path) -> ProjectConfig | None:
    """The configuration in directory: pyproject.toml first, then sherlock-kit.toml."""
    for name in ("pyproject.toml", "sherlock-kit.toml"):
        path = directory / name
        if not path.is_file():
            continue
        try:
            with open(path, "rb") as stream:
                data = tomllib.load(stream)
        except (OSError, ValueError) as exc:
            raise ShkError(EXIT_LOCAL, "config_invalid", f"{path}: {exc}") from None
        tool = data.get("tool") if name == "pyproject.toml" else {"sherlock-kit": data}
        if isinstance(tool, dict) and "sherlock-kit" in tool:
            return parse(tool["sherlock-kit"], path)
    return None


def discover(
    project_dir: str | Path | None = None, cwd: str | Path | None = None
) -> ProjectConfig | None:
    """The configuration of exactly project_dir, else the nearest one at or above cwd."""
    if project_dir is not None:
        directory = Path(project_dir).expanduser()
        if not directory.is_dir():
            raise ShkError(
                EXIT_LOCAL, "config_invalid", f"--project-dir {project_dir} is not a directory"
            )
        return _in_directory(directory.resolve())
    start = Path(os.getcwd() if cwd is None else cwd).resolve()
    for directory in (start, *start.parents):
        found = _in_directory(directory)
        if found is not None:
            return found
    return None
