"""`shk rules` and `shk install-skill`: packaged text verbatim, project copies, never HOME."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fakes import SRC

sys.path.insert(0, str(SRC))
import sherlock_kit  # noqa: E402
from sherlock_kit import cli, install  # noqa: E402

DATA = SRC / "sherlock_kit/data"
SKILL = DATA / "skills/sherlock-kit/SKILL.md"
COPIES = {"claude": ".claude/skills/sherlock-kit/SKILL.md", "codex": ".agents/skills/sherlock-kit/SKILL.md"}
RULES_COPY = ".claude/rules/sherlock-kit.md"
MARKER = (f"<!-- installed by sherlock-kit {sherlock_kit.__version__} via shk install-skill; "
          "edits are overwritten -->\n")


def inventory(root: Path) -> dict:
    return {str(p.relative_to(root)): (p.is_symlink(), p.read_bytes() if p.is_file() else None)
            for p in sorted(root.rglob("*"))}


class InstallCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.home, self.project = self.tmp / "home", self.tmp / "project"
        self.home.mkdir()
        self.project.mkdir()
        home = patch.dict(os.environ, {"HOME": str(self.home)})
        home.start()
        self.addCleanup(home.stop)

    def shk(self, *argv):
        out = io.StringIO()
        context = cli.Context(env={}, hostname="lab-ubuntu", cwd=self.tmp)
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(list(argv), modules=[install], context=context)
        return code, json.loads(out.getvalue())

    def skill(self, *extra):
        return self.shk("install-skill", "--project", str(self.project), *extra)


class RulesTests(InstallCase):
    def test_rules_print_the_packaged_files_byte_for_byte(self):
        env = {**os.environ, "PYTHONPATH": str(SRC), "HOME": str(self.home)}
        for kind in ("workstation", "cluster"):
            with self.subTest(kind=kind):
                done = subprocess.run([sys.executable, "-B", "-m", "sherlock_kit", "rules", kind], env=env,
                                      cwd=self.tmp, capture_output=True, check=True)
                self.assertEqual(done.stdout, (DATA / f"rules/{kind}.md").read_bytes())
                self.assertEqual(done.stderr, b"")
        self.assertEqual(inventory(self.home), {})

    def test_unknown_kind_is_a_usage_error(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main(["rules", "laptop"], modules=[install])
        self.assertEqual(caught.exception.code, 2)


class InstallSkillTests(InstallCase):
    def test_both_clients_get_a_marked_copy(self):
        code, report = self.skill()
        self.assertEqual((code, report["ok"]), (0, True))
        self.assertEqual(report["files"], {COPIES["claude"]: "written", COPIES["codex"]: "written"})
        source = SKILL.read_text(encoding="utf-8")
        for rel in COPIES.values():
            text = (self.project / rel).read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\nname: sherlock-kit\n"))
            front, body = source.split("\n---\n", 1)
            self.assertEqual(text, f"{front}\n---\n{MARKER}{body}")
            self.assertEqual(text.replace(MARKER, ""), source)
        self.assertEqual(inventory(self.home), {})

    def test_one_client(self):
        for client, rel in COPIES.items():
            with self.subTest(client=client):
                code, report = self.shk("install-skill", "--project", str(self.project), "--client", client)
                self.assertEqual((code, list(report["files"])), (0, [rel]))
        self.assertEqual(sorted(p.name for p in self.project.iterdir()), [".agents", ".claude"])

    def test_idempotent(self):
        self.skill("--rules", "workstation")
        before = {rel: os.stat(self.project / rel).st_mtime_ns for rel in (*COPIES.values(), RULES_COPY)}
        code, report = self.skill("--rules", "workstation")
        self.assertEqual(code, 0)
        self.assertEqual(set(report["files"].values()), {"current"})
        self.assertEqual({rel: os.stat(self.project / rel).st_mtime_ns for rel in before}, before)

    def test_check_reports_missing_and_stale_without_writing(self):
        code, report = self.skill("--check")
        self.assertEqual((code, report["ok"], set(report["files"].values())), (1, False, {"missing"}))
        self.assertEqual(list(self.project.iterdir()), [])
        self.skill()
        self.assertEqual(self.skill("--check")[0], 0)
        stale = self.project / COPIES["codex"]
        stale.write_text(stale.read_text(encoding="utf-8").replace(MARKER, MARKER.replace(
            sherlock_kit.__version__, "0.3.9")), encoding="utf-8")
        snapshot = inventory(self.project)
        code, report = self.skill("--check")
        self.assertEqual((code, report["files"][COPIES["codex"]], report["files"][COPIES["claude"]]),
                         (1, "stale", "current"))
        self.assertEqual(inventory(self.project), snapshot)
        self.assertEqual(self.skill()[1]["files"][COPIES["codex"]], "written")
        self.assertEqual(self.skill("--check")[0], 0)

    def test_rules_copy_and_global_link_skip(self):
        code, report = self.skill("--client", "claude", "--rules", "cluster")
        self.assertEqual((code, report["files"][RULES_COPY]), (0, "written"))
        self.assertEqual((self.project / RULES_COPY).read_bytes(), (DATA / "rules/cluster.md").read_bytes())
        (self.project / RULES_COPY).unlink()
        rules = self.home / ".claude/rules"
        rules.mkdir(parents=True)
        (rules / "sherlock-kit.md").symlink_to(DATA / "rules/workstation.md")
        before = inventory(self.home)
        code, report = self.skill("--client", "claude", "--rules", "cluster")
        self.assertEqual(code, 0)
        self.assertNotIn(RULES_COPY, report["files"])
        self.assertFalse((self.project / RULES_COPY).exists())
        self.assertTrue(any("~/.claude/rules/sherlock-kit.md" in n and "--force" in n for n in report["notices"]))
        code, report = self.skill("--client", "claude", "--rules", "cluster", "--force")
        self.assertEqual((code, report["files"][RULES_COPY]), (0, "written"))
        self.assertEqual(inventory(self.home), before)

    def test_notice_when_a_personal_skill_shadows_the_project_copy(self):
        self.assertEqual(self.skill()[1]["notices"], [])
        (self.home / ".claude/skills").mkdir(parents=True)
        (self.home / ".claude/skills/sherlock-kit").symlink_to(SKILL.parent, target_is_directory=True)
        before = inventory(self.home)
        code, report = self.skill()
        self.assertEqual(code, 0)
        self.assertTrue(any("~/.claude/skills/sherlock-kit" in n for n in report["notices"]))
        self.assertEqual(self.skill("--client", "codex")[1]["notices"], [])
        self.assertEqual(inventory(self.home), before)

    def test_codex_gets_a_hint_instead_of_a_claude_rules_file(self):
        code, report = self.skill("--client", "codex", "--rules", "cluster")
        self.assertEqual((code, list(report["files"])), (0, [COPIES["codex"]]))
        self.assertFalse((self.project / RULES_COPY).exists())
        hints = [n for n in report["notices"] if "AGENTS.md" in n]
        self.assertEqual(len(hints), 1)
        self.assertIn("shk rules cluster", hints[0])
        code, report = self.skill("--rules", "cluster")
        self.assertEqual((code, report["files"][RULES_COPY]), (0, "written"))
        self.assertEqual(len([n for n in report["notices"] if "AGENTS.md" in n]), 1)
        self.assertEqual(self.skill("--client", "claude", "--rules", "cluster")[1]["notices"], [])
        self.assertEqual(self.skill("--client", "codex")[1]["notices"], [])

    def test_links_inside_the_project_are_followed(self):
        (self.project / ".claude/skills").mkdir(parents=True)
        (self.project / ".agents").mkdir()
        (self.project / ".agents/skills").symlink_to("../.claude/skills", target_is_directory=True)
        code, report = self.skill()
        self.assertEqual((code, report["files"]), (0, {rel: "written" for rel in COPIES.values()}))
        self.assertTrue((self.project / ".agents/skills").is_symlink())
        self.assertIn(MARKER, (self.project / COPIES["claude"]).read_text(encoding="utf-8"))
        self.assertEqual(self.skill("--check")[0], 0)

    def test_refuses_home_missing_projects_and_targets_outside_the_project(self):
        for project in (self.home, self.tmp / "absent"):
            with self.subTest(project=project):
                code, report = self.shk("install-skill", "--project", str(project))
                self.assertEqual((code, report["error"]), (1, "project_invalid"))
        self.assertEqual(inventory(self.home), {})
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (self.project / ".claude").symlink_to(elsewhere, target_is_directory=True)
        for extra in ((), ("--check",)):
            with self.subTest(extra=extra):
                code, report = self.skill("--client", "claude", *extra)
                self.assertEqual((code, report["error"]), (1, "target_outside_project"))
                self.assertIn(str(elsewhere), report["message"])
        self.assertEqual(list(elsewhere.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
