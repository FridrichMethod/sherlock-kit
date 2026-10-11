"""CLI frame, version provenance, bin/shk entry point and the read-only doctor."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fakes import ROOT, SRC, install_fake_ssh

sys.path.insert(0, str(SRC))
import sherlock_kit  # noqa: E402
from sherlock_kit import cli  # noqa: E402
from sherlock_kit.errors import Outcome, ShkError  # noqa: E402

CHECK = ["-O", "check", "-oBatchMode=yes"]
VERSION = r"sherlock-kit 0\.4\.0 \((git [0-9a-f]+|editable .+|submodule .+|source .+|unknown)\)\n\Z"


def command(name, handler):
    def register(subparsers):
        subparsers.add_parser(name).set_defaults(handler=handler)
    return SimpleNamespace(register=register)


def run(argv, modules=(), context=None):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv, modules=list(modules), context=context)
    return code, out.getvalue(), err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()


class VersionTests(TempCase):
    def test_module_entry_point(self):
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        out = subprocess.run([sys.executable, "-B", "-m", "sherlock_kit", "--version"], env=env, cwd=self.tmp,
                             capture_output=True, text=True, check=True).stdout
        self.assertRegex(out, VERSION)
        self.assertEqual(sherlock_kit.__version__, "0.4.0")

    def test_provenance_forms(self):
        tree = self.tmp / "tree"
        tree.mkdir()
        p = sherlock_kit.provenance
        self.assertEqual(p({"url": "git+https://x", "vcs_info": {"vcs": "git", "commit_id": "ab12"}}, None), "git ab12")
        self.assertEqual(p({"url": tree.as_uri(), "dir_info": {"editable": True}}, tree), f"editable {tree}")
        self.assertEqual(p({"url": "file:///elsewhere", "dir_info": {"editable": True}}, tree), f"source {tree}")
        self.assertEqual(p({}, None), "unknown")
        (tree / ".git").write_text("gitdir: ../../.git/modules/common/sherlock-kit/sherlock-kit\n")
        self.assertEqual(p({}, tree), f"submodule {tree}")
        (tree / ".git").write_text("gitdir: /repo/.git/worktrees/sk-a\n")
        self.assertEqual(p({}, tree), f"source {tree}")

    def test_bin_shk_two_hop_symlink_writes_nothing(self):
        repo = self.tmp / "repo"
        shutil.copytree(SRC / "sherlock_kit", repo / "src/sherlock_kit",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy2(ROOT / "pyproject.toml", repo)
        (repo / "bin").mkdir()
        shutil.copy2(ROOT / "bin/shk", repo / "bin/shk")
        self.assertTrue(os.access(ROOT / "bin/shk", os.X_OK))
        (self.tmp / "hop1").symlink_to(repo / "bin/shk")
        (self.tmp / "hop2").symlink_to(self.tmp / "hop1")
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        out = subprocess.run([str(self.tmp / "hop2"), "--version"], env=env, cwd=self.tmp,
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out, f"sherlock-kit 0.4.0 (source {repo})\n")
        self.assertEqual(list(repo.rglob("__pycache__")), [])

    def test_bin_shk_refuses_old_python(self):
        code = ("import runpy, sys; sys.version_info = (3, 10, 4, 'final', 0); "
                "runpy.run_path(sys.argv[1], run_name='__main__')")
        result = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT / "bin/shk")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Python 3.11", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class FrameTests(TempCase):
    def context(self, **kw):
        return cli.Context(project_dir=self.tmp, env={}, hostname="lab-ubuntu", **kw)

    def test_outcomes_errors_and_text(self):
        modules = [command("good", lambda a, c: Outcome({"value": 1})),
                   command("anomaly", lambda a, c: Outcome({"value": 2}, 6)),
                   command("text", lambda a, c: Outcome.text("raw\n")),
                   command("refuse", lambda a, c: (_ for _ in ()).throw(ShkError(3, "live", "busy", "wait"))),
                   command("crash", lambda a, c: 1 / 0)]
        code, out, _ = run(["good"], modules, self.context())
        self.assertEqual((code, json.loads(out)), (0, {"ok": True, "value": 1}))
        self.assertEqual(json.loads(run(["anomaly"], modules, self.context())[1])["ok"], True)
        self.assertEqual(run(["text"], modules, self.context())[:2], (0, "raw\n"))
        code, out, _ = run(["refuse"], modules, self.context())
        expected = {"ok": False, "error": "live", "message": "busy", "hint": "wait"}
        self.assertEqual((code, json.loads(out)), (3, expected))
        code, out, err = run(["crash"], modules, self.context())
        self.assertEqual((code, json.loads(out)["error"]), (1, "internal_error"))
        self.assertIn("ZeroDivisionError", err)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            cli.main([], modules=[])
        self.assertEqual(caught.exception.code, 2)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.main(["--executor", "remote", "doctor"], modules=[])

    def test_loading_handler_and_rendering_failures_are_json(self):
        broken = SimpleNamespace(register=lambda subparsers: 1 / 0)
        modules = [command("none_text", lambda a, c: Outcome.text(None)),
                   command("bad_key", lambda a, c: Outcome({"rows": {(1, 2): 3}}))]
        for argv, mods in ((["none_text"], modules), (["bad_key"], modules), (["doctor"], [broken])):
            with self.subTest(argv=argv):
                code, out, err = run(argv, mods, self.context())
                self.assertEqual((code, json.loads(out)["error"]), (1, "internal_error"))
                self.assertIn("Traceback", err)
        for payload, code in (("text", 0), ({"text": 5}, 0), ({}, 7), ({}, True)):
            with self.subTest(payload=payload, code=code), self.assertRaises(TypeError):
                Outcome(payload, code)

    def test_context(self):
        now = datetime(2026, 10, 10, tzinfo=timezone.utc)
        ctx = self.context(clock=lambda: now)
        self.assertEqual(ctx.clock(), now)
        self.assertIsNotNone(cli.Context().clock().tzinfo)
        self.assertIsNone(ctx.config)
        with self.assertRaises(ShkError) as caught:
            ctx.require("project")
        self.assertEqual((caught.exception.code, caught.exception.token), (1, "config_required"))
        (self.tmp / "sherlock-kit.toml").write_text('project = "p"\n')
        ctx = self.context()
        self.assertEqual(ctx.require("project").project, "p")
        with self.assertRaises(ShkError) as caught:
            ctx.require("project", "remote_root")
        self.assertIn("remote_root", caught.exception.message)
        self.assertIs(ctx.executor(), ctx.executor())
        self.assertEqual((ctx.executor().host, ctx.data_executor().host), ("sherlock-plain", "sherlock-dtn"))
        self.assertEqual(cli.Context(executor="local", env={}, hostname="x").data_executor().kind, "local")

    def test_command_module_loading(self):
        self.assertEqual(cli.COMMAND_MODULES, ("slurm", "transfer", "install"))
        self.assertEqual(cli.load_modules(("not_written_yet",)), [])
        with patch.object(cli, "STRICT_COMMAND_IMPORTS", True), self.assertRaises(ModuleNotFoundError):
            cli.load_modules(("not_written_yet",))
        (self.tmp / "broken_dep.py").write_text("import shk_missing_dependency_xyz\n")
        with patch.object(sherlock_kit, "__path__", [*sherlock_kit.__path__, str(self.tmp)]):
            with self.assertRaises(ModuleNotFoundError):
                cli.load_modules(("broken_dep",))
            sys.modules.pop("sherlock_kit.broken_dep", None)


class DoctorTests(TempCase):
    def doctor(self, *args, **knobs):
        self.fake = install_fake_ssh(self.tmp)
        self.fake.record.unlink(missing_ok=True)
        for tool in ("sbatch", "squeue", "sacct"):
            (self.fake.bin / tool).write_text("#!/bin/sh\nexit 0\n")
            (self.fake.bin / tool).chmod(0o755)
        with patch.dict(os.environ, self.fake.env(**knobs)):
            code, out, _ = run(["--project-dir", str(self.tmp), *args])
        return code, json.loads(out)

    def test_local_doctor_only_queries_master_sockets(self):
        checks = [[*CHECK, "sherlock-plain"], [*CHECK, "sherlock-dtn"]]
        code, report = self.doctor("--executor", "ssh", "doctor", SHERLOCK_KIT_STATE_ROOT="/old")
        self.assertEqual((code, report["ok"], report["problems"]), (0, True, []))
        self.assertEqual(report["masters"], {"control": "running", "data": "running"})
        self.assertEqual(self.fake.calls(), checks)
        self.assertTrue(any("SHERLOCK_KIT_STATE_ROOT" in w for w in report["warnings"]))
        self.assertRegex(report["version"] + "\n", VERSION)
        code, report = self.doctor("--executor", "ssh", "doctor", FAKE_SSH_MASTER="none")
        self.assertEqual((code, report["ok"]), (4, False))
        self.assertEqual(report["masters"], {"control": "none", "data": "none"})
        self.assertTrue(any("ssh sherlock-plain" in p for p in report["problems"]))
        self.assertTrue(any("ssh -fN sherlock-dtn" in w for w in report["warnings"]))
        self.assertEqual(self.fake.calls(), checks)

    def test_doctor_master_states(self):
        code, report = self.doctor("--executor", "ssh", "doctor", FAKE_SSH_MASTER="none",
                                   FAKE_SSH_MASTER_HOSTS="sherlock-dtn")
        self.assertEqual((code, report["masters"]), (0, {"control": "running", "data": "none"}))
        self.assertTrue(any("ssh -fN sherlock-dtn" in w for w in report["warnings"]))
        code, report = self.doctor("--executor", "ssh", "doctor", FAKE_SSH_MASTER="unconfigured")
        self.assertEqual((code, report["masters"]["control"]), (4, "not_configured"))
        self.assertTrue(any("No ControlPath specified" in p and "ControlPath ~/.ssh/%C for sherlock-plain in "
                            "~/.ssh/config" in p for p in report["problems"]))

    def test_remote_doctor(self):
        code, report = self.doctor("--executor", "ssh", "doctor", "--remote")
        self.assertEqual((code, report["remote"]["status"]), (0, "complete"))
        self.assertEqual(set(report["remote"]["tools"]), {"sbatch", "squeue", "sacct", "sha256sum"})
        self.assertEqual(len([c for c in self.fake.calls() if "-O" not in c]), 1)
        code, report = self.doctor("--executor", "ssh", "doctor", "--remote", FAKE_SSH_MASTER="none")
        self.assertEqual((code, report["remote"]["status"]), (4, "auth_required"))
        self.assertTrue(all("-O" in c for c in self.fake.calls()))

    def test_local_executor_doctor_never_uses_ssh(self):
        code, report = self.doctor("--executor", "local", "doctor", "--remote")
        self.assertEqual((code, report["executor"], report["masters"]), (0, "local", {}))
        self.assertEqual(report["remote"]["user"], pwd.getpwuid(os.getuid()).pw_name)
        self.assertEqual(self.fake.calls(), [])


if __name__ == "__main__":
    unittest.main()
