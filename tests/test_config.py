"""Project configuration discovery and validation."""

import dataclasses
import os
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import config
from sherlock_kit.errors import ShkError

GOOD = 'project = "bioemu-ft"\nremote_root = "/scratch/users/u/bioemu-ft"\n'


class ConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def write(self, name, text, directory=None):
        path = (directory or self.root) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def invalid(self, text):
        with self.assertRaises(ShkError) as caught:
            config.parse(tomllib.loads(text), Path("/p/sherlock-kit.toml"))
        self.assertEqual((caught.exception.code, caught.exception.token), (1, "config_invalid"))
        self.assertIn("/p/sherlock-kit.toml", caught.exception.message)

    def test_discovery_order_and_walk_up(self):
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)
        self.assertIsNone(config.discover(cwd=nested))
        alt = self.write("sherlock-kit.toml", 'project = "alt"\n')
        self.assertEqual(config.discover(cwd=nested).path, alt)
        self.write("pyproject.toml", '[project]\nname = "x"\n')
        self.assertEqual(config.discover(cwd=nested).project, "alt")
        py = self.write("pyproject.toml", "[tool.sherlock-kit]\n" + GOOD)
        found = config.discover(cwd=nested)
        self.assertEqual((found.path, found.root, found.project), (py, self.root, "bioemu-ft"))
        self.assertEqual(found.remote_root, "/scratch/users/u/bioemu-ft")

    def test_project_dir_is_exact(self):
        self.write("pyproject.toml", "[tool.sherlock-kit]\n" + GOOD)
        child = self.root / "child"
        child.mkdir()
        self.assertIsNone(config.discover(project_dir=child))
        self.assertEqual(config.discover(project_dir=self.root).project, "bioemu-ft")
        with self.assertRaises(ShkError):
            config.discover(project_dir=self.root / "missing")

    def test_defaults_and_frozen(self):
        found = config.parse({}, None)
        self.assertEqual((found.control_host, found.data_host), ("sherlock-plain", "sherlock-dtn"))
        self.assertEqual(
            (found.connect_timeout, found.command_timeout, found.transfer_timeout), (15, 60, 3600)
        )
        self.assertIsNone(found.project)
        self.assertEqual(dict(found.validators), {})
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.project = "x"
        self.assertEqual(config.DEFAULTS, found)

    def test_validators(self):
        found = config.parse(tomllib.loads('[validators]\nbundle = ["python", "-m", "v"]\n'), None)
        self.assertEqual(found.validators["bundle"], ("python", "-m", "v"))
        with self.assertRaises(TypeError):
            found.validators["other"] = ("x",)
        for text in (
            "validators = 1",
            "[validators]\nb = []",
            '[validators]\nb = "python"',
            '[validators]\nb = ["", "x"]',
            '[validators]\n"b c" = ["x"]',
        ):
            with self.subTest(text=text):
                self.invalid(text)

    def test_grammar(self):
        for text in (
            'project = ""',
            'project = "-a"',
            'project = "Ab"',
            'project = "a_b"',
            f'project = "{"a" * 25}"',
            "project = 5",
            'remote_root = "rel/path"',
            'remote_root = "/a/../b"',
            'remote_root = "/a b"',
            'remote_root = "$SCRATCH/x"',
            'remote_root = "/"',
            'control_host = "-oProxyCommand=x"',
            'data_host = "a b"',
            'data_host = "sherlock-plain"',
            "connect_timeout = 0",
            "command_timeout = -1",
            "transfer_timeout = true",
            "command_timeout = 1.5",
            'connect_timeout = "9"',
            'secret = "x"',
            'tool = "nested"',
        ):
            with self.subTest(text=text):
                self.invalid(text)
        ok = config.parse(tomllib.loads(f'project = "{"a" * 24}"\nremote_root = "/x/y/"'), None)
        self.assertEqual(ok.remote_root, "/x/y")

    def test_unreadable_files(self):
        self.write("pyproject.toml", "[tool.sherlock-kit\n")
        with self.assertRaises(ShkError) as caught:
            config.discover(cwd=self.root)
        self.assertEqual(caught.exception.token, "config_invalid")
        self.write("pyproject.toml", '[tool]\nsherlock-kit = "x"\n')
        with self.assertRaises(ShkError):
            config.discover(cwd=self.root)

    def test_unstattable_candidates_are_errors_on_every_python(self):
        loop = self.root / "loop"
        loop.mkdir()
        (loop / "pyproject.toml").symlink_to(loop / "pyproject.toml")
        cases = [{"cwd": loop}, {"project_dir": loop}]
        if os.geteuid() != 0:  # root ignores directory modes
            locked = self.root / "locked"
            locked.mkdir(mode=0o600)
            self.addCleanup(locked.chmod, 0o700)
            cases.append({"project_dir": locked})
        for kwargs in cases:
            with self.subTest(**kwargs), self.assertRaises(ShkError) as caught:
                config.discover(**kwargs)
            self.assertEqual((caught.exception.code, caught.exception.token), (1, "config_invalid"))

    def test_walk_up_stops_at_a_directory_another_user_owns(self):
        self.write("sherlock-kit.toml", 'project = "planted"\n')
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)
        real = config._stat

        def foreign_root(path):
            found = real(path)
            if found is None or Path(path) != self.root:
                return found
            return os.stat_result([*found[:4], os.geteuid() + 1, *found[5:]])

        with patch.object(config, "_stat", foreign_root):
            self.assertIsNone(config.discover(cwd=nested))
            self.assertEqual(config.discover(project_dir=self.root).project, "planted")
            self.write("sherlock-kit.toml", 'project = "mine"\n', self.root / "a")
            self.assertEqual(config.discover(cwd=nested).project, "mine")


if __name__ == "__main__":
    unittest.main()
