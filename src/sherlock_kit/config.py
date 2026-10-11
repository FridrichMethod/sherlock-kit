"""Project configuration: [tool.sherlock-kit] in pyproject.toml, else sherlock-kit.toml (frozen 0.4.0).

discover(project_dir=None, cwd=None) reads exactly project_dir (explicit, trusted), else
walks up from cwd until a directory the user does not own (as git's safe.directory);
None when nothing is found. parse(table, path) validates; bad values, unknown keys and
unstattable candidates raise ShkError(1, "config_invalid"). ProjectConfig is frozen;
DEFAULTS is the no-config value. Keys (no secrets):
  project      ^[a-z0-9][a-z0-9-]{0,23}$ (submit/status/cancel)
  remote_root  absolute [A-Za-z0-9_./-]+ without "..", trailing "/" dropped (submit/push/pull)
  control_host "sherlock-plain", data_host "sherlock-dtn": distinct OpenSSH aliases
  connect_timeout 15, command_timeout 60, transfer_timeout 3600: positive integer seconds
  [validators] NAME = ["argv", ...]: `pull --validate NAME` runs argv + [stage dir]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import takewhile
import os
from pathlib import Path
import re
import stat
import tomllib
from types import MappingProxyType

from sherlock_kit.errors import EXIT_LOCAL, ShkError

PATTERNS = {"project": re.compile(r"[a-z0-9][a-z0-9-]{0,23}"), "remote_root": re.compile(r"/[A-Za-z0-9_./-]+"),
            "control_host": re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*")}
PATTERNS["data_host"] = HOST = PATTERNS["control_host"]
TIMEOUTS = ("connect_timeout", "command_timeout", "transfer_timeout")
KEYS = frozenset({*PATTERNS, *TIMEOUTS, "validators"})


@dataclass(frozen=True)
class ProjectConfig:
    path: Path | None = None
    project: str | None = None
    remote_root: str | None = None
    control_host: str = "sherlock-plain"
    data_host: str = "sherlock-dtn"
    connect_timeout: int = 15
    command_timeout: int = 60
    transfer_timeout: int = 3600
    validators: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))

    @property
    def root(self) -> Path | None:
        return self.path.parent if self.path else None


DEFAULTS = ProjectConfig()


def _validators(bad, table) -> MappingProxyType:
    if not isinstance(table, dict):
        raise bad("validators must be a table of argv lists")
    for name, argv in table.items():
        if (not re.fullmatch(r"[A-Za-z0-9_.-]+", name) or not isinstance(argv, list) or not argv
                or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv)):
            raise bad(f"validator {name!r} must be a NAME = [non-empty argv strings]")
    return MappingProxyType({name: tuple(argv) for name, argv in table.items()})


def _value(bad, key, value):
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


def parse(table, path: Path | None = None) -> ProjectConfig:
    def bad(message, hint=None):
        return ShkError(EXIT_LOCAL, "config_invalid", f"{path or 'sherlock-kit configuration'}: {message}", hint)
    if not isinstance(table, dict):
        raise bad("[tool.sherlock-kit] must be a table")
    if set(table) - KEYS:
        raise bad(f"unknown keys {sorted(set(table) - KEYS)}", hint=f"allowed: {', '.join(sorted(KEYS))}")
    found = ProjectConfig(path=path, **{key: _value(bad, key, value) for key, value in table.items()})
    if found.control_host == found.data_host:
        raise bad("control_host and data_host must differ")
    return found


def _stat(path: Path) -> os.stat_result | None:  # Path.is_file() swallows EACCES on 3.13+ only
    try:
        return os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        raise ShkError(EXIT_LOCAL, "config_invalid", f"{path}: {exc.strerror or exc}") from None


def _in_directory(directory: Path) -> ProjectConfig | None:
    for name in ("pyproject.toml", "sherlock-kit.toml"):
        path = directory / name
        if not stat.S_ISREG(getattr(_stat(path), "st_mode", 0)):
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


def discover(project_dir=None, cwd=None) -> ProjectConfig | None:
    if project_dir is not None:
        if not stat.S_ISDIR(getattr(_stat(Path(project_dir).expanduser()), "st_mode", 0)):
            raise ShkError(EXIT_LOCAL, "config_invalid", f"--project-dir {project_dir} is not a directory")
        return _in_directory(Path(project_dir).expanduser().resolve())
    start = Path(os.getcwd() if cwd is None else cwd).resolve()
    mine = takewhile(lambda d: getattr(_stat(d), "st_uid", None) == os.geteuid(), (start, *start.parents))
    return next((found for d in mine if (found := _in_directory(d)) is not None), None)
