"""Release metadata: contract paths C1-C4, one version, rules format, mentioned CLI, removed terms, HOME.

The rules, skill and mention checks cover both sites' content; tracked files name a Marlowe project
only by the synthetic id m000000.
"""

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import ClassVar

from fakes import ROOT, SRC, in_git, install_fake_ssh

sys.path.insert(0, str(SRC))
import sherlock_kit
from sherlock_kit import cli, partitions

CONTRACT = {
    "C1": "src/sherlock_kit/data/rules/workstation.md",
    "C2": "src/sherlock_kit/data/rules/cluster.md",
    "C3": "src/sherlock_kit/data/skills/sherlock-kit/SKILL.md",
    "C4": "bin/shk",
}
RULES = (ROOT / CONTRACT["C1"], ROOT / CONTRACT["C2"])
MARLOWE_RULES = (
    ROOT / "src/sherlock_kit/data/rules/marlowe-workstation.md",
    ROOT / "src/sherlock_kit/data/rules/marlowe-cluster.md",
)
MARLOWE_SKILL = ROOT / "src/sherlock_kit/data/skills/sherlock-kit-marlowe/SKILL.md"
SITE_RULES = {"Sherlock": RULES, "Marlowe": MARLOWE_RULES}
SKILLS = {"sherlock-kit": ROOT / CONTRACT["C3"], "sherlock-kit-marlowe": MARLOWE_SKILL}
MENTIONING = (
    *RULES,
    ROOT / CONTRACT["C3"],
    ROOT / "README.md",
    ROOT / "docs/cli.md",
    ROOT / "docs/install.md",
    *MARLOWE_RULES,
    MARLOWE_SKILL,
    ROOT / "docs/marlowe-policy.md",
    ROOT / "docs/validation/observed-marlowe.md",
)
REMOVED = (
    "policy --identity",
    "reconcile",
    "--attempt",
    "SHERLOCK_KIT_STATE_ROOT",
    ".local/share/sherlock-kit",
    "registry_root",
    "SHERLOCK.md",
)
GLOBAL_VALUE = ("--project-dir", "--executor", "--site")
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
SPAN = re.compile(r"`([^`\n]+)`")
# A Marlowe project id is m and six digits (alone, in marlowe-m..., /scratch/m..., /projects/m...).
# The pattern holds no id and SYNTHETIC_ID is the only one allowed; a finding names lines, not ids.
PROJECT_ID = re.compile(rb"m[0-9]{6,}")
SYNTHETIC_ID = b"m000000"


def git(*args) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout


def snippets(text: str) -> list[str]:
    """Shell-like snippets: fenced lines (indented lines continue the previous one) and inline code spans."""
    found = []
    for block in FENCE.findall(text):
        for raw in block.splitlines():
            line = re.sub(r"\s#\s.*$", "", raw).rstrip().removesuffix("\\")
            if found and line[:1].isspace():
                found[-1] += " " + line.strip()
            elif line:
                found.append(line)
    return found + SPAN.findall(FENCE.sub("", text))


def invocation(tokens: list[str]) -> list[tuple[str, list[str]]]:
    command, options, skip = None, [], False
    for token in tokens:
        if skip:
            skip = False
        elif token in ("&&", "||", ";", "shk"):
            break
        elif command is None and token.split("=")[0] in GLOBAL_VALUE:
            skip = "=" not in token
        elif command is None and not token.startswith("-"):
            command = token
        elif command is not None:
            options += [o.split("=")[0] for o in token.split("|") if o.startswith("--")]
    return [(command, options)] if command and command[0].isalpha() else []


def mentions(text: str) -> list[tuple[str, list[str]]]:
    """(command, --options) for every shk invocation in the text's code."""
    found = []
    for snippet in snippets(text):
        tokens = [t for t in (t.strip("[](),;.") for t in snippet.split()) if t]
        for start, token in enumerate(tokens):
            if token == "shk" or token.endswith("/shk"):
                found.extend(invocation(tokens[start + 1 :]))
    return found


def foreign_id_lines(data: bytes) -> list[int]:
    """The 1-based lines of data that hold a Marlowe project id other than SYNTHETIC_ID."""
    return sorted({
        data.count(b"\n", 0, found.start()) + 1
        for found in PROJECT_ID.finditer(data)
        if found.group() != SYNTHETIC_ID
    })


