"""Project configuration: [tool.sherlock-kit] in pyproject.toml, else sherlock-kit.toml (frozen 0.4.0).

discover(project_dir, cwd) reads exactly project_dir when it is given, else the nearest
directory at or above cwd that has a configuration; None when there is none. parse(table,
path) validates: bad values and unknown keys raise ShkError(1, "config_invalid").
ProjectConfig is frozen and DEFAULTS is the no-config value; defaults(site) is that value for a
packaged site (DEFAULTS is Sherlock's). Keys (no secrets):
  site         a packaged site name (sites.SITES); absent means "sherlock"
  project      ^[a-z0-9][a-z0-9-]{0,23}$ (submit, status, cancel)
  remote_root  absolute [A-Za-z0-9_./-]+ without "..", trailing "/" dropped (submit and its
               default run dirs, push unless --anywhere)
  account      only for a site with an account rule, which it must fullmatch ("" when not set)
  control_host, data_host: OpenSSH aliases, by default the site's ("sherlock-plain" and
               "sherlock-dtn"; both "marlowe-plain"). They must differ where the site's
               defaults differ, and neither may be a default alias of another packaged site.
  connect_timeout 15, command_timeout 60, transfer_timeout 3600: positive integer seconds
  [validators] NAME = ["argv", ...]: `pull --validate NAME` runs argv + [stage dir]
  cpu_thread_budget: optional positive per-task numerical thread defaults for submit
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

from sherlock_kit import sites
from sherlock_kit.errors import EXIT_LOCAL, ShkError

HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]*")
PATTERNS = {
    "project": re.compile(r"[a-z0-9][a-z0-9-]{0,23}"),
    "remote_root": re.compile(r"/[A-Za-z0-9_./-]+"),
    "control_host": HOST,
    "data_host": HOST,
}
TIMEOUTS = ("connect_timeout", "command_timeout", "transfer_timeout")
KEYS = frozenset({*PATTERNS, *TIMEOUTS, "validators", "cpu_thread_budget", "site", "account"})
HOST_KEYS = ("control_host", "data_host")

Invalid = Callable[..., ShkError]  # (message, hint=None) -> config_invalid


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
    cpu_thread_budget: int | None = None
    validators: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: MappingProxyType({}))
    site: str = sites.SHERLOCK.name
    account: str = ""

    @property
    def root(self) -> Path | None:
        """The project directory: the one holding the configuration file."""
        return self.path.parent if self.path else None


def defaults(site: sites.Site) -> ProjectConfig:
    """The no-config value for a packaged site: its name and default hosts."""
    return ProjectConfig(site=site.name, control_host=site.control_host, data_host=site.data_host)


DEFAULTS = defaults(sites.SHERLOCK)


def _validators(bad: Invalid, table: object) -> Mapping[str, tuple[str, ...]]:
    argv_lists = isinstance(table, dict) and all(
        isinstance(argv, list) and argv and all(isinstance(arg, str) for arg in argv)
        for argv in table.values()
    )
    if not isinstance(table, dict) or not argv_lists:
        raise bad('validators must be a table of NAME = ["argv", ...]')
    return MappingProxyType({name: tuple(argv) for name, argv in table.items()})


def _value(bad: Invalid, key: str, value: object) -> Any:
    if key == "site":
        if not isinstance(value, str) or value not in sites.SITES:
            raise bad(f"site = {value!r} must be one of {', '.join(sites.SITES)}")
        return value
    if key == "account":  # checked against the site's rule once the site is known
        return value
    if key == "cpu_thread_budget":
        if type(value) is not int or value < 1:
            raise bad("cpu_thread_budget must be a positive integer (omit to disable)")
        return value
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
    site = sites.site(values.get("site", sites.SHERLOCK.name))
    _check_account(bad, site, values)
    _check_hosts(bad, site, values)
    hosts = {"control_host": site.control_host, "data_host": site.data_host}
    found = ProjectConfig(path=path, **{**hosts, **values})
    if site.control_host != site.data_host and found.control_host == found.data_host:
        raise bad("control_host and data_host must differ")
    return found


def _check_account(bad: Invalid, site: sites.Site, values: Mapping[str, Any]) -> None:
    """Accept account only for a site with an account rule, and only fully matching it."""
    if "account" not in values:
        return
    account = values["account"]
    if site.account is None:
        named = ", ".join(name for name, other in sites.SITES.items() if other.account)
        raise bad(
            f"account is set, but site {site.name} takes no account",
            hint=f"accounts belong to site {named}: set site, or remove account",
        )
    if not isinstance(account, str) or not site.account.fullmatch(account):
        raise bad(f"account = {account!r} must match {site.account.pattern}")


def _check_hosts(bad: Invalid, site: sites.Site, values: Mapping[str, Any]) -> None:
    """No host key may name a default alias of another packaged site."""
    foreign = {
        host: other
        for other in sites.SITES.values()
        if other.name != site.name
        for host in (other.control_host, other.data_host)
    }
    for key in HOST_KEYS:
        other = foreign.get(values.get(key, ""))
        if other is not None:
            raise bad(
                f"{key} = {values[key]!r} is a default host of {other.display}, and this is a "
                f"{site.display} configuration (site {site.name})",
                hint=f"remove {key} to use {getattr(site, key)}, or set the site it belongs to",
            )


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
