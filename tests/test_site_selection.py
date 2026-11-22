"""Selecting the site: --site, the configuration's site, site_mismatch, doctor and transfers."""

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fakes import SRC, install_fake_ssh, marlowe_config, marlowe_site

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, executor, sites, transfer
from sherlock_kit.errors import ShkError

CHECK = ["-O", "check", "-oBatchMode=yes"]
# Every way a command reaches the site of its Context.
USES = (
    lambda ctx: ctx.site,
    lambda ctx: ctx.settings,
    lambda ctx: ctx.executor(),
    lambda ctx: ctx.data_executor(),
)
SHERLOCK_TABLE = (
    '[tool.sherlock-kit]\nproject = "demo"\nremote_root = "/scratch/users/u/demo"\n'
    'control_host = "sherlock-plain"\ndata_host = "sherlock-dtn"\n'
)


class SiteCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.empty = self.tmp / "empty"
        self.empty.mkdir()
        self.fake = install_fake_ssh(self.tmp)
        self.enterContext(marlowe_site(on_cluster=False))

    def knobs(self, **values):
        self.enterContext(patch.dict(os.environ, self.fake.env(**values)))

    def context(self, project_dir=None, site=None, executor="auto", cwd=None):
        return cli.Context(
            project_dir=project_dir,
            executor=executor,
            site=site,
            env={},
            hostname="lab-ubuntu",
            cwd=cwd,
        )

    def shk(self, *argv, modules=cli.MODULES, **where):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(list(argv), modules=modules, context=self.context(**where))
        return code, json.loads(out.getvalue())

    def hosts(self):
        """Every host the fake ssh saw: the last word of a check, the host word of a dial."""
        seen = []
        for call in self.fake.calls():
            if "-O" in call:
                seen.append(call[-1])
            else:
                seen.append(next(word for word in call if not word.startswith("-")))
        return seen


