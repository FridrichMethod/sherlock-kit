"""The site key of replies, the selector in hints and the replayable exit 5 `next`, per site."""

import io
import json
import os
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from fakes import SRC, marlowe_config
from test_flows import NEXT, NOW, Flow

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, executor, sites, slurm, spec
from sherlock_kit.errors import ShkError


class SherlockReplies(Flow):
    """No --site and a Sherlock configuration: no "site" key, hints and `next` without a selector."""

    site = sites.SHERLOCK
    flag = None
    project = "demo"
    partition = "normal"
    shk_prefix = "shk"

    def shk(self, *argv):
        ctx = cli.Context(
            project_dir=self.tmp,
            executor=self.executor,
            site=self.flag,
            env={},
            hostname="lab-ubuntu",
            clock=lambda: NOW,
        )
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(list(argv), modules=[slurm], context=ctx)
        return code, json.loads(out.getvalue())

    def assert_site(self, reply):
        """A Marlowe reply has "site" right after "executor"; a Sherlock reply has no "site"."""
        if self.site is sites.SHERLOCK:
            self.assertNotIn("site", reply)
            return
        keys = list(reply)
        self.assertEqual(keys[keys.index("executor") + 1], "site", reply)
        self.assertEqual(reply["site"], self.site.name)

    def answering(self, stdout):
        """Every remote program answers stdout, from the site of this flow."""
        result = executor.Result("complete", stdout, "", 0, site=self.site)
        return patch.object(executor, "run_program", return_value=result)

    def test_status_and_cancel_replies(self):
        for argv in (
            ("status", "--task", "a"),
            ("status", "--task", "a", "--details"),
            ("status", "--all"),
            ("cancel", "--task", "a"),
        ):
            with self.subTest(argv=argv):
                code, reply = self.shk(*argv)
                self.assertEqual(code, 0, reply)
                self.assert_site(reply)
        self.slurm.add(
            spec.job_name(self.project, "default", "a"), "RUNNING", partition=self.partition
        )
        code, reply = self.shk("cancel", "--task", "a")
        self.assertEqual((code, reply["action"]), (0, "cancelled"))
        self.assert_site(reply)

    def test_release_requeue_and_update_replies(self):
        cases = (
            (
                ("release", "--job-id", "44", "--expect-token", "tok-1"),
                "SHK_CHANGED 44",
                "released",
            ),
            (("requeue", "--self"), "SHK_CHANGED 44", "requeued"),
            (("update", "--job-id", "44", "--time", "1h"), "SHK_UNCHANGED", "unchanged"),
        )
        for argv, answer, action in cases:
            with (
                self.subTest(command=argv[0]),
                self.answering(answer + "\n"),
                patch.dict(os.environ, SLURM_JOB_ID="44"),
            ):
                code, reply = self.shk(*argv, "--expect-name", "n")
                self.assertEqual((code, reply["action"]), (0, action), reply)
                self.assert_site(reply)

    def test_slurm_conf_reaches_the_clients_only_from_a_site_environment(self):
        self.assertEqual(self.shk("status", "--task", "a")[0], 0)
        env = self.slurm.calls("squeue")[0]["env"]
        self.assertEqual(env.get("SLURM_CONF"), self.site.slurm and self.site.slurm.conf)

    def test_exit_5_next_is_one_replayable_argv(self):
        self.slurm.add(
            spec.job_name(self.project, "default", "k"), "RUNNING", partition=self.partition
        )
        with patch.dict(os.environ, FAKE_SCANCEL_RC="1"):
            code, reply = self.shk("cancel", "--task", "k")
        self.assertEqual((code, reply["action"]), (5, "unknown"), reply)
        command = shlex.join(["shk", "--project-dir", str(self.tmp), *sites.selector(self.site)])
        self.assertEqual(
            reply["next"],
            f"{command} --executor local status --task k --campaign default (not before {NEXT})",
        )
        argv = shlex.split(reply["next"].rsplit(" (not before ", 1)[0])
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(argv[1:], modules=[slurm])  # its own flags build the Context
        replayed = json.loads(out.getvalue())
        self.assertEqual((code, replayed["names"][0]["state"]), (0, "live"), replayed)
        self.assert_site(replayed)

    def test_hints_name_the_site_command(self):
        live = f"wait; check {self.shk_prefix} status at most once per 60 s"
        with self.assertRaises(ShkError) as caught:
            slurm._refuse_existing(
                "n", {"state": "live", "current": {"job_id": "1"}}, None, self.site
            )
        self.assertEqual(caught.exception.hint, live)
        with self.answering("SHK_LIVE\n"):
            code, reply = self.shk("cancel", "--task", "k")
        self.assertEqual((code, reply["error"], reply["hint"]), (3, "live", live))
        with self.answering("SHK_USER u\n"):
            code, reply = self.shk("status", "--task", "k")
        self.assertEqual(
            (code, reply["error"], reply["hint"]),
            (
                1,
                "malformed_read",
                f"retry once after 60 s; {self.shk_prefix} doctor --remote checks the remote tools",
            ),
        )
        code, reply = self.shk(
            "update", "--job-id", "44", "--expect-name", "n", "--time", "1h", "--throttle", "2"
        )
        self.assertEqual(
            (code, reply["hint"]),
            (
                1,
                f"run one {self.shk_prefix} update per field: Slurm applies the fields of one "
                "update one by one, without rollback",
            ),
        )


class MarloweReplies(SherlockReplies):
    """--site marlowe and a Marlowe configuration: the same commands carry the site."""

    site = sites.MARLOWE
    flag = "marlowe"
    project = "demo-mw"
    partition = "preempt"
    shk_prefix = "shk --site marlowe"

    def config(self, **extra):
        values = {key: str(value) for key, value in extra.items()}
        marlowe_config(self.tmp, remote_root=f'"{self.remote}"', **values)

    def test_the_configuration_alone_selects_marlowe(self):
        self.flag = None
        code, reply = self.shk("status", "--task", "a")
        self.assertEqual((code, reply["site"]), (0, "marlowe"))


if __name__ == "__main__":
    unittest.main()
