"""Offline packaging from committed HEAD with isolated runtime-only installations.

SHK_BUILD_PYTHON has the build frontend and backend requirements; it is used read-only.
SHK_TEST_WHEELHOUSE supplies runtime/ and build/ wheel closures prepared explicitly
outside the suite. Every subprocess uses temporary HOME and disabled pip configuration.
"""

import json
import os
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

from fakes import ROOT, in_git
from packaging.requirements import Requirement
from test_expired_status import assert_expiry_report, expiry_fixture

BUILD_PYTHON = os.environ.get("SHK_BUILD_PYTHON")
WHEELHOUSE = os.environ.get("SHK_TEST_WHEELHOUSE")
DATA = "sherlock_kit/data/"
CONTENT = (
    "rules/workstation.md",
    "rules/cluster.md",
    "skills/sherlock-kit/SKILL.md",
    "partitions.json",
)
VERSION = "sherlock-kit 0.6.0 "


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
            if not k.startswith(("PYTHON", "PIP_", "SHK_", "VIRTUAL_ENV", "SHERLOCK_KIT_"))
        }
        self.env = {
            **keep,
            "HOME": str(self.home),
            "PIP_NO_INDEX": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "PYTHONNOUSERSITE": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_command(self, *argv, cwd=None):
        return subprocess.run(
            [str(a) for a in argv],
            cwd=cwd or self.tmp,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def venv(self, name: str) -> Path:
        self.run_ok(BUILD_PYTHON, "-B", "-m", "venv", self.tmp / name)
        bin_dir = self.tmp / name / "bin"
        # Python 3.11's ensurepip also seeds setuptools; runtime targets must not
        # borrow a backend merely because an older venv happened to include it.
        self.run_ok(bin_dir / "python", "-B", "-m", "pip", "uninstall", "--yes", "setuptools")
        return bin_dir

    def runtime_requirements(self) -> list:
        with (self.tree / "pyproject.toml").open("rb") as stream:
            return tomllib.load(stream)["project"].get("dependencies", [])

    def links(self, *, build=False) -> list:
        links = ["--no-index", "--find-links", str(Path(WHEELHOUSE) / "runtime")]
        if build:
            links.extend(["--find-links", str(Path(WHEELHOUSE) / "build")])
        return links

    def install(self, bin_dir: Path, artifact: Path, *, editable=False):
        self.run_ok(
            bin_dir / "python",
            "-B",
            "-m",
            "pip",
            "install",
            *self.links(build=editable or artifact.suffix != ".whl"),
            *(["--editable"] if editable else []),
            artifact,
        )
        self.run_ok(bin_dir / "python", "-B", "-m", "pip", "check")
        self.assert_runtime_only(bin_dir)

    def assert_runtime_only(self, bin_dir: Path, *, installed=True):
        code = (
            "import importlib.metadata as m, json; "
            "print(json.dumps({d.metadata['Name'].lower().replace('_', '-'): "
            "d.requires or [] for d in m.distributions()}))"
        )
        distributions = json.loads(self.run_ok(bin_dir / "python", "-B", "-c", code))
        self.assertFalse(set(distributions) & {"build", "hypothesis", "setuptools", "wheel"})
        if installed:
            requirements = {
                Requirement(req)
                for req in distributions["sherlock-kit"]
                if Requirement(req).marker is None or "extra" not in str(Requirement(req).marker)
            }
            self.assertEqual(
                requirements, {Requirement(req) for req in self.runtime_requirements()}
            )
            self.assertFalse({req.name for req in requirements} & {"build", "hypothesis"})
        else:
            self.assertNotIn("sherlock-kit", distributions)

    def run_ok(self, *argv, cwd=None) -> str:
        before = sorted(self.home.rglob("*"))
        done = self.run_command(*argv, cwd=cwd)
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


class PreparationContractTests(unittest.TestCase):
    def test_committed_metadata_and_backend_hooks_determine_requirements(self):
        # Import only during execution; discovery never runs preparation.
        from prepare_wheelhouse import archive_head, project_requirements

        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            project = tmp / "project"
            project.mkdir()
            metadata = project / "pyproject.toml"
            metadata.write_text(
                '[build-system]\nrequires = ["setuptools>=80"]\n'
                'build-backend = "backend"\nbackend-path = ["."]\n'
                '[project]\nname = "example"\nversion = "1"\n'
                'dependencies = ["example-runtime>=1"]\n'
            )
            (project / "backend.py").write_text(
                "import pathlib, tarfile\n"
                "def get_requires_for_build_sdist(config_settings=None):\n"
                "    return ['wheel>=0.45']\n"
                "def get_requires_for_build_editable(config_settings=None):\n"
                "    return ['packaging>=24']\n"
                "def get_requires_for_build_wheel(config_settings=None):\n"
                "    return ['pyproject-hooks>=1'] if pathlib.Path('from-sdist').exists() "
                "else ['typing-extensions>=4']\n"
                "def build_sdist(sdist_directory, config_settings=None):\n"
                "    pathlib.Path('from-sdist').touch()\n"
                "    with tarfile.open(pathlib.Path(sdist_directory) / 'example-1.tar.gz', 'w:gz') as t:\n"
                "        for name in ('pyproject.toml', 'backend.py', 'from-sdist'):\n"
                "            t.add(name, arcname='example-1/' + name)\n"
                "    return 'example-1.tar.gz'\n"
            )
            for args in (
                ("init", "-q"),
                ("add", "pyproject.toml", "backend.py"),
                (
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "-qm",
                    "fixture",
                ),
            ):
                subprocess.run(["git", "-C", str(project), *args], check=True, capture_output=True)
            metadata.write_text(metadata.read_text().replace("example-runtime", "uncommitted"))
            tree = tmp / "archive"
            archive_head(project, tree)
            runtime, backend = project_requirements(tree, tmp / "artifacts", dict(os.environ))
            self.assertEqual(runtime, ["example-runtime>=1"])
            self.assertEqual(
                backend,
                [
                    "packaging>=24",
                    "pyproject-hooks>=1",
                    "setuptools>=80",
                    "typing-extensions>=4",
                    "wheel>=0.45",
                ],
            )
            # A project without runtime dependencies still needs its backend closure.
            archived_metadata = tree / "pyproject.toml"
            archived_metadata.write_text(
                archived_metadata.read_text().replace('["example-runtime>=1"]', "[]")
            )
            runtime, backend = project_requirements(tree, tmp / "empty-artifacts", dict(os.environ))
            self.assertEqual(runtime, [])
            self.assertIn("setuptools>=80", backend)


@unittest.skipUnless(in_git(), "needs a git checkout to archive HEAD")
@unittest.skipUnless(BUILD_PYTHON and WHEELHOUSE, "set SHK_BUILD_PYTHON and SHK_TEST_WHEELHOUSE")
class BinShkTests(HeadCase):
    def test_bin_shk_through_a_two_hop_symlink(self):
        bin_dir = self.venv("source-env")
        requirements = self.runtime_requirements()
        if requirements:
            self.run_ok(
                bin_dir / "python", "-B", "-m", "pip", "install", *self.links(), *requirements
            )
        self.run_ok(bin_dir / "python", "-B", "-m", "pip", "check")
        self.assert_runtime_only(bin_dir, installed=False)
        self.env["PATH"] = f"{bin_dir}{os.pathsep}{self.env['PATH']}"
        self.assertTrue(os.access(self.tree / "bin/shk", os.X_OK), "bin/shk lost its mode in git")
        (self.tmp / "hop1").symlink_to(self.tree / "bin/shk")
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin/shk").symlink_to(self.tmp / "hop1")
        self.assert_cli(self.tmp / "bin/shk", f"source {self.tree}")
        self.assertEqual(list(self.tree.rglob("__pycache__")), [])


@unittest.skipUnless(in_git(), "needs a git checkout to archive HEAD")
@unittest.skipUnless(BUILD_PYTHON and WHEELHOUSE, "set SHK_BUILD_PYTHON and SHK_TEST_WHEELHOUSE")
class BuildTests(HeadCase):
    def build(self) -> tuple[Path, Path]:
        dist = self.tmp / "dist"
        self.run_ok(
            BUILD_PYTHON, "-B", "-m", "build", "--no-isolation", "--outdir", dist, self.tree
        )
        (wheel,), (sdist,) = dist.glob("*.whl"), dist.glob("*.tar.gz")
        return wheel, sdist

    def test_wheel_and_sdist_contents(self):
        wheel, sdist = self.build()
        with zipfile.ZipFile(wheel) as bundle:
            names = set(bundle.namelist())
        for rel in (*CONTENT, *(f"remote/{n}" for n in self.remote_programs())):
            self.assertIn(DATA + rel, names)
        self.assertTrue(self.remote_programs())
        self.assertFalse([
            n for n in names if not n.startswith(("sherlock_kit/", "sherlock_kit-0.6.0.dist-info/"))
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
        self.install(bin_dir, wheel)
        self.assert_cli(bin_dir / "shk", "unknown")

    def test_wheel_status_expired_ids(self):
        wheel, _ = self.build()
        bin_dir = self.venv("expiry-wheel-env")
        self.install(bin_dir, wheel)
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

    def test_sdist_install(self):
        _, sdist = self.build()
        bin_dir = self.venv("sdist-env")
        self.install(bin_dir, sdist)
        self.assert_cli(bin_dir / "shk", "unknown")

    def test_missing_runtime_wheel_fails_offline(self):
        if not self.runtime_requirements():
            # The runtime closure can be empty when the validation pilot is rejected.
            return
        wheel, _ = self.build()
        bin_dir = self.venv("incomplete-env")
        empty = self.tmp / "empty-runtime"
        empty.mkdir()
        result = self.run_command(
            bin_dir / "python",
            "-B",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            empty,
            wheel,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No matching distribution found", result.stderr)

    def test_editable_install(self):
        bin_dir = self.venv("editable-env")
        self.install(bin_dir, self.tree, editable=True)
        self.assert_cli(bin_dir / "shk", f"editable {self.tree}")


if __name__ == "__main__":
    unittest.main()