class ResolutionTests(SiteCase):
    def test_help_lists_the_site_option(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("--site {sherlock,marlowe}", out.getvalue())

    def test_flag_then_configuration_then_sherlock(self):
        self.assertEqual(self.context(self.empty).site.name, "sherlock")
        self.assertEqual(self.context(self.empty, "marlowe").site.name, "marlowe")
        self.assertEqual(self.context(self.empty, "sherlock").site.name, "sherlock")
        marlowe = self.tmp / "marlowe"
        marlowe_config(marlowe)
        self.assertEqual(self.context(marlowe).site.name, "marlowe")
        self.assertEqual(self.context(marlowe, "marlowe").site.name, "marlowe")
        (self.empty / "sherlock-kit.toml").write_text('project = "demo"\n')
        self.assertEqual(self.context(self.empty, "sherlock").site.name, "sherlock")
        self.assertEqual(self.context(self.empty).settings.control_host, "sherlock-plain")

    def test_a_flag_naming_another_site_than_the_configuration_is_refused(self):
        marlowe = self.tmp / "marlowe"
        marlowe_config(marlowe)
        (self.empty / "sherlock-kit.toml").write_text('project = "demo"\n')  # no site: Sherlock
        for directory, flag, configured in (
            (self.empty, "marlowe", "sherlock"),
            (marlowe, "sherlock", "marlowe"),
        ):
            ctx = self.context(directory, flag)
            for use in USES:
                with self.subTest(flag=flag), self.assertRaises(ShkError) as caught:
                    use(ctx)
                error = caught.exception
                path = directory / "sherlock-kit.toml"
                self.assertEqual((error.code, error.token), (1, "site_mismatch"))
                self.assertEqual(
                    error.message,
                    f"--site {flag} does not match {path}, a configuration of site {configured}",
                )
                self.assertIn(str(path), error.hint)
                self.assertIn("--project-dir", error.hint)
                self.assertIn(f'site = "{flag}"', error.hint)
            self.assertEqual(ctx.flag_site.name, flag)  # rules and install-skill read no config
        self.assertEqual(self.fake.calls(), [])

    def test_the_flag_site_never_reads_a_configuration(self):
        (self.empty / "sherlock-kit.toml").write_text("unknown = 1\n")
        for flag, name in ((None, "sherlock"), ("sherlock", "sherlock"), ("marlowe", "marlowe")):
            ctx = self.context(self.empty, flag)
            self.assertEqual(ctx.flag_site.name, name)
            with self.assertRaises(ShkError) as caught:
                _ = ctx.site
            self.assertEqual(caught.exception.token, "config_invalid")

    def test_mismatch_stops_a_command_before_any_remote_call(self):
        (self.empty / "sherlock-kit.toml").write_text('project = "demo"\n')
        self.knobs()
        for argv in (("status", "--task", "t"), ("cancel", "--task", "t"), ("status", "--all")):
            with self.subTest(argv=argv):
                code, reply = self.shk(*argv, project_dir=self.empty, site="marlowe")
                self.assertEqual((code, reply["error"]), (1, "site_mismatch"), reply)
        code, reply = self.shk(
            "push", "--anywhere", str(self.empty), "/x", project_dir=self.empty, site="marlowe"
        )
        self.assertEqual((code, reply["error"]), (1, "site_mismatch"), reply)
        self.assertEqual(self.fake.calls(), [])


class TwoClusterRepositoryTests(SiteCase):
    """A Sherlock table at the root and a Marlowe configuration in marlowe/sherlock-kit.toml."""

    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        (self.repo / "pyproject.toml").write_text(SHERLOCK_TABLE)
        marlowe_config(self.repo / "marlowe", remote_root='"/scratch/m000000/USER/demo"')
        self.knobs()

    def run_shk(self, *argv, cwd):
        """The real command line: --site reaches the Context through argv."""
        done = subprocess.run(
            [sys.executable, "-B", "-m", "sherlock_kit", *argv],
            env={**os.environ, "PYTHONPATH": str(SRC)},
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return done.returncode, json.loads(done.stdout)

    def test_from_the_root_site_marlowe_is_a_mismatch(self):
        code, report = self.run_shk(
            "--site", "marlowe", "--executor", "ssh", "doctor", cwd=self.repo
        )
        self.assertEqual(
            (code, report["ok"], report["executor"], report["masters"]), (1, False, None, {})
        )
        self.assertEqual(report["site"], "marlowe")
        self.assertEqual(len(report["problems"]), 1)
        self.assertIn(
            f"{self.repo / 'pyproject.toml'}, a configuration of site sherlock",
            report["problems"][0],
        )
        code, reply = self.run_shk(
            "--site", "marlowe", "--executor", "ssh", "status", "--task", "t", cwd=self.repo
        )
        self.assertEqual((code, reply["error"]), (1, "site_mismatch"))
        self.assertIn("--project-dir", reply["hint"])
        self.assertEqual(self.fake.calls(), [])

    def test_with_the_marlowe_directory_it_is_marlowe(self):
        for argv, cwd in (
            (("--project-dir", "marlowe", "--site", "marlowe"), self.repo),
            (("--site", "marlowe"), self.repo / "marlowe"),
            ((), self.repo / "marlowe"),
        ):
            with self.subTest(argv=argv, cwd=cwd):
                before = len(self.fake.calls())
                code, report = self.run_shk(*argv, "--executor", "ssh", "doctor", cwd=cwd)
                self.assertEqual((code, report["site"], report["problems"]), (0, "marlowe", []))
                self.assertEqual(self.fake.calls()[before:], [[*CHECK, "marlowe-plain"]])
        code, report = self.run_shk("--executor", "ssh", "doctor", cwd=self.repo)
        self.assertEqual((code, report["problems"]), (0, []))
        self.assertNotIn("site", report)
        self.assertEqual(
            self.fake.calls()[-2:], [[*CHECK, "sherlock-plain"], [*CHECK, "sherlock-dtn"]]
        )


class DoctorTests(SiteCase):
    def setUp(self):
        super().setUp()
        for tool in ("sbatch", "squeue", "sacct"):
            (self.fake.bin / tool).write_text("#!/bin/sh\nexit 0\n")
            (self.fake.bin / tool).chmod(0o755)

    def doctor(self, *args, project_dir=None, site="marlowe", **knobs):
        self.knobs(**knobs)
        self.fake.record.unlink(missing_ok=True)
        return self.shk("doctor", *args, project_dir=project_dir or self.empty, site=site)

    def test_marlowe_without_a_configuration_checks_the_one_marlowe_master(self):
        code, report = self.doctor()
        self.assertEqual((code, report["ok"], report["problems"]), (0, True, []))
        self.assertEqual(report["masters"], {"control": "running", "data": "running"})
        keys = list(report)
        self.assertEqual(
            keys[keys.index("executor") : keys.index("executor") + 2], ["executor", "site"]
        )
        self.assertEqual((report["executor"], report["site"]), ("ssh", "marlowe"))
        self.assertEqual(self.fake.calls(), [[*CHECK, "marlowe-plain"]])

    def test_a_missing_marlowe_master_is_one_problem_and_no_sherlock_hint(self):
        code, report = self.doctor(FAKE_SSH_MASTER="none")
        self.assertEqual((code, report["masters"]), (4, {"control": "none", "data": "none"}))
        self.assertEqual(len(report["problems"]), 1)
        self.assertIn("ask the user to: ssh marlowe-plain", report["problems"][0])
        self.assertEqual(report["warnings"], [])
        code, report = self.doctor(FAKE_SSH_MASTER="blocked")
        self.assertEqual(report["masters"]["control"], "inaccessible")
        with patch.object(executor, "master_status", return_value=("timeout", "slow")):
            code, report = self.doctor()
        self.assertIn("run shk --site marlowe doctor;", report["problems"][0])

    def test_remote_doctor_runs_through_the_prelude_and_has_no_instructions_check(self):
        code, report = self.doctor("--remote", FAKE_SSH_BANNER="none")
        self.assertEqual((code, report["remote"]["status"]), (0, "complete"), report)
        remote = report["remote"]
        self.assertEqual(list(remote)[:3], ["status", "executor", "site"])
        self.assertEqual(remote["site"], "marlowe")
        self.assertNotIn("site_instructions", remote)
        self.assertEqual(report["warnings"], [])
        self.assertTrue(all(remote["tools"].values()), remote)
        dial = [call for call in self.fake.calls() if "-O" not in call]
        self.assertEqual(len(dial), 1)
        words = shlex.split(dial[0][-1])
        self.assertEqual(
            words[3:], ["shk-doctor", *(sites.MARLOWE.slurm.bin, sites.MARLOWE.slurm.conf)]
        )
        self.assertTrue(words[2].startswith(executor.program_text("site-env")))
        self.assertEqual(set(self.hosts()), {"marlowe-plain"})

    def test_a_site_mismatch_is_a_problem_without_master_checks(self):
        (self.empty / "sherlock-kit.toml").write_text('project = "demo"\n')
        code, report = self.doctor("--remote")
        self.assertEqual(
            (code, report["ok"], report["executor"], report["masters"]), (1, False, None, {})
        )
        self.assertNotIn("remote", report)
        self.assertEqual(report["config"]["status"], "found")
        self.assertEqual(len(report["problems"]), 1)
        self.assertIn("does not match", report["problems"][0])
        self.assertEqual(self.fake.calls(), [])

    def test_an_invalid_configuration_checks_the_defaults_of_the_flag_site(self):
        (self.empty / "sherlock-kit.toml").write_text("unknown = 1\n")
        code, report = self.doctor()
        self.assertEqual((code, report["config"]["status"]), (1, "config_invalid"))
        self.assertEqual(report["masters"], {"control": "running", "data": "running"})
        self.assertEqual(self.fake.calls(), [[*CHECK, "marlowe-plain"]])
        self.assertEqual(report["site"], "marlowe")
        code, report = self.doctor(site=None)
        self.assertEqual(self.hosts(), ["sherlock-plain", "sherlock-dtn"])
        self.assertNotIn("site", report)

    def test_a_marlowe_configuration_selects_marlowe_without_the_flag(self):
        marlowe = self.tmp / "marlowe"
        marlowe_config(marlowe)
        code, report = self.doctor(project_dir=marlowe, site=None)
        self.assertEqual((code, report["site"]), (0, "marlowe"))
        self.assertEqual(self.fake.calls(), [[*CHECK, "marlowe-plain"]])

    def test_sherlock_doctor_has_no_site_key(self):
        code, report = self.doctor("--remote", site=None)
        self.assertEqual(code, 0, report)
        self.assertNotIn("site", report)
        self.assertNotIn("site", report["remote"])
        self.assertIn("site_instructions", report["remote"])
        self.assertEqual(self.hosts(), ["sherlock-plain", "sherlock-dtn", *["sherlock-plain"] * 2])


class OfflineGateTests(SiteCase):
    """--site marlowe without a configuration names marlowe-plain everywhere and Sherlock nowhere."""

    def setUp(self):
        super().setUp()
        self.knobs(FAKE_SSH_BANNER="none")
        self.remote = self.tmp / "scratch" / "run"
        (self.remote / "sub").mkdir(parents=True)
        (self.remote / "a.txt").write_text("a\n")
        (self.remote / "sub" / "b.txt").write_text("b\n")
        subprocess.run(
            [
                "sh",
                "-c",
                'cd "$1" && sha256sum a.txt sub/b.txt > SHA256SUMS',
                "sh",
                str(self.remote),
            ],
            check=True,
        )
        (self.tmp / "results").mkdir()

    def marlowe(self, *argv):
        return self.shk(*argv, project_dir=self.empty, site="marlowe")

    def test_executors_doctor_and_rsync_targets(self):
        ctx = self.context(self.empty, "marlowe")
        self.assertEqual(
            (ctx.executor().host, ctx.data_executor().host), ("marlowe-plain", "marlowe-plain")
        )
        self.assertEqual(
            (ctx.executor().site.name, ctx.data_executor().site.name), ("marlowe", "marlowe")
        )
        code, report = self.marlowe("doctor")
        self.assertEqual((code, report["masters"]), (0, {"control": "running", "data": "running"}))
        local = self.tmp / "local"
        (local / "d").mkdir(parents=True)
        (local / "d" / "x").write_text("x\n")
        code, reply = self.marlowe("push", "--anywhere", str(local), str(self.tmp / "pushed"))
        self.assertEqual(
            (code, reply["target"]), (0, f"marlowe-plain:{self.tmp / 'pushed'}"), reply
        )
        self.assertNotIn("site", reply)  # transfer replies print no executor
        self.assertEqual((self.tmp / "pushed" / "d" / "x").read_text(), "x\n")
        code, reply = self.marlowe("pull", str(self.remote), str(self.tmp / "results" / "run"))
        self.assertEqual((code, reply["action"], reply["files"]), (0, "pulled", 2), reply)
        self.assertEqual(set(self.hosts()), {"marlowe-plain"})
        self.assertFalse(any("sherlock" in word for call in self.fake.calls() for word in call))

    def test_messages_name_marlowe(self):
        code, reply = self.marlowe(
            "pull", str(self.tmp / "absent"), str(self.tmp / "results" / "x")
        )
        self.assertEqual(
            (code, reply["error"], reply["message"]),
            (1, "manifest_unavailable", f"{self.tmp / 'absent'} is not a directory on Marlowe"),
        )
        self.knobs(FAKE_SSH_BANNER="none", FAKE_SSH_MODE="disconnect")
        code, reply = self.marlowe("pull", str(self.remote), str(self.tmp / "results" / "x"))
        self.assertEqual(
            (code, reply["error"], reply["hint"]),
            (4, "unavailable", "run shk --site marlowe doctor"),
        )
        self.assertTrue(reply["message"].startswith("read SHA256SUMS: Marlowe unreachable."))
        stub = executor.Result("complete", "SHK_COUNT 1\n", "", 0, site=sites.MARLOWE)
        with (
            patch.object(executor, "run_program", return_value=stub),
            self.assertRaises(ShkError) as caught,
        ):
            transfer._read_manifest(None, "/scratch/run")
        self.assertEqual(caught.exception.hint, "run shk --site marlowe doctor --remote")

    def test_on_marlowe_push_and_pull_are_refused(self):
        with marlowe_site(on_cluster=True):
            for argv in (
                ("push", "--anywhere", str(self.tmp), "/scratch/m000000/x"),
                ("pull", str(self.remote), str(self.tmp / "results" / "run")),
            ):
                for override in ("auto", "ssh"):
                    with self.subTest(argv=argv[0], executor=override):
                        code, reply = self.shk(
                            *argv, project_dir=self.empty, site="marlowe", executor=override
                        )
                        self.assertEqual((code, reply["error"]), (3, "transfer_on_marlowe"), reply)
        self.assertEqual(self.fake.calls(), [])


class SherlockIdentityTests(SiteCase):
    """Without --site and with today's configurations, every argv is the one of a kit without sites."""

    def test_rsync_argv_of_push_and_pull(self):
        project, remote = self.tmp / "project", self.tmp / "run"
        (project / "results").mkdir(parents=True)
        (project / "sherlock-kit.toml").write_text(f'remote_root = "{self.tmp}"\n')
        remote.mkdir()
        (remote / "a").write_text("a\n")
        subprocess.run(
            ["sh", "-c", 'cd "$1" && sha256sum a > SHA256SUMS', "sh", remote], check=True
        )
        self.knobs()
        seen = []
        with patch.object(transfer, "_rsync", lambda argv, timeout, stdin=None: seen.append(argv)):
            code, reply = self.shk("push", str(project), str(self.tmp / "r"), project_dir=project)
            self.assertEqual(code, 0, reply)
            code, reply = self.shk(
                "pull", str(remote), str(project / "results" / "run"), project_dir=project
            )
            self.assertEqual((code, reply["error"]), (1, "verify_failed"))  # nothing fetched
        ssh = shlex.join(["ssh", *(option.format(15) for option in executor.DIAL)])
        stage = project / "results" / ".run.shk-stage"
        self.assertEqual(
            seen,
            [
                [*transfer.PUSH, "-e", ssh, "--", f"{project}/", f"sherlock-dtn:{self.tmp / 'r'}/"],
                [*transfer.PULL, "-e", ssh, "--", f"sherlock-dtn:{remote}/", f"{stage}/"],
            ],
        )
        self.assertEqual(
            self.hosts(), ["sherlock-dtn", "sherlock-plain", "sherlock-plain", "sherlock-dtn"]
        )


if __name__ == "__main__":
    unittest.main()
