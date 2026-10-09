"""Packaged partition profiles: agreed flags, integrity hash, validation and summary."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from types import MappingProxyType
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import sherlock_partitions as partitions

TABLE = ROOT / "src/sherlock_kit_data/partitions.json"
EXPECTED_FLAGS = {
    "normal": {"preemptible": False, "requeue": False, "borrowed": False, "gpus_allowed": False},
    "owners": {"preemptible": True, "requeue": True, "borrowed": False, "gpus_allowed": True},
    "btrippe": {"preemptible": False, "requeue": False, "borrowed": True, "gpus_allowed": True},
}
LINE = re.compile(r"`[A-Za-z0-9_.-]+`: preemptible=(yes|no), requeue=(yes|no), borrowed=(yes|no), gpus=(yes|no)\.( \S.*)?")


def well_formed():
    return json.loads(TABLE.read_text())


class PartitionProfileTests(unittest.TestCase):
    def test_agreed_flags_per_profile(self):
        profiles = partitions.partition_profiles()
        self.assertEqual(set(profiles), set(EXPECTED_FLAGS))
        for name, flags in EXPECTED_FLAGS.items():
            profile = profiles[name]
            self.assertEqual(set(profile), {*flags, "courtesy"}, name)
            self.assertEqual({key: profile[key] for key in flags}, flags, name)
            self.assertIsInstance(profile["courtesy"], str)
            self.assertEqual(partitions.partition_profile(name), profile)
        self.assertEqual(profiles["normal"]["courtesy"], "")
        self.assertIn("checkpoint", profiles["owners"]["courtesy"])
        self.assertIn("shk occupancy", profiles["btrippe"]["courtesy"])
        self.assertIn("Borrowed", profiles["btrippe"]["courtesy"])
        for profile in profiles.values():
            self.assertNotIn("\n", profile["courtesy"])

    def test_sha256_and_text_match_committed_bytes(self):
        raw = TABLE.read_bytes()
        self.assertEqual(partitions.partitions_text(), raw.decode("utf-8"))
        self.assertEqual(partitions.partitions_sha256(), hashlib.sha256(raw).hexdigest())
        self.assertRegex(partitions.partitions_sha256(), r"\A[0-9a-f]{64}\Z")
        self.assertEqual(json.loads(partitions.partitions_text())["schema_version"], partitions.SCHEMA_VERSION)
        self.assertEqual(partitions.SCHEMA_VERSION, 1)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"max_", raw)

    def test_profiles_are_immutable_but_copyable(self):
        profiles = partitions.partition_profiles()
        self.assertIsInstance(profiles, MappingProxyType)
        with self.assertRaises(TypeError):
            profiles["gpu"] = {}
        for name in EXPECTED_FLAGS:
            profile = profiles[name]
            self.assertIsInstance(profile, MappingProxyType)
            with self.assertRaises(TypeError):
                profile["requeue"] = True
            with self.assertRaises(TypeError):
                del profile["courtesy"]
            frozen = dict(profile)
            frozen["requeue"] = not frozen["requeue"]
            self.assertNotEqual(frozen["requeue"], profiles[name]["requeue"])
            self.assertEqual(json.loads(json.dumps(dict(profile))), dict(profile))
        self.assertEqual(partitions.partition_profiles(), profiles)

    def test_unknown_partition_refused(self):
        self.assertTrue(issubclass(partitions.PartitionError, ValueError))
        for name in ("gpu", "", "normal ", "Normal", "owners\n", "btrippe;id", 42, None, ["normal"]):
            with self.assertRaises(partitions.PartitionError) as caught:
                partitions.partition_profile(name)
            self.assertIn("unknown partition", str(caught.exception))

    def test_malformed_tables_rejected(self):
        def table(**changes):
            payload = well_formed()
            payload.update(changes)
            return payload

        def profile(name="normal", **changes):
            payload = well_formed()
            entry = dict(payload["partitions"][name])
            for key, value in changes.items():
                if value is partitions.PartitionError:
                    del entry[key]
                else:
                    entry[key] = value
            payload["partitions"][name] = entry
            return payload

        def renamed(name):
            payload = well_formed()
            payload["partitions"][name] = payload["partitions"].pop("normal")
            return payload

        valid = partitions.parse_partitions(json.dumps(well_formed()))
        self.assertEqual(valid, partitions.partition_profiles())
        bad = {
            "schema 2": table(schema_version=2),
            "schema string": table(schema_version="1"),
            "schema bool": table(schema_version=True),
            "missing schema": {"partitions": well_formed()["partitions"]},
            "missing partitions": {"schema_version": 1},
            "extra top-level key": table(max_jobs=2),
            "payload list": [well_formed()],
            "partitions list": table(partitions=[]),
            "no partitions": table(partitions={}),
            "profile not a mapping": table(partitions={"normal": ["preemptible"]}),
            "missing flag": profile(requeue=partitions.PartitionError),
            "missing courtesy": profile(courtesy=partitions.PartitionError),
            "extra key": profile(max_jobs=2),
            "extra cap": profile(max_walltime_seconds=7200),
            "string flag": profile(preemptible="yes"),
            "int flag": profile(gpus_allowed=1),
            "zero flag": profile(borrowed=0),
            "null flag": profile(requeue=None),
            "courtesy int": profile(courtesy=0),
            "courtesy null": profile(courtesy=None),
            "courtesy newline": profile(courtesy="first line\nsecond line"),
            "courtesy control": profile(courtesy="tab\tseparated"),
            "name with space": renamed("gpu partition"),
            "name with slash": renamed("a/b"),
            "name with shell": renamed("normal;id"),
            "empty name": renamed(""),
            "unicode name": renamed("normál"),
        }
        for label, payload in bad.items():
            with self.subTest(label):
                with self.assertRaises(partitions.PartitionError):
                    partitions.parse_partitions(json.dumps(payload))
        for label, text in {
            "invalid json": "{",
            "empty text": "",
            "duplicate name": '{"schema_version": 1, "partitions": {"normal": %s, "normal": %s}}' % (
                json.dumps(well_formed()["partitions"]["normal"]), json.dumps(well_formed()["partitions"]["owners"])),
            "duplicate flag": '{"schema_version": 1, "partitions": {"normal": {"preemptible": false, "preemptible": true, "requeue": false, "borrowed": false, "gpus_allowed": false, "courtesy": ""}}}',
            "bytes": b'{"schema_version": 1, "partitions": {}}',
        }.items():
            with self.subTest(label):
                with self.assertRaises(partitions.PartitionError):
                    partitions.parse_partitions(text)

    def test_summary_lines_deterministic(self):
        lines = partitions.partition_summary_lines()
        self.assertEqual(lines, partitions.partition_summary_lines())
        self.assertEqual(lines, [
            "`btrippe`: preemptible=no, requeue=no, borrowed=yes, gpus=yes. "
            + partitions.partition_profile("btrippe")["courtesy"],
            "`normal`: preemptible=no, requeue=no, borrowed=no, gpus=no.",
            "`owners`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. "
            + partitions.partition_profile("owners")["courtesy"],
        ])
        for line in lines:
            self.assertRegex(line, LINE)
            self.assertNotIn("\n", line)
        self.assertEqual([line.split("`")[1] for line in lines], sorted(EXPECTED_FLAGS))

    def test_loader_is_standalone_and_importable_from_source_tree(self):
        tree = ast.parse((ROOT / "src/sherlock_partitions.py").read_text())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertFalse({name for name in imported if name.startswith("sherlock")}, imported)
        env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
        env["PYTHONPATH"] = str(ROOT / "src")
        output = subprocess.check_output(
            [sys.executable, "-P", "-c",
             "import sherlock_partitions as p, json; print(json.dumps([p.partitions_sha256(), sorted(p.partition_profiles())]))"],
            cwd=ROOT, env=env, text=True, timeout=30)
        self.assertEqual(json.loads(output), [partitions.partitions_sha256(), sorted(EXPECTED_FLAGS)])


if __name__ == "__main__":
    unittest.main()
