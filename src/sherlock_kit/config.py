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
  cpu_thread_budget: optional positive per-task numerical thread defaults for submit
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Any

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError, with_config
from typing_extensions import TypedDict

from sherlock_kit.errors import EXIT_LOCAL, ShkError

_PositiveInt = Annotated[int, Field(gt=0)]
_Host = Annotated[str, Field(pattern=r"\A[A-Za-z0-9][A-Za-z0-9_.@-]*\z")]


@with_config(ConfigDict(strict=True, extra="forbid"))
class _ConfigTable(TypedDict, total=False):
    """Only explicitly supplied settings; ProjectConfig owns all defaults."""

    project: Annotated[str, Field(pattern=r"\A[a-z0-9][a-z0-9-]{0,23}\z")]
    remote_root: Annotated[str, Field(pattern=r"\A/[A-Za-z0-9_./-]+\z")]
    control_host: _Host
    data_host: _Host
    connect_timeout: _PositiveInt
    command_timeout: _PositiveInt
    transfer_timeout: _PositiveInt
    cpu_thread_budget: _PositiveInt
    validators: dict[str, Annotated[list[str], Field(min_length=1)]]


_CONFIG = TypeAdapter(_ConfigTable)


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

    @property
    def root(self) -> Path | None:
        """The project directory: the one holding the configuration file."""
        return self.path.parent if self.path else None


DEFAULTS = ProjectConfig()


def parse(table: object, path: Path | None = None) -> ProjectConfig:
    """Validate a [tool.sherlock-kit] table read from path."""

    def bad(message: str, hint: str | None = None) -> ShkError:
        where = path or "sherlock-kit configuration"
        return ShkError(EXIT_LOCAL, "config_invalid", f"{where}: {message}", hint)

    try:
        supplied = _CONFIG.validate_python(table)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        hint = None
        if any(error["type"] in ("extra_forbidden", "invalid_key") for error in errors):
            hint = f"allowed: {', '.join(sorted(_ConfigTable.__annotations__))}"
        message = "; ".join(
            f"{'.'.join(map(str, error['loc'])) or '[tool.sherlock-kit]'}: {error['msg']}"
            for error in errors
        )
        raise bad(message, hint) from None
    # Adapt the validated table to the public dataclass without mirroring its defaults.
    values: dict[str, Any] = dict(supplied)
    if "validators" in supplied:
        values["validators"] = MappingProxyType({
            name: tuple(argv) for name, argv in supplied["validators"].items()
        })
    if "remote_root" in supplied:
        root = supplied["remote_root"].rstrip("/")
        if not root or ".." in root.split("/"):
            raise bad("remote_root must be below / and contain no '..'")
        values["remote_root"] = root
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
