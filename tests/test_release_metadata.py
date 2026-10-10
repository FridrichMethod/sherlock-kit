"""Release metadata ties: one version string, a projection that carries the packaged
partition profiles, operator documentation that names every private config key and
none of the removed ledger keys, one changelog entry for this release, two skill
adapters that differ only in their client-specific closing paragraph, and a remote
registry program whose duplicated constants and shipped bytes match the library."""
import base64
import hashlib
import json
from pathlib import Path
import re
import sys
import tomllib
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import sherlock_kit as kit
import sherlock_orchestration as orchestration
import sherlock_partitions as partitions
import sherlock_registry as registry
from sherlock_commands import OPTIONAL_CONFIG, REQUIRED_CONFIG

VERSION = re.compile(r"\d+\.\d+\.\d+")
CHANGELOG_HEADING = re.compile(r"^## (\S+)", re.MULTILINE)
FENCED_JSON = re.compile(r"^```json\n(.*?)^```", re.MULTILINE | re.DOTALL)
# A line of docs/orchestration.md may name a removed key only when that same line also
# says it is refused, removed or a 0.2.0 migration concern; the rule is per line, so a
# sentence whose removal word falls on the next line is rejected (see the test docstring).
REMOVAL_CONTEXT = re.compile(r"refus|0\.2\.0|migrat|remov|no longer|dropped|not a config", re.IGNORECASE)
REMOVED_KEYS = ("state_root", "limits")
# Plain-token needles the changelog entry of each release must contain; markdown
# formatting around a token (code spans, wrapping) is the author's choice. Bumping the
# version means adding its row here.
RELEASE_NEEDLES = {"0.3.0": ("registry_root", "--abandon", "array")}
SKILLS = ("adapters/claude/skills/sherlock-kit-operate/SKILL.md", "adapters/codex/skills/sherlock-kit-operate/SKILL.md")
SAMPLE = {"z": [1, 2.5, {"b": None, "a": "λ   text"}], "a": {"nested": [True, False, "x"]}, "n": 10 ** 20}


def changelog_sections():
    text = (ROOT / "CHANGELOG.md").read_text()
    starts = [match.start() for match in CHANGELOG_HEADING.finditer(text)] + [len(text)]
    return [text[a:b] for a, b in zip(starts, starts[1:])]


