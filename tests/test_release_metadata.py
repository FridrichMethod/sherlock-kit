"""Release metadata ties: one version string, a projection that carries the packaged
partition profiles, operator documentation that names every private config key, and
two skill adapters that differ only in their client-specific closing paragraph."""
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import sherlock_kit as kit
import sherlock_partitions as partitions
from sherlock_commands import OPTIONAL_CONFIG, REQUIRED_CONFIG

RELEASE = "0.2.0"
CHANGELOG_HEADING = re.compile(r"^## (\S+)", re.MULTILINE)
SKILLS = ("adapters/claude/skills/sherlock-kit-operate/SKILL.md", "adapters/codex/skills/sherlock-kit-operate/SKILL.md")


class ReleaseMetadataTests(unittest.TestCase):
    def test_three_version_strings_agree(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        plugin = json.loads((ROOT / "adapters/claude/.claude-plugin/plugin.json").read_text())["version"]
        heading = CHANGELOG_HEADING.search((ROOT / "CHANGELOG.md").read_text())
        self.assertIsNotNone(heading, "CHANGELOG.md needs a '## VERSION' heading")
        self.assertEqual({"pyproject": project, "plugin": plugin, "changelog": heading.group(1)},
                         {"pyproject": RELEASE, "plugin": RELEASE, "changelog": RELEASE})

    def test_projection_lists_every_packaged_profile_verbatim(self):
        projection = kit.policy_projection()
        lines = partitions.partition_summary_lines()
        self.assertEqual(len(lines), len(partitions.partition_profiles()))
        for line in lines:
            with self.subTest(line=line[:line.index(":")]):
                # One sub-bullet per profile, the summary line unbroken so a reader and
                # the loader agree byte for byte on what each partition permits.
                self.assertIn("\n  - " + line + "\n", projection)
        for name in partitions.partition_profiles():
            self.assertEqual(projection.count(f"`{name}`:"), 1, name)

    def test_projection_metadata_line_is_generated_not_stored(self):
        identity = kit.policy_identity()
        policy = kit.policy_text()
        self.assertEqual(identity["policy_sha256"], hashlib.sha256(policy.encode("utf-8")).hexdigest())
        self.assertEqual(policy, (ROOT / "SHERLOCK.md").read_text())
        metadata = "<!-- source: SHERLOCK.md; schema_version: 1; policy_sha256: " + identity["policy_sha256"] + " -->"
        self.assertEqual(kit.policy_projection().splitlines()[1], metadata)
        self.assertNotIn("<!-- source: SHERLOCK.md", policy)

    def test_orchestration_guide_names_every_private_config_key(self):
        text = (ROOT / "docs/orchestration.md").read_text()
        keys = REQUIRED_CONFIG | OPTIONAL_CONFIG
        self.assertTrue(REQUIRED_CONFIG and OPTIONAL_CONFIG and not REQUIRED_CONFIG & OPTIONAL_CONFIG)
        for key in sorted(keys):
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", text)

    def test_skill_adapters_share_everything_but_the_closing_paragraph(self):
        claude, codex = ((ROOT / path).read_text().rstrip("\n").split("\n\n") for path in SKILLS)
        self.assertGreater(len(claude), 2)
        self.assertEqual(claude[:-1], codex[:-1])
        self.assertNotEqual(claude[-1], codex[-1])
        self.assertIn("hook", claude[-1])


if __name__ == "__main__":
    unittest.main()
