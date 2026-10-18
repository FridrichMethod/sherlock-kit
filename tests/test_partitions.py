"""Packaged partition table (schema 1, 6-key profiles) and the generated rules block."""

import json
import sys
import unittest

from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import partitions
from sherlock_kit.errors import ShkError

NAMES = ["normal", "bigmem", "dev", "service", "gpu", "owners", "bioe", "stat", "btrippe", "possu"]
KEYS = {"preemptible", "requeue", "borrowed", "gpus_allowed", "batch", "courtesy"}


class PartitionTableTests(unittest.TestCase):
    def test_packaged_table_shape(self):
        raw = json.loads((SRC / "sherlock_kit/data/partitions.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["schema_version"], 1)
        self.assertEqual(list(raw["partitions"]), NAMES)
        for name, entry in raw["partitions"].items():
            self.assertEqual(set(entry), KEYS, name)
        self.assertEqual(list(partitions.profiles()), NAMES)

    def test_flags(self):
        p = partitions.profiles()
        self.assertTrue(p["owners"].preemptible and p["owners"].requeue)
        self.assertEqual([n for n in NAMES if p[n].requeue], ["owners"])
        self.assertEqual([n for n in NAMES if p[n].borrowed], ["btrippe", "possu"])
        self.assertEqual(
            [n for n in NAMES if not p[n].gpus_allowed], ["normal", "bigmem", "service"]
        )
        self.assertIn("between 00:00 and 07:00 Pacific", p["possu"].courtesy)
        self.assertEqual(p["normal"].courtesy, "")
        # Sherlock refuses every sbatch job in dev (interactive sessions only, seen 2026-10-18)
        self.assertEqual([n for n in NAMES if not p[n].batch], ["dev"])
        self.assertIn("`sh_dev`", p["dev"].courtesy)

    def test_profile_lookup(self):
        self.assertEqual(partitions.profile("gpu").name, "gpu")
        with self.assertRaises(ShkError) as caught:
            partitions.profile("borrowed-guess")
        self.assertEqual((caught.exception.code, caught.exception.token), (3, "unknown_partition"))
        self.assertIn("normal", caught.exception.message)
        self.assertIsNone(partitions.profiles().get("borrowed-guess"))

    def test_read_only(self):
        with self.assertRaises(TypeError):
            partitions.profiles()["normal"] = None
        with self.assertRaises(AttributeError):
            partitions.profile("normal").requeue = True


class MarkdownTests(unittest.TestCase):
    def test_block(self):
        text = partitions.markdown()
        self.assertEqual(text, partitions.markdown())
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
        lines = text.splitlines()
        self.assertEqual(lines[0], "### Partition profiles")
        self.assertEqual(
            lines[1],
            "`shk` refuses partitions not listed here. Requeue only where `requeue=yes`. "
            "Borrowed partitions need `shk occupancy` first and `--courtesy-ack`.",
        )
        self.assertEqual(len(lines), 2 + len(NAMES))
        self.assertEqual([line.split("`")[1] for line in lines[2:]], NAMES)
        self.assertEqual(lines[2], "- `normal`: preemptible=no, requeue=no, borrowed=no, gpus=no.")
        owners = partitions.profile("owners")
        owners_line = (
            "- `owners`: preemptible=yes, requeue=yes, borrowed=no, gpus=yes. " + owners.courtesy
        )
        self.assertEqual(lines[7], owners_line)
        self.assertNotIn("\n\n", text)


if __name__ == "__main__":
    unittest.main()