class ContractTests(unittest.TestCase):
    def test_contract_paths_exist(self):
        for name, rel in CONTRACT.items():
            self.assertTrue((ROOT / rel).is_file(), f"{name} {rel} is missing")
        self.assertTrue(os.access(ROOT / CONTRACT["C4"], os.X_OK))

    @unittest.skipUnless(in_git(), "needs a git checkout")
    def test_contract_paths_are_tracked_and_bin_shk_is_executable(self):
        listed = git("ls-files", "-s", *CONTRACT.values()).splitlines()
        entries = {line.split("\t")[1]: line.split()[0] for line in listed}
        self.assertEqual(set(entries), set(CONTRACT.values()))
        self.assertEqual(entries[CONTRACT["C4"]], "100755")

    @unittest.skipUnless(in_git(), "needs a git checkout")
    def test_no_absolute_symlinks(self):
        links = [
            line.split()
            for line in git("ls-files", "-s").splitlines()
            if line.startswith("120000 ")
        ]
        for _, blob, _, path in links:
            self.assertFalse(
                git("cat-file", "-p", blob).startswith("/"), f"{path} is an absolute symlink"
            )

    @unittest.skipUnless(in_git(), "needs a git checkout")
    def test_tracked_files_name_only_the_synthetic_marlowe_project(self):
        for rel in filter(None, git("ls-files", "-z").split("\0")):
            for where, data in (("path", rel.encode()), ("lines", (ROOT / rel).read_bytes())):
                lines = foreign_id_lines(data)
                self.assertTrue(
                    not lines,  # never print the id itself
                    f"{rel}: {where} {lines} name a Marlowe project id other than m000000",
                )

    def test_foreign_project_ids_are_found_in_every_form(self):
        digits = "".join(map(str, range(1, 7)))  # built at run time: this file holds no such id
        for text, lines in (
            ("m000000 marlowe-m000000 /scratch/m000000/USER /projects/m000000\n", []),
            (f"m{digits}", [1]),
            (f"a\naccount = marlowe-m{digits}\n", [2]),
            (f"/scratch/m{digits}/USER\n/projects/m{digits}\n", [1, 2]),
            (f"x_m{digits} m000000{digits}\n", [1]),
        ):
            with self.subTest(text=text):
                self.assertEqual(foreign_id_lines(text.encode()), lines)

    def test_one_version(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["project"]["dynamic"], ["version"])
        self.assertEqual(
            project["tool"]["setuptools"]["dynamic"]["version"],
            {"attr": "sherlock_kit.__version__"},
        )
        self.assert_latest_release((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))

    def assert_latest_release(self, changelog: str):
        headings = re.findall(r"^##(?:[ \t]+(.*))?$", changelog, re.M)
        if headings[:1] == ["Unreleased"]:
            headings = headings[1:]
        self.assertTrue(headings, "CHANGELOG.md needs a '## VERSION' heading")
        self.assertEqual(headings[0], sherlock_kit.__version__)

    def test_latest_release_allows_one_optional_leading_unreleased(self):
        for prefix in ("", "## Unreleased\n\n- Pending change.\n\n"):
            with self.subTest(prefix=prefix):
                self.assert_latest_release(
                    f"# Changelog\n\n{prefix}## {sherlock_kit.__version__}\n\n## 0.1.0\n"
                )

    def test_latest_release_rejects_missing_mismatched_or_malformed_headings(self):
        for changelog in (
            "# Changelog\n",
            "## Unreleased\n",
            f"## Unreleased\n\n## Unreleased\n\n## {sherlock_kit.__version__}\n",
            "## 999.0.0\n",
            f"## 999.0.0\n\n## {sherlock_kit.__version__}\n",
            "## Unreleased\n\n## 999.0.0\n",
            "## VERSION\n",
            "##\n",
            f"## {sherlock_kit.__version__} extra\n",
            f"## Unreleased extra\n\n## {sherlock_kit.__version__}\n",
        ):
            with self.subTest(changelog=changelog), self.assertRaises(AssertionError):
                self.assert_latest_release(changelog)


