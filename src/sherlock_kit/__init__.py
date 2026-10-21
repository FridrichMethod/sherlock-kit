"""sherlock-kit: submit and track Slurm jobs and move data on Sherlock (stdlib only).

version_line() is `shk --version`: "sherlock-kit <version> (git <commit> | editable <path> |
submodule <path> | source <path> | unknown)". It reads files only.
"""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

__version__ = "0.6.0"
DIST = "sherlock-kit"


def _source_tree() -> Path | None:
    """The checkout this code runs from (<root>/src/sherlock_kit with <root>/pyproject.toml)."""
    package = Path(__file__).resolve().parent
    root = package.parent.parent
    return root if package.parent.name == "src" and (root / "pyproject.toml").is_file() else None


def _direct_url() -> dict[str, Any]:
    """PEP 610 data of the installed distribution; empty when it is not installed."""
    try:
        text = metadata.distribution(DIST).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return {}
    data: dict[str, Any] = json.loads(text or "{}")
    return data


def _is_submodule(root: Path) -> bool:
    """Whether root is a git submodule checkout: its .git file points into .git/modules/."""
    git = root / ".git"
    text = git.read_text(encoding="utf-8") if git.is_file() else ""
    parts = Path(text.removeprefix("gitdir:").strip()).parts if text.startswith("gitdir:") else ()
    return ".git" in parts and "modules" in parts[parts.index(".git") :]


def provenance(direct_url: dict[str, Any], source: Path | None) -> str:
    """Where the running code came from, given PEP 610 data and the source tree (if any)."""
    url = urlparse(direct_url.get("url", ""))
    path = Path(unquote(url.path)).resolve() if url.scheme == "file" else None
    if direct_url.get("dir_info", {}).get("editable") and path and source in (None, path):
        return f"editable {path}"
    if source is not None:
        return f"{'submodule' if _is_submodule(source) else 'source'} {source}"
    commit = direct_url.get("vcs_info", {}).get("commit_id")
    return f"git {commit}" if commit else "unknown"


def version_line() -> str:
    """The `shk --version` line."""
    return f"sherlock-kit {__version__} ({provenance(_direct_url(), _source_tree())})"