class ReleaseMetadataTests(unittest.TestCase):
    def test_three_version_strings_agree(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        plugin = json.loads((ROOT / "adapters/claude/.claude-plugin/plugin.json").read_text())["version"]
        heading = CHANGELOG_HEADING.search((ROOT / "CHANGELOG.md").read_text())
        self.assertIsNotNone(heading, "CHANGELOG.md needs a '## VERSION' heading")
        self.assertEqual({project, plugin, heading.group(1)}, {project}, "pyproject, plugin manifest and changelog versions differ")
        self.assertRegex(project, VERSION)

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
        self.assertIn("registry_root", REQUIRED_CONFIG)
        self.assertFalse(set(REMOVED_KEYS) & keys)
        for key in sorted(keys):
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", text)

    def test_orchestration_guide_does_not_offer_the_removed_ledger_keys(self):
        """`state_root` and `limits` are refused by the CLI; the guide may only mention them as removed or refused.

        Rule for the docs author: every line that contains a backticked removed key must
        itself match ``REMOVAL_CONTEXT`` (refus / 0.2.0 / migrat / remov / no longer /
        dropped / not a config). Keep the key and its removal word on one line; a
        sentence wrapped so the removal word lands on the next line fails the check.
        Every fenced JSON example that looks like a private configuration must use the
        current key set and name ``registry_root``.
        """
        text = (ROOT / "docs/orchestration.md").read_text()
        for key in REMOVED_KEYS:
            for number, line in enumerate(text.splitlines(), 1):
                if f"`{key}`" in line:
                    with self.subTest(key=key, line=number):
                        self.assertRegex(line, REMOVAL_CONTEXT, f"docs/orchestration.md:{number} presents `{key}` as a live config key")
        examples = [json.loads(block) for block in FENCED_JSON.findall(text) if block.lstrip().startswith("{")]
        configs = [example for example in examples if isinstance(example, dict) and "schema_version" in example and "principal" in example]
        self.assertTrue(configs, "docs/orchestration.md needs a fenced JSON private-configuration example")
        for example in configs:
            with self.subTest(keys=sorted(example)):
                self.assertIn("registry_root", example)
                self.assertFalse(set(example) - (REQUIRED_CONFIG | OPTIONAL_CONFIG), "example uses keys the CLI refuses")
                self.assertFalse(set(example) & set(REMOVED_KEYS))

    def test_changelog_has_exactly_one_entry_for_this_release(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
        sections = [section for section in changelog_sections() if CHANGELOG_HEADING.match(section).group(1) == project]
        self.assertEqual(len(sections), 1, f"CHANGELOG.md needs exactly one '## {project}' entry")
        entry = sections[0]
        self.assertIn(project, RELEASE_NEEDLES, "add this release's changelog needles to RELEASE_NEEDLES")
        # Token checks, not formatting checks: `--abandon` inside a longer code span counts.
        for needle in RELEASE_NEEDLES[project]:
            with self.subTest(needle=needle):
                self.assertIn(needle, entry)

    def test_skill_adapters_share_everything_but_the_closing_paragraph(self):
        claude, codex = ((ROOT / path).read_text().rstrip("\n").split("\n\n") for path in SKILLS)
        self.assertGreater(len(claude), 2)
        self.assertEqual(claude[:-1], codex[:-1])
        self.assertNotEqual(claude[-1], codex[-1])
        self.assertIn("hook", claude[-1])

    def test_registry_program_duplicates_the_orchestration_contract(self):
        """The standalone remote program re-declares a few constants; they must equal the library's."""
        for name in ("BASE_TERMINAL", "PREEMPTION_STATES", "SUBMIT_TIME_TOLERANCE_SECONDS", "PROFILE_FLAGS"):
            with self.subTest(constant=name):
                self.assertEqual(getattr(registry, name), getattr(orchestration, name))
        self.assertEqual(registry.canonical(SAMPLE), orchestration.canonical(SAMPLE))
        self.assertEqual(registry.digest(SAMPLE), orchestration.digest(SAMPLE))
        self.assertEqual(registry.digest(SAMPLE), hashlib.sha256(orchestration.canonical(SAMPLE).encode()).hexdigest())
        for requeue in (False, True):
            for profile in ({"preemptible": True}, {"preemptible": False}):
                with self.subTest(requeue=requeue, profile=profile):
                    self.assertEqual(registry.terminal_states({"requeue": requeue}, profile), orchestration.terminal_states({"requeue": requeue}, profile))
        for value in (float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                registry.canonical(value)
            with self.assertRaises(ValueError):
                orchestration.canonical(value)

    def test_registry_program_text_is_bounded_and_hashed(self):
        source = registry.program_source()
        self.assertEqual(source, (ROOT / "src/sherlock_registry.py").read_bytes())
        self.assertLess(len(source), 100_000)
        self.assertEqual(registry.program_sha256(), hashlib.sha256(source).hexdigest())
        argv = registry.program_argv("read", "/synthetic/registry", "--open")
        self.assertEqual((argv[:2], argv[3:]), (["python3", "-c"], ["read", "/synthetic/registry", "--open"]))
        self.assertLess(sum(len(part.encode()) for part in argv), 100_000)
        encoded = argv[2].split("'")[1]
        self.assertRegex(encoded, r"^[A-Za-z0-9+/=]+$", "the embedded program must be shell-neutral base64")
        self.assertEqual(zlib.decompress(base64.b64decode(encoded)), source, "the stub must carry exactly the module bytes it hashes")
        self.assertFalse(re.search(r"^\s*(from|import)\s+sherlock_", source.decode(), re.MULTILINE), "the remote program must not import a sibling module")


if __name__ == "__main__":
    unittest.main()