class RulesFormatTests(unittest.TestCase):
    def test_partition_block_is_generated_and_identical(self):
        block = partitions.markdown()
        for path in RULES:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(
                    text.count(block), 1, "paste the output of partitions.markdown() unchanged"
                )
                self.assertTrue(text.endswith(block), "the partition block closes the file")

    def test_rules_format(self):
        for display, paths in SITE_RULES.items():
            for path in paths:
                with self.subTest(path=path.name):
                    raw = path.read_bytes()
                    text = raw.decode("ascii")
                    self.assertLessEqual(len(raw), 7 * 1024)
                    self.assertTrue(
                        text.startswith(f"## {display}"), "starts with an H2, no front matter"
                    )
                    self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
                    self.assertNotIn("<!--", text)
                    self.assertNotIn("@", text)
                    self.assertNotRegex(text, r"\]\((?!https://)", "no relative links")
                    reviewed = {"Sherlock": "2026-11-22", "Marlowe": "2026-11-21"}[display]
                    self.assertIn(f"Reviewed {reviewed}", text)

    def test_rules_headings(self):
        expected = {
            "workstation.md": [
                "Connection",
                "Jobs",
                "Data",
                "GPU scheduling defaults",
                "Partition profiles",
            ],
            "cluster.md": [
                "Where you are",
                "Filesystems",
                "Login nodes and software",
                "Slurm",
                "GPU scheduling defaults",
                "Partition profiles",
            ],
            "marlowe-workstation.md": [
                "Site rules",
                "Connection",
                "Jobs",
                "Cost and waiting",
                "Preemption",
                "Data",
                "Partition profiles",
            ],
            "marlowe-cluster.md": [
                "Site rules",
                "Where you are",
                "Login nodes and software",
                "Jobs",
                "Cost and waiting",
                "Preemption",
                "Data",
                "Partition profiles",
            ],
        }
        for path in (*RULES, *MARLOWE_RULES):
            with self.subTest(path=path.name):
                headings = re.findall(r"^### (.+)$", path.read_text(encoding="utf-8"), re.M)
                self.assertEqual(headings, expected[path.name])

    def test_agent_ssh_commands_cannot_log_in(self):
        """An ssh line that runs a remote command must ride the master only: never a fresh login."""
        checked = 0
        for path in (*RULES, *MARLOWE_RULES, *SKILLS.values()):
            for snippet in snippets(path.read_text(encoding="utf-8")):
                tokens = snippet.split()
                hosts = [
                    i for i, token in enumerate(tokens) if token.startswith(("sherlock", "marlowe"))
                ]
                if tokens[:1] != ["ssh"] or not hosts or hosts[0] == len(tokens) - 1:
                    continue
                checked += 1
                with self.subTest(path=path.name, snippet=snippet):
                    self.assertIn("-oBatchMode=yes", tokens[: hosts[0]])
                    self.assertIn("-oProxyCommand=false", tokens[: hosts[0]])
        self.assertGreater(checked, 0)

    def test_skill_front_matter(self):
        for name, path in SKILLS.items():
            with self.subTest(skill=name):
                text = path.read_text(encoding="ascii")
                front = re.match(r"---\n(.*?)\n---\n", text, re.S)
                self.assertIsNotNone(front)
                keys = [line.split(":", 1)[0] for line in front.group(1).splitlines()]
                self.assertEqual(keys, ["name", "description"])
                self.assertIn(f"name: {name}\n", front.group(0))
                self.assertNotIn(": ", front.group(1).split("description:", 1)[1].strip())


class MentionedCliTests(unittest.TestCase):
    def test_mentions_are_parsed(self):
        self.assertEqual(
            mentions(
                "`shk --executor local status --all ...` and `shk rules <kind>`\n```\n"
                "shk cancel (--task T | --name N)  # --x\n  [--a|--b=1] && shk doctor\n```\n"
            ),
            [
                ("cancel", ["--task", "--name", "--a", "--b"]),
                ("doctor", []),
                ("status", ["--all"]),
                ("rules", []),
            ],
        )

    def test_site_is_a_global_option_with_a_value(self):
        self.assertEqual(
            mentions(
                "`shk --site marlowe submit --task t` and "
                "`shk --site=marlowe --project-dir marlowe status --all`"
            ),
            [("submit", ["--task"]), ("status", ["--all"])],
        )

    def test_every_mentioned_command_and_option_exists(self):
        parser = cli.build_parser()
        commands = next(a for a in parser._actions if a.dest == "command").choices
        top = {s for a in parser._actions for s in a.option_strings}
        mentioned = set()
        for path in MENTIONING:
            for command, options in mentions(path.read_text(encoding="utf-8")):
                mentioned.add(command)
                self.assertTrue(command in commands, f"{path.name}: shk {command} does not exist")
                known = top | {s for a in commands[command]._actions for s in a.option_strings}
                for option in options:
                    self.assertTrue(option in known, f"{path.name}: shk {command} has no {option}")
        self.assertEqual(mentioned, set(commands))


