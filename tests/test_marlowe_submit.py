"""Submit on Marlowe: the partition table, --account in the argv, the test before submit.

Every request runs the real remote programs against the fake Slurm (tests/fakes/fakeslurm.py),
whose sbatch --test-only branch gives Slurm's verdict. The refusal texts are those Marlowe
printed for sbatch --test-only (docs/validation/observed-marlowe.md); ids and names are
synthetic. The same requests on Sherlock keep the argv and the calls of a kit without sites.
"""

import io
import json
import os
import shlex
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from fakes import MARLOWE_ACCOUNT, SRC, marlowe_config
from test_flows import NEXT, NOW, Flow

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, executor, slurm, spec

PROJECT = "demo-mw"
NAME = spec.job_name(PROJECT, "default", "train-a")
SHERLOCK_NAME = spec.job_name("demo", "default", "train-a")
ACCOUNT_ARG = f"--account={MARLOWE_ACCOUNT}"
POLICY = (
    "allocation failure: Job violates accounting/QOS policy "
    "(job submit limit, user's size and/or time limits)\n"
)
REFUSALS = (  # what Marlowe printed for a refused sbatch --test-only
    "allocation failure: Requested time limit is invalid (missing or exceeds some limit)\n",
    "allocation failure: Invalid feature specification\n",
    "allocation failure: Requested node configuration is not available\n",
    "sbatch: error: MaxSubmitJobsPerAccount\n" + POLICY,
    "sbatch: error: MaxGRESPerAccount\n" + POLICY,
    "allocation failure: Invalid account or account/partition combination specified\n",
)
NOTE = "not a wait prediction on Marlowe; see the Marlowe rules"
PREFLIGHT_KEYS = [
    "schema_version",
    "ok",
    "action",
    "job_submitted",
    "name",
    "run_dir",
    "argv",
    "script_sha256",
    "spec",
    "preflight",
    "estimate",
    "executor",
    "site",
    "queried_at",
    "next_check_after",
]
SUBMITTED_KEYS = [
    "schema_version",
    "ok",
    "action",
    "job_id",
    "state_before",
    "name",
    "run_dir",
    "argv",
    "script_sha256",
    "executor",
    "site",
    "user",
    "host",
    "queried_at",
    "next_check_after",
]


def golden(name, run, partition, *, account=None, requeue=False, array=None, gpus=0, held=False):
    """The full sbatch argv of one request, spelled out: the contract these tests pin."""
    log = f"{run}/slurm-{'%A_%a' if array else '%j'}"
    argv = ["sbatch", "--parsable", f"--job-name={name}", f"--partition={partition}"]
    argv += [f"--account={account}"] if account else []
    argv += [f"--array={array}"] if array else []
    argv += ["--cpus-per-task=8" if gpus else "--cpus-per-task=1"]
    argv += ["--requeue", "--open-mode=append"] if requeue else ["--no-requeue"]
    argv += [
        "--ntasks=1",
        "--mem=65536M" if gpus else "--mem=2048M",
        "--time=120" if gpus else "--time=20",
        f"--chdir={run}",
        f"--output={log}.out",
        f"--error={log}.err",
    ]
    argv += ["-G", str(gpus)] if gpus else []
    token = ",SHK_SUBMISSION_TOKEN=tok-1" if held else ""
    argv.append(f"--export=ALL,SHK_NAME={name}{token}")
    argv += [] if array else ["--dependency=singleton"]
    argv += ["--comment=tok-1", "--hold"] if held else []
    return argv


REQUESTS = {  # label -> (extra submit arguments, golden keywords)
    "cpu": ((), {}),
    "gpu": (("--cpus", "8", "--mem", "64G", "--time", "2h", "--gpus", "1"), {"gpus": 1}),
    "array": (("--array", "4%2"), {"array": "0-3%2"}),
    "held": (("--submission-token", "tok-1", "--hold"), {"held": True}),
}


class OnMarlowe:
    """Mixed in before a Flow: a Marlowe configuration, and every command given --site marlowe."""

    modules = (slurm,)

    def config(self, **extra):
        values = {key: str(value) for key, value in extra.items()}
        marlowe_config(self.tmp, remote_root=f'"{self.remote}"', **values)

    def without_account(self):
        (self.tmp / "sherlock-kit.toml").write_text(
            f'site = "marlowe"\nproject = "{PROJECT}"\nremote_root = "{self.remote}"\n'
        )

    def shk(self, *argv):
        ctx = cli.Context(
            project_dir=self.tmp,
            executor=self.executor,
            site="marlowe",
            env={},
            hostname="lab-ubuntu",
            clock=lambda: NOW,
        )
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(list(argv), modules=list(self.modules), context=ctx)
        return code, json.loads(out.getvalue())

    def submit(self, *extra, task="train-a", partition="preempt", script="job.sh"):
        return Flow.submit(self, *extra, task=task, partition=partition, script=script)

    def run_dir(self, name=NAME):
        return f"{self.remote}/runs/{name}"

    def sbatch_calls(self):
        """The fake sbatch calls: (test-only argv, real argv), each without its last word."""
        calls = [call["argv"] for call in self.slurm.calls("sbatch")]
        tested = [argv[:-1] for argv in calls if argv[-1] == "--test-only"]
        return tested, [argv[:-1] for argv in calls if argv[-1] != "--test-only"]


