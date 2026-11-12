"""Explicit online preparation for offline packaging tests; never run by discovery.

Invoke with the prepared build interpreter, for the Python/platform being tested:
BUILD_PYTHON -B tests/prepare_wheelhouse.py --project DIR --dest DIRECTORY
"""

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path


def archive_head(project: Path, tree: Path) -> None:
    """Extract only committed project files, without importing its package."""
    archive = tree.with_suffix(".tar")
    subprocess.run(
        ["git", "-C", str(project), "archive", "--format=tar", f"--output={archive}", "HEAD"],
        check=True,
    )
    with tarfile.open(archive) as bundle:
        bundle.extractall(tree, filter="data")


def project_requirements(tree: Path, artifacts: Path, env: dict) -> tuple[list, list]:
    """Collect declared runtime requirements and static/dynamic backend requirements."""
    from build import ProjectBuilder

    def runner(cmd, cwd=None, extra_environ=None):
        subprocess.run(cmd, cwd=cwd, env={**env, **(extra_environ or {})}, check=True)

    with (tree / "pyproject.toml").open("rb") as stream:
        metadata = tomllib.load(stream)
    project = metadata["project"]
    if "dependencies" in project.get("dynamic", []):
        raise ValueError(
            "dynamic project dependencies are unsupported; declare them in pyproject.toml"
        )
    runtime = list(project.get("dependencies", []))
    builder = ProjectBuilder(str(tree), python_executable=sys.executable, runner=runner)
    backend = set(builder.build_system_requires)
    for kind in ("sdist", "wheel", "editable"):
        backend.update(builder.get_requires_for_build(kind))
    # Default `python -m build` builds the wheel from the sdist. Inspect that
    # backend too, since its requirements can differ from those in the checkout.
    artifacts.mkdir()
    sdist = builder.build("sdist", str(artifacts))
    unpacked = artifacts / "unpacked"
    with tarfile.open(sdist) as bundle:
        bundle.extractall(unpacked, filter="data")
    (source,) = unpacked.iterdir()
    sdist_builder = ProjectBuilder(str(source), python_executable=sys.executable, runner=runner)
    backend.update(sdist_builder.build_system_requires)
    backend.update(sdist_builder.get_requires_for_build("wheel"))
    return runtime, sorted(backend)


def download(requirements: list, dest: Path, constraint: Path | None, env: dict) -> None:
    """Resolve the platform's transitive closure as wheels, or fail explicitly."""
    dest.mkdir()
    if not requirements:
        return
    command = [
        sys.executable,
        "-B",
        "-m",
        "pip",
        "download",
        "--only-binary=:all:",
        "--dest",
        str(dest),
    ]
    if constraint is not None:
        command.extend(["--constraint", str(constraint)])
    subprocess.run([*command, *requirements], env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--constraint", type=Path)
    args = parser.parse_args()
    destination = args.dest.absolute()
    try:
        if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
            raise ValueError(f"refusing nonempty or non-directory destination: {destination}")
        if args.constraint is not None:
            args.constraint = args.constraint.resolve(strict=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="shk-wheelhouse-") as directory:
            temporary = Path(directory)
            home = temporary / "home"
            home.mkdir()
            env = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("PYTHON", "PIP_", "SHK_", "VIRTUAL_ENV"))
            }
            env.update(
                HOME=str(home),
                PIP_CONFIG_FILE=os.devnull,
                PIP_DISABLE_PIP_VERSION_CHECK="1",
                PIP_NO_CACHE_DIR="1",
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONNOUSERSITE="1",
            )
            tree = temporary / "source"
            archive_head(args.project.resolve(), tree)
            runtime, backend = project_requirements(tree, temporary / "artifacts", env)
            # Stage on the destination filesystem. An interrupted/failed download
            # leaves no destination that can be mistaken for a prepared closure.
            with tempfile.TemporaryDirectory(
                prefix=f".{destination.name}-", dir=destination.parent
            ) as stage:
                output = Path(stage) / "wheelhouse"
                output.mkdir()
                download(runtime, output / "runtime", args.constraint, env)
                download(backend, output / "build", args.constraint, env)
                output.replace(destination)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"wheelhouse preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