class ManifestOneLinerTests(unittest.TestCase):
    """Each skill's sha256sum fallback must write what `shk pull` accepts, like `shk manifest`."""

    FILES: ClassVar[dict[str, bytes]] = {
        "a.txt": b"a\n",
        "-b": b"b\n",
        "-c": b"c\n",
        "sub/d.bin": b"\0d",
        "slurm-1.out": b"log\n",
    }

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.out = Path(temporary.name).resolve() / "out"
        for rel, data in self.FILES.items():
            (self.out / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.out / rel).write_bytes(data)
        (self.out / ".shk").mkdir()
        (self.out / ".shk/shk.p.t.0123456789.1.sh").write_text("#!/bin/sh\n")
        (self.out / "sub/.shk-stage").write_text("x\n")
        (self.out / "sub/SHA256SUMS").write_text("nested\n")
        (self.out / "SHA256SUMS").write_text("old\n")

    def one_liner(self, skill: Path) -> str:
        text = skill.read_text(encoding="utf-8")
        found = [span for span in SPAN.findall(text) if "sha256sum" in span and "find" in span]
        self.assertEqual(len(found), 1, "one sha256sum one-liner in the skill")
        return found[0]

    def written(self, skill: Path) -> str:
        command = self.one_liner(skill).replace("DIR", shlex.quote(str(self.out)))
        subprocess.run(["sh", "-c", command], cwd=self.out.parent, check=True, timeout=60)
        return (self.out / "SHA256SUMS").read_text(encoding="utf-8")

    def expected(self) -> dict:
        return {rel: hashlib.sha256(data).hexdigest() for rel, data in self.FILES.items()}

    def test_one_liner_lists_every_file_except_shk_entries(self):
        for name, skill in SKILLS.items():
            with self.subTest(skill=name):
                lines = self.written(skill).splitlines()
                self.assertEqual(
                    sorted(lines),
                    sorted(f"{digest}  {rel}" for rel, digest in self.expected().items()),
                )

    def test_shk_pull_accepts_the_one_liner(self):
        from sherlock_kit import transfer

        program = SRC / "sherlock_kit/data/remote/manifest.sh"
        for name, skill in SKILLS.items():
            with self.subTest(skill=name):
                self.assertEqual(transfer.parse_manifest(self.written(skill)), self.expected())
                reply = subprocess.run(
                    ["sh", str(program), str(self.out)],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=60,
                ).stdout
                self.assertIn(f"SHK_COUNT {len(self.FILES)}\n", reply)


class RemovedTermTests(unittest.TestCase):
    def test_removed_terms_are_gone(self):
        paths = [
            p
            for top in ("src", "docs", "bin")
            for p in (ROOT / top).rglob("*")
            if p.is_file()
            and not any(part == "__pycache__" or part.endswith(".egg-info") for part in p.parts)
        ]
        paths += [
            ROOT / name for name in ("README.md", "CONTRIBUTING.md", "AGENTS.md", "pyproject.toml")
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for term in REMOVED:
                self.assertFalse(term in text, f"{path.relative_to(ROOT)} still mentions {term}")
        self.assertFalse((ROOT / "SHERLOCK.md").exists())


class NoHomeWriteTests(unittest.TestCase):
    """G9: read-only commands leave an empty HOME empty (no state root, no caches)."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.home, self.project = self.tmp / "home", self.tmp / "project"
        self.home.mkdir()
        self.project.mkdir()
        (self.project / "sherlock-kit.toml").write_text(
            'project = "g9"\nremote_root = "/scratch/users/x/g9"\n'
        )
        (self.project / "job.sh").write_text("#!/bin/bash -l\necho ok\n")
        fake = install_fake_ssh(self.tmp / "tools")
        keep = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("PYTHON", "SHK_", "SHERLOCK_KIT_"))
        }
        self.env = {**keep, **fake.env(), "HOME": str(self.home), "PYTHONPATH": str(SRC)}

    def shk(self, *argv) -> subprocess.CompletedProcess:
        command = [
            sys.executable,
            "-B",
            "-m",
            "sherlock_kit",
            "--project-dir",
            str(self.project),
            *argv,
        ]
        done = subprocess.run(
            command,
            env=self.env,
            cwd=self.project,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(list(self.home.rglob("*")), [], f"shk {' '.join(argv)} wrote under HOME")
        return done

    def test_read_only_commands(self):
        self.assertEqual(self.shk("--version").returncode, 0)
        for kind in ("workstation", "cluster"):
            self.assertEqual(self.shk("rules", kind).returncode, 0)
        done = self.shk("--executor", "ssh", "doctor")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertTrue(json.loads(done.stdout)["ok"])

    def test_submit_dry_run(self):
        done = self.shk(
            "submit",
            "--task",
            "g9",
            "--script",
            "job.sh",
            "--partition",
            "normal",
            "--cpus",
            "1",
            "--mem",
            "2G",
            "--time",
            "20m",
            "--dry-run",
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
