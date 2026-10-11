"""Release metadata: contract paths C1-C4, one version, rules format, mentioned CLI, removed terms, HOME."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

from fakes import ROOT, SRC, install_fake_ssh

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, partitions  # noqa: E402

CONTRACT = {"C1": "src/sherlock_kit/data/rules/workstation.md", "C2": "src/sherlock_kit/data/rules/cluster.md",
            "C3": "src/sherlock_kit/data/skills/sherlock-kit/SKILL.md", "C4": "bin/shk"}
RULES = (ROOT / CONTRACT["C1"], ROOT / CONTRACT["C2"])
MENTIONING = (*RULES, ROOT / CONTRACT["C3"], ROOT / "README.md")
REMOVED = ("policy --identity", "reconcile", "--attempt", "SHERLOCK_KIT_STATE_ROOT", ".local/share/sherlock-kit",
           "registry_root", "SHERLOCK.md")
# Commands registered by modules another package writes; until the lead merges them their
# checks skip as integration-only, and run_suite fails those skips under SHK_REQUIRE_ALL.
OWNER = {"submit": "slurm", "status": "slurm", "cancel": "slurm", "occupancy": "slurm",
         "push": "transfer", "pull": "transfer", "manifest": "transfer"}
GLOBAL_VALUE = ("--project-dir", "--executor")
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
SPAN = re.compile(r"`([^`\n]+)`")


def git(*args) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout


def in_git() -> bool:
    return shutil.which("git") is not None and subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"], capture_output=True).returncode == 0


def absent(module: str) -> bool:
    return importlib.util.find_spec(f"sherlock_kit.{module}") is None


def snippets(text: str) -> list[str]:
    """Shell-like snippets: fenced lines (indented lines continue the previous one) and inline code spans."""
    found = []
    for block in FENCE.findall(text):
        for line in block.splitlines():
            line = re.sub(r"\s#\s.*$", "", line).rstrip().removesuffix("\\")
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
                found.extend(invocation(tokens[start + 1:]))
    return found


class ContractTests(unittest.TestCase):
    def test_contract_paths_exist(self):
        for name, rel in CONTRACT.items():
            self.assertTrue((ROOT / rel).is_file(), f"{name} {rel} is missing")
        self.assertTrue(os.access(ROOT / CONTRACT["C4"], os.X_OK))

    @unittest.skipUnless(in_git(), "needs a git checkout")
    def test_contract_paths_are_tracked_and_bin_shk_is_executable(self):
        entries = {line.split("\t")[1]: line.split()[0] for line in git("ls-files", "-s", *CONTRACT.values()).splitlines()}
        self.assertEqual(set(entries), set(CONTRACT.values()))
        self.assertEqual(entries[CONTRACT["C4"]], "100755")

    @unittest.skipUnless(in_git(), "needs a git checkout")
    def test_no_absolute_symlinks(self):
        links = [line.split() for line in git("ls-files", "-s").splitlines() if line.startswith("120000 ")]
        for _, blob, _, path in links:
            self.assertFalse(git("cat-file", "-p", blob).startswith("/"), f"{path} is an absolute symlink")

    def test_one_version(self):
        version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        heading = re.search(r"^## (\S+)", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(heading, "CHANGELOG.md needs a '## VERSION' heading")
        self.assertEqual(heading.group(1), version)


class RulesFormatTests(unittest.TestCase):
    def test_partition_block_is_generated_and_identical(self):
        block = partitions.markdown()
        for path in RULES:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.count(block), 1, "paste the output of partitions.markdown() unchanged")
                self.assertTrue(text.endswith(block), "the partition block closes the file")

    def test_rules_format(self):
        for path in RULES:
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                text = raw.decode("ascii")
                self.assertLessEqual(len(raw), 7 * 1024)
                self.assertTrue(text.startswith("## Sherlock"), "starts with an H2, no front matter")
                self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))
                self.assertNotIn("<!--", text)
                self.assertNotIn("@", text)
                self.assertNotRegex(text, r"\]\((?!https://)", "no relative links")
                self.assertIn("Reviewed 2026-10-10", text)

    def test_skill_front_matter(self):
        text = (ROOT / CONTRACT["C3"]).read_text(encoding="ascii")
        front = re.match(r"---\n(.*?)\n---\n", text, re.S)
        self.assertIsNotNone(front)
        keys = [line.split(":", 1)[0] for line in front.group(1).splitlines()]
        self.assertEqual(keys, ["name", "description"])
        self.assertIn("name: sherlock-kit\n", front.group(0))
        self.assertNotIn(": ", front.group(1).split("description:", 1)[1].strip())


class MentionedCliTests(unittest.TestCase):
    def check(self, owned_by):
        parser = cli.build_parser(cli.load_modules())
        commands = next(a for a in parser._actions if a.dest == "command").choices
        top = {s for a in parser._actions for s in a.option_strings}
        checked = 0
        for path in MENTIONING:
            for command, options in mentions(path.read_text(encoding="utf-8")):
                if OWNER.get(command) != owned_by:
                    continue
                checked += 1
                self.assertTrue(command in commands, f"{path.name}: shk {command} does not exist")
                known = top | {s for a in commands[command]._actions for s in a.option_strings}
                for option in options:
                    self.assertTrue(option in known, f"{path.name}: shk {command} has no {option}")
        self.assertGreater(checked, 0)

    def test_core_and_install_commands_exist(self):
        self.assertEqual(mentions("`shk --executor local status --all ...` and `shk rules <kind>`\n```\n"
                                  "shk cancel (--task T | --name N)  # --x\n  [--a|--b=1] && shk doctor\n```\n"),
                         [("cancel", ["--task", "--name", "--a", "--b"]), ("doctor", []), ("status", ["--all"]),
                          ("rules", [])])
        self.check(None)

    @unittest.skipIf(absent("slurm"), "integration-only: slurm absent")
    def test_slurm_commands_exist(self):
        self.check("slurm")

    @unittest.skipIf(absent("transfer"), "integration-only: transfer absent")
    def test_transfer_commands_exist(self):
        self.check("transfer")


class RemovedTermTests(unittest.TestCase):
    def test_removed_terms_are_gone(self):
        paths = [p for top in ("src", "docs", "bin") for p in (ROOT / top).rglob("*") if p.is_file()
                 and not any(part == "__pycache__" or part.endswith(".egg-info") for part in p.parts)]
        paths += [ROOT / name for name in ("README.md", "CONTRIBUTING.md", "AGENTS.md", "pyproject.toml")]
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
        (self.project / "sherlock-kit.toml").write_text('project = "g9"\nremote_root = "/scratch/users/x/g9"\n')
        (self.project / "job.sh").write_text("#!/bin/bash -l\necho ok\n")
        fake = install_fake_ssh(self.tmp / "tools")
        keep = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "SHK_", "SHERLOCK_KIT_"))}
        self.env = {**keep, **fake.env(), "HOME": str(self.home), "PYTHONPATH": str(SRC)}

    def shk(self, *argv) -> subprocess.CompletedProcess:
        command = [sys.executable, "-B", "-m", "sherlock_kit", "--project-dir", str(self.project), *argv]
        done = subprocess.run(command, env=self.env, cwd=self.project, capture_output=True, text=True, timeout=60)
        self.assertEqual(list(self.home.rglob("*")), [], f"shk {' '.join(argv)} wrote under HOME")
        return done

    def test_read_only_commands(self):
        self.assertEqual(self.shk("--version").returncode, 0)
        for kind in ("workstation", "cluster"):
            self.assertEqual(self.shk("rules", kind).returncode, 0)
        done = self.shk("--executor", "ssh", "doctor")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertTrue(json.loads(done.stdout)["ok"])

    @unittest.skipIf(absent("slurm"), "integration-only: slurm absent")
    def test_submit_dry_run(self):
        done = self.shk("submit", "--task", "g9", "--script", "job.sh", "--partition", "normal", "--cpus", "1",
                        "--mem", "2G", "--time", "20m", "--dry-run")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
