"""Packaging from committed HEAD: sdist, wheel, wheel and editable installs, bin/shk via symlinks.

The build steps need SHK_BUILD_PYTHON, an interpreter with setuptools>=80, used read-only
with --no-build-isolation and no index, so nothing touches the network. Every
command runs with a temporary HOME; no shk command may change it (pip itself may: its
user agent runs `rustc --version`, and a rustup shim then writes ~/.rustup).
"""

import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from fakes import ROOT, in_git
from test_expired_status import assert_expiry_report, expiry_fixture

BUILD_PYTHON = os.environ.get("SHK_BUILD_PYTHON")
DATA = "sherlock_kit/data/"
CONTENT = (
    "rules/workstation.md",
    "rules/cluster.md",
    "skills/sherlock-kit/SKILL.md",
    "partitions.json",
)
VERSION = "sherlock-kit 0.5.0 "


class HeadCase(unittest.TestCase):
    """A temporary HOME and an unpacked `git archive HEAD` (committed files only)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.home, self.tree = self.tmp / "home", self.tmp / "tree"
        self.home.mkdir()
        archive = self.tmp / "head.tar"
        subprocess.run(
            ["git", "-C", str(ROOT), "archive", "--format=tar", f"--output={archive}", "HEAD"],
            check=True,
            capture_output=True,
        )
        with tarfile.open(archive) as bundle:
            bundle.extractall(self.tree, filter="tar")
        keep = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PYTHON", "PIP_", "SHK_", "VIRTUAL_ENV"))
        }
        self.env = {
            **keep,
            "HOME": str(self.home),
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_ok(self, *argv, cwd=None) -> str:
        before = sorted(self.home.rglob("*"))
        done = subprocess.run(
            [str(a) for a in argv],
            cwd=cwd or self.tmp,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        self.assertEqual(done.returncode, 0, f"{argv}\n{done.stdout}\n{done.stderr}")
        if Path(argv[0]).name == "shk":
            self.assertEqual(sorted(self.home.rglob("*")), before, f"{argv} wrote under HOME")
        return done.stdout

    def remote_programs(self) -> set:
        return {p.name for p in (self.tree / "src" / DATA / "remote").glob("*.sh")}

    def assert_cli(self, shk: Path, provenance: str):
        self.assertEqual(self.run_ok(shk, "--version"), f"{VERSION}({provenance})\n")
        rules = (self.tree / "src" / DATA / "rules/cluster.md").read_text(encoding="utf-8")
        self.assertEqual(self.run_ok(shk, "rules", "cluster"), rules)
        project = self.tmp / f"project-{shk.parent.parent.name}"
        project.mkdir()
        self.run_ok(shk, "install-skill", "--project", project)
        for rel in (".claude/skills/sherlock-kit/SKILL.md", ".agents/skills/sherlock-kit/SKILL.md"):
            self.assertIn("via shk install-skill", (project / rel).read_text(encoding="utf-8"))
        self.run_ok(shk, "install-skill", "--project", project, "--check")


@unittest.skipUnless(in_git(), "needs a git checkout to archive HEAD")
class BinShkTests(HeadCase):
    def test_bin_shk_through_a_two_hop_symlink(self):
        self.assertTrue(os.access(self.tree / "bin/shk", os.X_OK), "bin/shk lost its mode in git")
        (self.tmp / "hop1").symlink_to(self.tree / "bin/shk")
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin/shk").symlink_to(self.tmp / "hop1")
        self.assert_cli(self.tmp / "bin/shk", f"source {self.tree}")
        self.assertEqual(list(self.tree.rglob("__pycache__")), [])


@unittest.skipUnless(in_git(), "needs a git checkout to archive HEAD")
@unittest.skipUnless(BUILD_PYTHON, "set SHK_BUILD_PYTHON to an interpreter with setuptools>=80")
class BuildTests(HeadCase):
    def build(self) -> tuple[Path, Path]:
        dist = self.tmp / "dist"
        self.run_ok(
            BUILD_PYTHON,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            dist,
            self.tree,
        )
        self.run_ok(
            BUILD_PYTHON,
            "-c",
            "import setuptools.build_meta as b, sys; b.build_sdist(sys.argv[1])",
            dist,
            cwd=self.tree,
        )
        (wheel,), (sdist,) = dist.glob("*.whl"), dist.glob("*.tar.gz")
        return wheel, sdist

    def venv(self, name: str) -> Path:
        self.run_ok(BUILD_PYTHON, "-m", "venv", self.tmp / name)
        return self.tmp / name / "bin"

    def test_wheel_and_sdist_contents(self):
        wheel, sdist = self.build()
        with zipfile.ZipFile(wheel) as bundle:
            names = set(bundle.namelist())
        for rel in (*CONTENT, *(f"remote/{n}" for n in self.remote_programs())):
            self.assertIn(DATA + rel, names)
        self.assertTrue(self.remote_programs())
        self.assertFalse([
            n for n in names if not n.startswith(("sherlock_kit/", "sherlock_kit-0.5.0.dist-info/"))
        ])
        with tarfile.open(sdist) as bundle:
            members = {m.name.split("/", 1)[1]: m for m in bundle.getmembers() if "/" in m.name}
        for rel in (  # with the files test_release_metadata reads, so the suite runs from it
            "bin/shk",
            "LICENSE",
            "CHANGELOG.md",
            "README.md",
            "CONTRIBUTING.md",
            "AGENTS.md",
            "docs/design.md",
            *(f"src/{DATA}{c}" for c in CONTENT),
        ):
            self.assertIn(rel, members)
        self.assertEqual(members["bin/shk"].mode & 0o111, 0o111)

    def test_wheel_install(self):
        wheel, _ = self.build()
        bin_dir = self.venv("wheel-env")
        self.run_ok(bin_dir / "python", "-m", "pip", "install", "--no-deps", wheel)
        self.assert_cli(bin_dir / "shk", "unknown")

    def test_wheel_status_expired_ids(self):
        wheel, _ = self.build()
        bin_dir = self.venv("expiry-wheel-env")
        self.run_ok(bin_dir / "python", "-m", "pip", "install", "--no-deps", wheel)
        env, state = expiry_fixture(self.tmp)
        self.env.update(env)
        report = json.loads(
            self.run_ok(
                bin_dir / "shk",
                "--executor",
                "local",
                "status",
                "--job-id",
                "44",
                "--job-id",
                "66",
                "--job-id",
                "55",
                "--details",
            )
        )
        assert_expiry_report(self, report, state)

    def test_editable_install(self):
        bin_dir = self.venv("editable-env")
        purelib = self.run_ok(
            BUILD_PYTHON, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"
        )
        site = self.run_ok(
            bin_dir / "python", "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"
        )
        tools = (
            Path(site.strip()) / "shk-build-tools.pth"
        )  # setuptools for --no-build-isolation, read-only
        tools.write_text(purelib)
        self.run_ok(
            bin_dir / "python",
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-build-isolation",
            "-e",
            self.tree,
        )
        tools.unlink()
        self.assert_cli(bin_dir / "shk", f"editable {self.tree}")


if __name__ == "__main__":
    unittest.main()