class MarloweArgv(OnMarlowe, Flow):
    def test_golden_argv_with_the_account_after_the_partition(self):
        for label, (extra, keywords) in REQUESTS.items():
            with self.subTest(request=label):
                code, out = self.submit(*extra, "--dry-run")
                self.assertEqual((code, out["action"]), (0, "dry_run"), out)
                expected = golden(
                    NAME,
                    self.run_dir(),
                    "preempt",
                    account=MARLOWE_ACCOUNT,
                    requeue=True,
                    **keywords,
                )
                self.assertEqual(out["argv"], expected)
                self.assertEqual(out["argv"][4], ACCOUNT_ARG)
                self.assertEqual(out["spec"]["argv"], expected)
                self.assertNotIn("account", out["spec"]["resources"])
                self.assertNotIn("account", out["spec"])
        self.assertEqual(self.slurm.calls(), [])

    def test_requeue_is_the_preempt_default_unless_no_requeue(self):
        code, out = self.submit("--dry-run")
        self.assertEqual(out["argv"][6:8], ["--requeue", "--open-mode=append"])
        self.assertTrue(out["spec"]["resources"]["requeue"])
        code, out = self.submit("--no-requeue", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(out["argv"][6], "--no-requeue")
        self.assertNotIn("--requeue", out["argv"])
        self.assertFalse(out["spec"]["resources"]["requeue"])
        self.assertEqual(self.submit("--requeue", "--dry-run")[1]["argv"][6], "--requeue")

    def test_only_preempt_is_listed(self):
        for partition in ("batch", "hero", "normal", "preempt,batch"):
            with self.subTest(partition=partition):
                code, out = self.submit("--dry-run", partition=partition)
                self.assertEqual((code, out["error"]), (3, "unknown_partition"), out)
                self.assertTrue(out["message"].endswith("is not listed; listed: preempt"))
        for argv in (
            ("resources", "--partition", "batch"),
            ("occupancy", "--partition", "hero"),
            ("update", "--job-id", "44", "--expect-name", NAME, "--partition", "batch"),
        ):
            with self.subTest(command=argv[0]):
                code, out = self.shk(*argv)
                self.assertEqual((code, out["error"]), (3, "unknown_partition"), out)
                self.assertTrue(out["message"].endswith("listed: preempt"))
        self.assertEqual(self.slurm.calls(), [])

    def test_an_account_directive_stays_reserved(self):
        (self.tmp / "job.sh").write_text("#!/bin/bash\n#SBATCH --account=marlowe-m000000\necho\n")
        code, out = self.submit("--dry-run")
        self.assertEqual((code, out["error"]), (3, "sbatch_directive"))
        self.assertIn("#SBATCH --account=marlowe-m000000 is reserved to shk", out["message"])
        self.assertIn("use the shk --site marlowe submit command line", out["hint"])


class MarloweAccount(OnMarlowe, Flow):
    def test_a_missing_account_refuses_every_submit_form(self):
        self.without_account()
        path = self.tmp / "sherlock-kit.toml"
        for extra in (("--dry-run",), ("--preflight",), ()):
            with self.subTest(extra=extra):
                code, out = self.submit(*extra)
                self.assertEqual((code, out["error"]), (1, "config_required"), out)
                self.assertEqual(
                    out["message"],
                    f"{path}: missing account: every Marlowe job names its project account, "
                    "and shk never guesses it",
                )
                self.assertEqual(
                    out["hint"],
                    "read it over the open master with ssh -oBatchMode=yes "
                    "-oProxyCommand=false marlowe-plain 'echo marlowe-$PROJECT_ID', then add "
                    f'account = "<that value>" to {path}',
                )
        self.assertEqual(self.slurm.calls(), [])
        self.assertFalse(self.remote.exists())

    def test_the_account_is_never_taken_from_the_environment(self):
        self.without_account()
        with patch.dict(os.environ, PROJECT_ID="m000000", SBATCH_ACCOUNT=MARLOWE_ACCOUNT):
            code, out = self.submit("--dry-run")
        self.assertEqual((code, out["error"]), (1, "config_required"))

    def test_the_hint_names_the_configured_control_host(self):
        self.without_account()
        path = self.tmp / "sherlock-kit.toml"
        path.write_text(path.read_text() + 'control_host = "mw-login"\ndata_host = "mw-login"\n')
        code, out = self.submit("--dry-run")
        self.assertEqual((code, out["error"]), (1, "config_required"))
        self.assertIn("-oProxyCommand=false mw-login 'echo marlowe-$PROJECT_ID'", out["hint"])


class SherlockArgv(Flow):
    """The same requests on Sherlock partitions: today's argv, no --account, no test-only call."""

    def test_golden_argv_without_an_account(self):
        for label, (extra, keywords) in REQUESTS.items():
            partition = "gpu" if keywords.get("gpus") else "normal"
            with self.subTest(request=label):
                code, out = self.submit(*extra, "--dry-run", partition=partition)
                self.assertEqual(code, 0, out)
                run = f"{self.remote}/runs/{SHERLOCK_NAME}"
                self.assertEqual(out["argv"], golden(SHERLOCK_NAME, run, partition, **keywords))
                self.assertFalse([word for word in out["argv"] if word.startswith("--account")])

    def test_preempt_is_not_a_sherlock_partition(self):
        code, out = self.submit("--dry-run", partition="preempt")
        self.assertEqual((code, out["error"]), (3, "unknown_partition"))
        self.assertIn("listed: normal, bigmem", out["message"])

    def test_submit_sends_no_test_only_call(self):
        code, out = self.submit()
        self.assertEqual((code, out["action"]), (0, "submitted"), out)
        self.assertNotIn("site", out)
        self.assertEqual(
            [call["tool"] for call in self.slurm.calls()], ["squeue", "sacct", "squeue", "sbatch"]
        )
        self.assertEqual(self.slurm.calls("sbatch")[0]["argv"][:-1], out["argv"][1:])

    def test_preflight_carries_no_estimate_note(self):
        code, out = self.submit("--preflight")
        self.assertEqual((code, out["estimate"]["status"]), (0, "parsed"), out)
        self.assertNotIn("note", out["estimate"])
        self.assertNotIn("site", out)


class TestBeforeSubmit(OnMarlowe, Flow):
    def entry_state(self):
        code, report = self.shk("status", "--task", "train-a")
        self.assertEqual((code, report["site"]), (0, "marlowe"), report)
        return report["names"][0]["state"]

    def test_accepted_the_submit_goes_on_with_one_test_only_call(self):
        code, out = self.submit("--gpus", "1")
        self.assertEqual((code, out["action"], out["site"]), (0, "submitted", "marlowe"), out)
        self.assertEqual(list(out), SUBMITTED_KEYS)
        self.assertEqual(
            [call["tool"] for call in self.slurm.calls()],
            ["squeue", "sacct", "sbatch", "squeue", "sbatch"],
        )
        tested, submitted = self.sbatch_calls()
        self.assertEqual((tested, submitted), ([out["argv"][1:]], [out["argv"][1:]]))
        self.assertIn(ACCOUNT_ARG, submitted[0])
        shk_dir = self.remote / "runs" / NAME / ".shk"
        self.assertEqual(
            sorted(path.name for path in shk_dir.iterdir()),
            [f"{NAME}.{out['job_id']}.json", f"{NAME}.{out['job_id']}.sh"],
        )
        self.assertEqual(self.entry_state(), "live")

    def test_refused_the_preflight_reply_returns_unchanged_and_nothing_is_staged(self):
        for message in REFUSALS:
            with (
                self.subTest(message=message.splitlines()[0]),
                patch.dict(os.environ, FAKE_SBATCH_TEST_RC="1", FAKE_SBATCH_TEST_ERR=message),
            ):
                code, out = self.submit()
                self.assertEqual(
                    (code, out["ok"], out["action"], out["job_submitted"]),
                    (3, False, "preflight", False),
                    out,
                )
                self.assertEqual(list(out), PREFLIGHT_KEYS)
                self.assertEqual(out["site"], "marlowe")
                self.assertEqual(
                    out["preflight"], {"returncode": 1, "stdout": "", "stderr": message}
                )
                self.assertEqual(
                    (out["estimate"]["status"], out["estimate"]["note"]), ("absent", NOTE)
                )
                self.assertEqual(self.submit("--preflight"), (code, out))
        tested, submitted = self.sbatch_calls()
        self.assertEqual((len(tested), submitted), (2 * len(REFUSALS), []))
        self.assertEqual(self.slurm.state()["jobs"], [])
        self.assertFalse(self.remote.exists())
        self.assertEqual(self.entry_state(), "new")

    def test_the_refusal_comes_after_the_duplicate_read(self):
        with patch.dict(os.environ, FAKE_SBATCH_TEST_RC="1"):
            self.submit()
        self.assertEqual(
            [call["tool"] for call in self.slurm.calls()], ["squeue", "sacct", "sbatch"]
        )

    def test_a_live_name_is_refused_before_the_test(self):
        self.slurm.add(NAME, "RUNNING", partition="preempt")
        code, out = self.submit()
        self.assertEqual((code, out["error"]), (3, "live"))
        self.assertEqual(self.sbatch_calls(), ([], []))

    def test_a_controller_failure_in_the_test_is_a_failed_read(self):
        failure = "allocation failure: Unable to contact slurm controller (connect failure)\n"
        with patch.dict(os.environ, FAKE_SBATCH_TEST_RC="1", FAKE_SBATCH_TEST_ERR=failure):
            code, out = self.submit()
        self.assertEqual((code, out["error"]), (1, "malformed_read"))
        self.assertEqual(self.sbatch_calls()[1], [])
        self.assertFalse(self.remote.exists())

    def test_a_lost_test_is_exit_4_and_dispatches_no_submit(self):
        real, programs = executor.run_program, []

        def lose_the_test(status):
            def run_program(ex, name, args=(), **options):
                programs.append(name)
                if name == "preflight":
                    lost = "Connection closed by 192.0.2.1 port 22\n"
                    return executor.Result(status, "", lost, 255, site=ex.site)
                return real(ex, name, args, **options)

            return patch.object(executor, "run_program", run_program)

        hint = "run shk --site marlowe doctor"
        for status in ("unavailable", "auth_required"):
            programs.clear()
            with self.subTest(status=status), lose_the_test(status):
                code, out = self.submit()
                self.assertEqual((code, out["error"], out["hint"]), (4, status, hint), out)
                self.assertEqual(programs, ["query", "preflight"])  # no submit.sh
        self.config(command_timeout=2)
        with patch.dict(os.environ, FAKE_SBATCH_HANG="30"):  # the test-only sbatch never returns
            code, out = self.submit()
        self.assertEqual((code, out["error"], out["hint"]), (4, "timeout", hint), out)
        tested, submitted = self.sbatch_calls()
        self.assertEqual((len(tested), submitted), (1, []))
        self.assertEqual(self.slurm.state()["jobs"], [])
        self.assertFalse(self.remote.exists())

    def test_preflight_alone_is_one_test_with_the_estimate_note(self):
        code, out = self.submit("--preflight")
        self.assertEqual((code, out["ok"]), (0, True), out)
        self.assertEqual(list(out), PREFLIGHT_KEYS)
        estimate = out["estimate"]
        self.assertEqual(
            list(estimate),
            ["status", "start", "start_zone", "nodes", "partition", "line", "observed_at", "note"],
        )
        self.assertEqual(
            (estimate["status"], estimate["start"], estimate["partition"], estimate["note"]),
            ("parsed", "2026-11-23T01:34:08", "preempt", NOTE),
        )
        self.assertEqual([call["tool"] for call in self.slurm.calls()], ["sbatch"])
        self.assertEqual(self.sbatch_calls(), ([out["argv"][1:]], []))
        self.assertFalse(self.remote.exists())

    def test_exit_5_next_replays_with_the_site_and_reads_the_job(self):
        with patch.dict(os.environ, FAKE_SBATCH_SUBMIT_THEN_FAIL="1"):
            code, out = self.submit()
        self.assertEqual((code, out["action"]), (5, "unknown"), out)
        self.assertEqual(
            out["next"],
            f"shk --project-dir {self.tmp} --site marlowe --executor local status "
            f"--task train-a --campaign default (not before {NEXT})",
        )
        argv = shlex.split(out["next"].rsplit(" (not before ", 1)[0])
        self.assertEqual(argv[:5], ["shk", "--project-dir", str(self.tmp), "--site", "marlowe"])
        replay = io.StringIO()
        with redirect_stdout(replay), redirect_stderr(io.StringIO()):
            code = cli.main(argv[1:], modules=[slurm])  # its own flags build the Context
        replayed = json.loads(replay.getvalue())
        self.assertEqual(code, 0, replayed)
        self.assertEqual(replayed["site"], "marlowe")
        entry = replayed["names"][0]
        self.assertEqual((entry["name"], entry["state"]), (NAME, "live"))
        self.assertEqual(entry["current"]["anomalies"], [])


if __name__ == "__main__":
    unittest.main()
