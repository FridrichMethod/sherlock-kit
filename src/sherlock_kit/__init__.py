"""sherlock-kit: submit and track Slurm jobs and move data on Sherlock (stdlib only).

__version__ comes from a source tree's pyproject.toml (checkout, submodule, bin/shk,
editable), which wins over importlib.metadata so another installed copy cannot mislabel
it, else "0+unknown". version_line() is `shk --version`: "sherlock-kit <version> (git
<commit> | editable <path> | submodule <path> | source <path> | unknown)". Reads files only.
"""
from __future__ import annotations

from importlib import metadata
import json
from pathlib import Path
import tomllib
from urllib.parse import unquote, urlparse

DIST = "sherlock-kit"


def _source_tree(package: Path) -> tuple[Path | None, str | None]:
    try:
        with open(package.parent.parent / "pyproject.toml", "rb") as stream:
            project = tomllib.load(stream).get("project")
    except (OSError, ValueError):
        return None, None
    if package.parent.name != "src" or not isinstance(project, dict) or project.get("name") != DIST:
        return None, None
    return (package.parent.parent, project["version"]) if isinstance(project.get("version"), str) else (None, None)


def _direct_url(distribution) -> dict:
    try:
        data = json.loads(distribution.read_text("direct_url.json") or "{}") if distribution else {}
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_submodule(root: Path) -> bool:
    try:
        text = (root / ".git").read_text(encoding="utf-8") if (root / ".git").is_file() else ""
    except OSError:
        return False
    parts = Path(text.removeprefix("gitdir:").strip()).parts if text.startswith("gitdir:") else ()
    return ".git" in parts and "modules" in parts[parts.index(".git"):]


def provenance(direct_url: dict, source: Path | None) -> str:
    """Where the running code came from, given PEP 610 data and the source tree (if any)."""
    dir_info, vcs_info = (direct_url.get(k) if isinstance(direct_url.get(k), dict) else {}
                          for k in ("dir_info", "vcs_info"))
    url = urlparse(str(direct_url.get("url", "")))
    path = Path(unquote(url.path)).resolve() if url.scheme == "file" else None
    if dir_info.get("editable") is True and path and source in (None, path):
        return f"editable {path}"
    if source is not None:
        return f"{'submodule' if _is_submodule(source) else 'source'} {source}"
    commit = vcs_info.get("commit_id")
    return f"git {commit}" if isinstance(commit, str) and commit else "unknown"


try:
    _DISTRIBUTION = metadata.distribution(DIST)
except metadata.PackageNotFoundError:
    _DISTRIBUTION = None
_SOURCE, _SOURCE_VERSION = _source_tree(Path(__file__).resolve().parent)
__version__ = _SOURCE_VERSION or (_DISTRIBUTION.version if _DISTRIBUTION else "0+unknown")


def version_line() -> str:
    return f"sherlock-kit {__version__} ({provenance(_direct_url(_DISTRIBUTION), _SOURCE)})"
