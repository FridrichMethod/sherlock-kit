"""Embed policy and exact source revision into wheels and source distributions."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import re

from setuptools import setup
from setuptools.command.build_py import build_py
from setuptools.command.sdist import sdist

ROOT = Path(__file__).parent.resolve()


def release_identity():
    archived_revision = os.environ.get("SHERLOCK_KIT_BUILD_REVISION")
    if archived_revision is not None and not re.fullmatch(r"[0-9a-f]{40}", archived_revision):
        raise RuntimeError("SHERLOCK_KIT_BUILD_REVISION must be an exact Git revision")
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, timeout=10
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=ROOT, text=True, timeout=10,
        ).strip()
        if dirty:
            raise RuntimeError("Build a frozen release from a clean committed revision")
    except (OSError, subprocess.SubprocessError):
        frozen = ROOT / "src/sherlock_kit_data/build_identity.json"
        if archived_revision is not None:
            revision = archived_revision
        elif frozen.is_file():
            revision = json.loads(frozen.read_text())["code_revision"]
        else:
            raise RuntimeError("A Git revision or frozen source-distribution identity is required")
    if archived_revision is not None and archived_revision != revision:
        raise RuntimeError("Explicit build revision differs from the source Git revision")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise RuntimeError("Source revision must be an exact Git revision")
    return {"schema_version": 1, "code_revision": revision,
            "policy_sha256": hashlib.sha256((ROOT / "SHERLOCK.md").read_bytes()).hexdigest(),
            "install_mode": "frozen"}


def write_resources(destination):
    destination.mkdir(parents=True, exist_ok=True)
    identity = release_identity()
    (destination / "SHERLOCK.md").write_bytes((ROOT / "SHERLOCK.md").read_bytes())
    (destination / "build_identity.json").write_text(json.dumps(identity, sort_keys=True) + "\n")


class BuildPolicy(build_py):
    def run(self):
        super().run()
        write_resources(Path(self.build_lib) / "sherlock_kit_data")


class SourcePolicy(sdist):
    def make_release_tree(self, base_dir, files):
        super().make_release_tree(base_dir, files)
        write_resources(Path(base_dir) / "src/sherlock_kit_data")


setup(cmdclass={"build_py": BuildPolicy, "sdist": SourcePolicy})
