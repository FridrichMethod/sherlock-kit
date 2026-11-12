"""Project configuration discovery and validation."""

import dataclasses
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

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
        self.assertEqual((found.project, found.remote_root, found.root), ("", "", None))
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
            "[validators]\nb = [1]",
        ):
            with self.subTest(text=text):
                self.invalid(text)

    def test_omitted_and_explicit_empty_or_null_values_differ(self):
        self.assertEqual(config.parse({}), config.DEFAULTS)
        self.assertIsNone(config.parse({}).cpu_thread_budget)
        for key in ("project", "remote_root", "cpu_thread_budget"):
            for value in ("", None):
                with self.subTest(key=key, value=value), self.assertRaises(ShkError):
                    config.parse({key: value})

    def test_all_integer_fields_are_strict_positive_integers(self):
        for key in ("connect_timeout", "command_timeout", "transfer_timeout", "cpu_thread_budget"):
            for value in (True, False, 1.0, 1.5, "1", 0, -1, None):
                with self.subTest(key=key, value=value), self.assertRaises(ShkError) as caught:
                    config.parse({key: value})
                self.assertEqual(
                    (caught.exception.code, caught.exception.token), (1, "config_invalid")
                )
            self.assertEqual(getattr(config.parse({key: 2}), key), 2)

    def test_regexes_require_full_matches_and_errors_retain_location_and_hint(self):
        path = Path("/p/sherlock-kit.toml")
        for key, valid in (
            ("project", "demo"),
            ("remote_root", "/x/y"),
            ("control_host", "login"),
            ("data_host", "data"),
        ):
            for suffix in ("\n", "\r", "\x00", "\t"):
                with self.subTest(key=key, suffix=suffix), self.assertRaises(ShkError):
                    config.parse({key: valid + suffix}, path)
        for key in ("path", "unknown", 1, None):
            with self.subTest(key=key), self.assertRaises(ShkError) as caught:
                config.parse({key: "x"}, path)
            self.assertEqual((caught.exception.code, caught.exception.token), (1, "config_invalid"))
            self.assertIn(str(path), caught.exception.message)
            self.assertIn("allowed:", caught.exception.hint)
            self.assertIn("cpu_thread_budget", caught.exception.hint)

    def test_validator_arguments_are_copied_and_nested_output_is_immutable(self):
        original = {"bundle": ["python", "-m", "v"]}
        found = config.parse({"validators": original})
        original["bundle"].append("changed")
        original["new"] = ["other"]
        self.assertEqual(dict(found.validators), {"bundle": ("python", "-m", "v")})
        with self.assertRaises(TypeError):
            found.validators["bundle"][0] = "changed"
        for argv in (("python",), [], [1], "python", None):
            with self.subTest(argv=argv), self.assertRaises(ShkError):
                config.parse({"validators": {"bundle": argv}})

    def test_public_dataclass_positional_keyword_equality_and_root(self):
        path = Path("/p/sherlock-kit.toml")
        found = config.ProjectConfig(path, "demo", "/x")
        self.assertEqual(found, config.ProjectConfig(path=path, project="demo", remote_root="/x"))
        self.assertEqual(found, config.parse({"project": "demo", "remote_root": "/x"}, path))
        self.assertEqual(found.root, path.parent)
        self.assertTrue(dataclasses.is_dataclass(found))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.validators = {}

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


if __name__ == "__main__":
    unittest.main()
