"""Project configuration discovery and validation."""

import dataclasses
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import config, sites
from sherlock_kit.errors import ShkError

GOOD = 'project = "bioemu-ft"\nremote_root = "/scratch/users/u/bioemu-ft"\n'
MARLOWE = 'site = "marlowe"\nproject = "demo-mw"\naccount = "marlowe-m000000"\n'


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

    def test_site_and_its_host_defaults(self):
        self.assertEqual((config.DEFAULTS.site, config.DEFAULTS.account), (sites.SHERLOCK.name, ""))
        self.assertEqual(config.defaults(sites.SHERLOCK), config.DEFAULTS)
        self.assertEqual(config.parse({"site": "sherlock"}, None), config.DEFAULTS)
        found = config.parse(tomllib.loads(MARLOWE), None)
        self.assertEqual(
            (found.site, found.project, found.account, found.control_host, found.data_host),
            ("marlowe", "demo-mw", "marlowe-m000000", "marlowe-plain", "marlowe-plain"),
        )
        defaults = config.defaults(sites.MARLOWE)
        self.assertEqual(
            (defaults.site, defaults.control_host, defaults.data_host, defaults.account),
            ("marlowe", "marlowe-plain", "marlowe-plain", ""),
        )
        self.assertEqual(config.parse({"site": "marlowe"}, None), defaults)
        # Control and data host must differ only where the site's own defaults differ.
        same = config.parse({"site": "marlowe", "control_host": "mw", "data_host": "mw"}, None)
        self.assertEqual((same.control_host, same.data_host), ("mw", "mw"))
        self.invalid('control_host = "c"\ndata_host = "c"')

    def test_site_values(self):
        for text in ('site = "Marlowe"', 'site = "other"', "site = 1", 'site = ["marlowe"]'):
            with self.subTest(text=text):
                self.invalid(text)

    def test_account_only_where_the_site_has_an_account_rule(self):
        found = config.parse({"site": "marlowe", "account": "marlowe-abc12"}, None)
        self.assertEqual(found.account, "marlowe-abc12")
        self.assertEqual(config.parse({"site": "marlowe"}, None).account, "")  # submit's concern
        for account in ('"marlowe-users"', '"marlowe-m000000-pm01"', '""', "5", '"m000000"'):
            with self.subTest(account=account):
                self.invalid(f'site = "marlowe"\naccount = {account}')
        for text in ('account = "marlowe-m000000"', 'site = "sherlock"\naccount = "marlowe-m0"'):
            with self.subTest(text=text), self.assertRaises(ShkError) as caught:
                config.parse(tomllib.loads(text), Path("/p/sherlock-kit.toml"))
            self.assertEqual(caught.exception.token, "config_invalid")
            self.assertIn("takes no account", caught.exception.message)
            self.assertIn("marlowe", caught.exception.hint)

    def test_another_sites_default_alias_is_refused(self):
        for text in (
            'site = "marlowe"\ncontrol_host = "sherlock-plain"',
            'site = "marlowe"\ndata_host = "sherlock-dtn"',
            'site = "marlowe"\ncontrol_host = "sherlock-dtn"\ndata_host = "sherlock-plain"',
            'control_host = "marlowe-plain"',
            'data_host = "marlowe-plain"',
            'site = "sherlock"\ncontrol_host = "marlowe-plain"\ndata_host = "x"',
        ):
            with self.subTest(text=text):
                self.invalid(text)
        with self.assertRaises(ShkError) as caught:
            config.parse({"site": "marlowe", "control_host": "sherlock-plain"}, None)
        self.assertIn("default host of Sherlock", caught.exception.message)
        self.assertIn("remove control_host to use marlowe-plain", caught.exception.hint)
        # A consumer's Sherlock table writes both of its own hosts explicitly.
        own = {"control_host": "sherlock-plain", "data_host": "sherlock-dtn"}
        self.assertEqual(config.parse(own, None), config.DEFAULTS)
        other = config.parse({"site": "marlowe", "control_host": "mw-login"}, None)
        self.assertEqual((other.control_host, other.data_host), ("mw-login", "marlowe-plain"))

    def test_unknown_key_hint_lists_every_key(self):
        with self.assertRaises(ShkError) as caught:
            config.parse({"secret": "x"}, None)
        self.assertEqual(
            caught.exception.hint,
            "allowed: account, command_timeout, connect_timeout, control_host, "
            "cpu_thread_budget, data_host, project, remote_root, site, transfer_timeout, "
            "validators",
        )

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
