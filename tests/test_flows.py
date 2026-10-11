"""End-to-end flows: the real remote sh programs (/bin/sh, dash here) against the fake Slurm."""

import io
import json
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fakes import SRC, install_fake_ssh
from fakes.fakeslurm import FakeSlurm

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, executor, partitions, slurm, spec

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
NEXT = "2026-10-10T12:01:00Z"
SCRIPT = "#!/bin/bash -l\n#SBATCH --mail-type=END\necho work\n"
USER = pwd.getpwuid(os.getuid()).pw_name
REMOTE = SRC / "sherlock_kit" / "data" / "remote"
PROGRAMS = sorted(REMOTE.glob("*.sh"))


class Flow(unittest.TestCase):
    executor = "local"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.remote = self.tmp / "remote"
        self.config(command_timeout=30)
        (self.tmp / "job.sh").write_text(SCRIPT)
        self.slurm = FakeSlurm(self.tmp)
        self.knobs(**self.slurm.env())

    def config(self, **extra):
        lines = ['project = "demo"', f'remote_root = "{self.remote}"']
        lines += [f"{key} = {value}" for key, value in extra.items()]
        (self.tmp / "sherlock-kit.toml").write_text("\n".join(lines) + "\n")

    def knobs(self, **values):
        environment = patch.dict(os.environ, values)
        environment.start()
        self.addCleanup(environment.stop)

    def shk(self, *argv):
        ctx = cli.Context(
            project_dir=self.tmp,
            executor=self.executor,
            env={},
            hostname="lab-ubuntu",
            clock=lambda: NOW,
        )
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(list(argv), modules=[slurm], context=ctx)
        return code, json.loads(out.getvalue())

    def submit(self, *extra, task="train-a", partition="normal", script="job.sh"):
        return self.shk(
            "submit",
            "--task",
            task,
            "--script",
            str(self.tmp / script),
            "--partition",
            partition,
            "--cpus",
            "1",
            "--mem",
            "2G",
            "--time",
            "20m",
            *extra,
        )

    def entry(self, task="train-a", code=0, *extra):
        found, report = self.shk("status", "--task", task, *extra)
        self.assertEqual(found, code)
        return report["names"][0]

    def shk_dir(self, task="train-a"):
        return self.remote / "runs" / spec.job_name("demo", "default", task) / ".shk"


class SubmitFlows(Flow):
    def test_submit_stages_files_and_refuses_live_then_done(self):
        self.knobs(
            SBATCH_ACCOUNT="leaky", SLURM_MEM_PER_NODE="999", SLURM_CONF="/etc/slurm/slurm.conf"
        )
        code, out = self.submit()
        self.assertEqual(
            (code, out["action"], out["next_check_after"], out["executor"]),
            (0, "submitted", NEXT, "local"),
        )
        name, job = out["name"], out["job_id"]
        staged = sorted(path.name for path in self.shk_dir().iterdir())
        self.assertEqual(staged, [f"{name}.{job}.json", f"{name}.{job}.sh"])
        self.assertEqual((self.shk_dir() / f"{name}.{job}.sh").read_text(), SCRIPT)
        frozen = json.loads((self.shk_dir() / f"{name}.{job}.json").read_text())
        self.assertEqual(frozen["argv"], out["argv"])
        sbatch = self.slurm.calls("sbatch")[0]
        self.assertEqual(sbatch["argv"][:-1], out["argv"][1:])
        staged_script = rf"/\.shk/{name}\.20261010T120000Z-[0-9a-f]{{4}}\.sh\Z"
        self.assertRegex(sbatch["argv"][-1], staged_script)
        conf = {"SLURM_CONF": "/etc/slurm/slurm.conf"}
        sacct_env = {**conf, "SLURM_TIME_FORMAT": "standard"}
        seen = [call["env"] for call in self.slurm.calls()]  # squeue, sacct, squeue, sbatch
        self.assertEqual(seen, [conf, sacct_env, conf, conf])
        self.assertEqual(self.slurm.state()["jobs"][0]["script"], SCRIPT)
        refusal = {
            "ok": False,
            "error": "live",
            "message": f"{name} is live (newest job {job})",
            "hint": "wait; check shk status at most once per 60 s",
        }
        for extra in ((), ("--resubmit", "again")):
            self.assertEqual(self.submit(*extra), (3, refusal))
        self.slurm.set(int(job), "COMPLETED")
        self.assertEqual(self.submit()[1]["error"], "done")
        code, again = self.submit("--resubmit", "rerun with more data")
        self.assertEqual((code, again["state_before"]), (0, "done"))
        frozen = json.loads((self.shk_dir() / f"{name}.{again['job_id']}.json").read_text())
        self.assertEqual(frozen["resubmit_reason"], "rerun with more data")
        entry = self.entry()
        self.assertEqual(
            (entry["state"], entry["current"]["job_id"], entry["spec_files"]),
            ("live", again["job_id"], 2),
        )
        history = entry["history"][0]
        self.assertEqual((history["job_id"], history["state"]), (job, "done"))

    def test_rejections_keep_nothing_and_a_queued_failure_stays_live(self):
        self.knobs(FAKE_SBATCH_RC="1")
        code, out = self.submit()
        self.assertEqual((code, out["error"]), (3, "slurm_rejected"))
        self.assertIn("Invalid partition name", out["message"])
        self.assertEqual(list(self.shk_dir().iterdir()), [])
        self.knobs(FAKE_SBATCH_RC="", FAKE_SBATCH_SUBMIT_THEN_FAIL="1")
        self.assertEqual(self.submit()[1]["error"], "slurm_rejected")
        self.knobs(FAKE_SBATCH_SUBMIT_THEN_FAIL="")
        self.assertEqual(self.submit("--resubmit", "retry")[1]["error"], "live")

    def test_failed_needs_a_resubmit_reason(self):
        self.slurm.set(int(self.submit()[1]["job_id"]), "FAILED")
        code, out = self.submit()
        self.assertEqual(
            (code, out["error"], out["hint"]),
            (3, "failed", "find the cause, then add --resubmit REASON"),
        )
        self.assertEqual(self.submit("--resubmit", "fixed the input")[1]["state_before"], "failed")

    def test_unparsed_and_hung_replies_are_unknown_and_status_finds_the_job(self):
        self.knobs(FAKE_SBATCH_UNPARSED="1")
        code, out = self.submit(task="c")
        self.assertEqual((code, out["action"], out["ok"]), (5, "unknown", False))
        self.assertEqual(out["next"], f"shk status --task c --campaign default (not before {NEXT})")
        self.assertEqual((self.entry("c")["state"], self.entry("c")["spec_files"]), ("live", 1))
        self.config(command_timeout=3)
        self.knobs(FAKE_SBATCH_UNPARSED="", FAKE_SBATCH_HANG="30", FAKE_SBATCH_CLUSTER="sherlock")
        code, out = self.submit(task="slow task")
        self.assertEqual((code, out["action"]), (5, "unknown"))
        self.assertIn("--task 'slow task'", out["next"])
        self.knobs(FAKE_SBATCH_HANG="")
        self.assertEqual(self.entry("slow task")["state"], "live")
        self.assertEqual(self.submit(task="cluster")[1]["job_id"], "1003")

    def test_remote_guards_refuse_before_sbatch(self):
        run, live = self.tmp / "run", self.slurm.add("shk.demo.t.0123456789", "RUNNING")
        args = [
            "shk.demo.t.0123456789",
            str(run),
            "20261010T120000Z-abcd",
            "0" * 64,
            "{}",
            "--",
            "sbatch",
            "--parsable",
        ]
        local = executor.LocalExecutor()
        reply = executor.run_program(local, "submit", args, stdin=b"x")
        self.assertEqual(reply.stdout, f"SHK_LIVE {live}\n")
        args[0] = "shk.demo.u.0123456789"
        reply = executor.run_program(local, "submit", args, stdin=b"#!/bin/sh\n")
        self.assertEqual(reply.stdout, "SHK_REFUSED digest\n")
        self.assertEqual(list((run / ".shk").iterdir()), [])
        reply = executor.run_program(local, "submit", [*args[:5], "sbatch"])
        self.assertEqual(reply.stdout, "SHK_REFUSED usage\n")
        self.assertEqual(self.slurm.calls("sbatch"), [])

    def test_offline_refusals_and_dry_run_make_no_call(self):
        code, out = self.submit("--dry-run", "--array", "4%2")
        self.assertEqual((code, out["action"], out["argv"][4]), (0, "dry_run", "--array=0-3%2"))
        self.assertEqual(
            (out["spec"]["argv"], out["spec"]["script"]["sha256"]),
            (out["argv"], out["script_sha256"]),
        )
        after = self.submit("--dry-run", "--after-ok", "11", "12_3", "--after-ok", "13")[1]
        self.assertEqual(after["argv"][-1], "--dependency=afterok:11:12_3:13,singleton")
        (self.tmp / "bad.sh").write_text("#!/bin/sh\n#SBATCH --partition=gpu\necho\n")
        self.assertEqual(self.submit(script="bad.sh")[1]["error"], "sbatch_directive")
        self.assertEqual(self.submit(partition="btrippe")[1]["error"], "courtesy_ack_required")
        self.assertEqual(self.submit("--requeue")[1]["error"], "requeue_not_allowed")
        self.assertEqual((self.slurm.calls(), self.remote.exists()), ([], False))

    def test_borrowed_submit_reports_occupancy_courtesy_and_budget(self):
        self.slurm.add(
            "theirs", "RUNNING", user="alice", partition="btrippe", tres="cpu=4,gres/gpu=2"
        )
        for index in range(2):
            self.slurm.add(f"mine-{index}", "PENDING", partition="btrippe")
        code, out = self.submit("--courtesy-ack", partition="btrippe")
        self.assertEqual(code, 0)
        self.assertEqual(
            out["occupancy"]["users"]["alice"],
            {"running_jobs": 1, "running_gpus": 2, "pending_jobs": 0},
        )
        self.assertEqual(out["courtesy"], partitions.profile("btrippe").courtesy)
        self.assertEqual(len(out["warnings"]), 1)
        self.assertIn("about 2 jobs x 2 h", out["warnings"][0])


class StatusCancelOccupancyFlows(Flow):
    def test_array_status_counts_throttle_held_tasks_as_pending(self):
        out = self.submit("--array", "4%2")[1]
        job = int(out["job_id"])
        self.slurm.set(job, "COMPLETED", "RUNNING")
        current = self.entry()["current"]
        tasks = [current["tasks"][key] for key in ("completed", "running", "pending", "missing")]
        self.assertEqual(tasks, [1, 1, 2, 0])
        self.assertEqual(current["array"], {"count": 4, "count_known": True})
        self.slurm.set(job, None, "COMPLETED", "COMPLETED", "COMPLETED")
        entry = self.entry()
        self.assertEqual(
            (entry["state"], entry["current"]["cost"]),
            ("done", {"cpu_seconds": 240, "gpu_seconds": 0, "final": True}),
        )
        self.slurm.add("shk.demo-x.t.0123456789", "RUNNING")
        self.slurm.add("shk.demo.theirs.0123456789", "RUNNING", user="alice")
        code, report = self.shk("status", "--all")
        self.assertEqual(
            (code, [e["name"] for e in report["names"]], report["truncated"]),
            (0, [out["name"]], False),
        )
        refused = self.shk("status", "--name", out["name"], "--since", "0")[1]
        self.assertEqual(refused["error"], "spec_invalid")

    def test_status_pairs_each_campaign_with_its_task(self):
        first = self.submit("--campaign", "x", task="a")[1]
        second = self.submit("--campaign", "y", task="b")[1]
        code, report = self.shk(
            "status", "--task", "a", "--campaign", "x", "--task", "b", "--campaign", "y"
        )
        found = [(e["task"], e["campaign"], e["name"], e["state"]) for e in report["names"]]
        self.assertEqual(
            (code, found),
            (0, [("a", "x", first["name"], "live"), ("b", "y", second["name"], "live")]),
        )
        shared = self.shk("status", "--task", "a", "--task", "b", "--campaign", "x")[1]
        self.assertEqual(
            [(e["campaign"], e["state"]) for e in shared["names"]], [("x", "live"), ("x", "new")]
        )
        code, refused = self.shk(
            "status",
            "--task",
            "a",
            "--task",
            "b",
            "--campaign",
            "x",
            "--campaign",
            "y",
            "--campaign",
            "z",
        )
        self.assertEqual((code, refused["error"]), (1, "spec_invalid"))

    def test_status_all_without_a_config_keeps_every_shk_name(self):
        (self.tmp / "sherlock-kit.toml").unlink()
        self.slurm.add("shk.demo.t.0123456789", "RUNNING")
        self.slurm.add("shk.other.t.0123456789", "COMPLETED")
        self.slurm.add("not-shk", "RUNNING")
        self.slurm.add("shk.theirs.t.0123456789", "RUNNING", user="alice")
        code, report = self.shk("status", "--all")
        found = [(entry["name"], entry["state"]) for entry in report["names"]]
        self.assertEqual(
            (code, found),
            (0, [("shk.demo.t.0123456789", "live"), ("shk.other.t.0123456789", "done")]),
        )

    def test_jobs_outside_the_sacct_window_are_forgotten(self):
        self.slurm.add(spec.job_name("demo", "default", "old"), "COMPLETED", age_days=40)
        self.assertEqual(self.entry("old")["state"], "new")
        self.assertEqual(self.entry("old", 0, "--since", "60")["state"], "done")
        self.assertEqual(self.submit(task="old")[1]["state_before"], "new")

    def test_stale_spec_files_submit_like_new_and_anomalies_exit_6(self):
        self.shk_dir("old").mkdir(parents=True)
        name = spec.job_name("demo", "default", "old")
        (self.shk_dir("old") / f"{name}.77.json").write_text('{"resources":{"array":null}}\n')
        stale = self.entry("old")
        self.assertEqual((stale["state"], stale["spec_files"]), ("stale", 1))
        self.assertEqual(self.submit(task="old")[1]["state_before"], "stale")
        self.slurm.requeue(int(self.submit(task="pre")[1]["job_id"]))
        anomalies = self.entry("pre", code=6)["current"]["anomalies"]
        self.assertEqual(anomalies, ["unexpected_preemption"])

    def test_cancel_is_scoped_to_one_name(self):
        out, other = self.submit()[1], self.submit(task="keep")[1]
        self.knobs(SCANCEL_STATE="RUNNING", SCANCEL_INTERACTIVE="1")
        code, cancelled = self.shk("cancel", "--task", "train-a")
        self.assertEqual(
            (code, cancelled["action"], cancelled["jobs"]), (0, "cancelled", [out["job_id"]])
        )
        self.assertEqual((cancelled["next_check_after"], cancelled["executor"]), (NEXT, "local"))
        scancel = self.slurm.calls("scancel")[0]
        self.assertEqual(
            (scancel["argv"], scancel["env"]), (["-u", USER, f"--name={out['name']}"], {})
        )
        states = {job["name"]: job["tasks"][0][-1]["state"] for job in self.slurm.state()["jobs"]}
        self.assertEqual(states, {out["name"]: "CANCELLED", other["name"]: "PENDING"})
        self.assertEqual(self.shk("cancel", "--name", out["name"])[1]["action"], "nothing_live")
        self.knobs(FAKE_SCANCEL_RC="1")
        self.assertEqual(self.shk("cancel", "--task", "keep")[1]["error"], "slurm_rejected")
        self.assertEqual(self.shk("cancel", "--name", "not-a-shk-name")[0], 1)

    def test_occupancy_works_without_a_config(self):
        (self.tmp / "sherlock-kit.toml").unlink()
        self.slurm.add(
            "x", "RUNNING", user="alice", partition="owners", tres="cpu=1,gres/gpu:h100=1"
        )
        self.slurm.add("y", "PENDING", user="bob", partition="owners")
        code, out = self.shk("occupancy", "--partition", "owners", "--partition", "normal")
        self.assertEqual((code, out["next_check_after"]), (0, NEXT))
        owners, normal = out["partitions"]["owners"], out["partitions"]["normal"]
        self.assertEqual(
            owners["totals"], {"running_jobs": 1, "running_gpus": 1, "pending_jobs": 1}
        )
        self.assertEqual((normal["users"], owners["profile"]["preemptible"]), ({}, True))
        self.assertEqual(
            self.shk("occupancy", "--partition", "mystery")[1]["error"], "unknown_partition"
        )


class CannedReplyFlows(Flow):
    """Remote replies the fake Slurm cannot produce, handed straight to the handlers."""

    def setUp(self):
        super().setUp()
        self.replies = []

        def run_program(*_args, **_kwargs):
            return executor.Result("complete", self.replies.pop(0))

        patcher = patch.object(executor, "run_program", run_program)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_mutation_markers_map_to_exit_codes(self):
        empty = "SHK_USER u\nSHK_RC squeue 0\nSHK_RC sacct 0\nSHK_END\n"
        cases = (
            ("", 5, "unknown"),
            ("SHK_SUBMITTED 5\nSHK_SUBMITTED 6\n", 5, "unknown"),
            ("SHK_UNPARSED\n", 5, "unknown"),
            ("SHK_REFUSED mkdir\n", 3, "remote_refused"),
            ("SHK_LIVE 9\n", 3, "live"),
            ("SHK_SUBMITTED 7\n", 0, "submitted"),
        )
        for reply, code, outcome in cases:
            with self.subTest(reply=reply):
                self.replies[:] = [empty, reply]
                found, out = self.submit()
                self.assertEqual(
                    (found, out.get("error", out.get("action")), self.replies), (code, outcome, [])
                )
        self.replies[:] = ["SHK_JOB|7\n"]
        self.assertEqual(
            self.shk("cancel", "--task", "train-a"),
            (
                5,
                {
                    "ok": False,
                    "action": "unknown",
                    "name": spec.job_name("demo", "default", "train-a"),
                    "detail": "0 terminal markers",
                    "next": f"shk status --task train-a --campaign default (not before {NEXT})",
                },
            ),
        )

    def test_a_name_with_only_unreadable_rows_is_never_new(self):
        name = spec.job_name("demo", "default", "train-a")
        hetjob = [
            f"SHK_A|4242+{k}|COMPLETED|normal|60|2|cpu=2|2026-10-10T12:00:00|0|0:0|{name}"
            for k in range(2)
        ]
        read = "\n".join(["SHK_USER u", *hetjob, "SHK_RC squeue 0", "SHK_RC sacct 0", "SHK_END"])
        for argv in (
            (
                "submit",
                "--task",
                "train-a",
                "--script",
                str(self.tmp / "job.sh"),
                "--partition",
                "normal",
                "--cpus",
                "1",
                "--mem",
                "2G",
                "--time",
                "20m",
            ),
            ("status", "--task", "train-a"),
            ("status", "--all"),
        ):
            with self.subTest(argv=argv[:2]):
                self.replies[:] = [read]
                code, out = self.shk(*argv)
                self.assertEqual((code, out["error"], self.replies), (1, "malformed_read", []))
                self.assertIn(name, out["message"])


class SshFlow(Flow):
    executor = "ssh"

    def test_submit_and_status_through_fake_ssh_exec_mode(self):
        fake = install_fake_ssh(self.tmp)
        self.knobs(**fake.env())
        code, out = self.submit()
        self.assertEqual((code, out["executor"]), (0, "ssh"))
        self.assertEqual(self.entry()["state"], "live")
        dials = [call for call in fake.calls() if "-O" not in call]
        self.assertEqual([call[-2] for call in dials], ["sherlock-plain"] * 3)


class BashAsShFlow(Flow):
    """Sherlock's sh is bash in POSIX mode: the same programs must behave the same there."""

    def setUp(self):
        super().setUp()
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("needs bash")
        shim = self.tmp / "bash-as-sh"
        shim.mkdir()
        (shim / "sh").symlink_to(bash)
        self.knobs(PATH=f"{shim}{os.pathsep}{os.environ['PATH']}")

    def test_array_submit_refusal_status_and_cancel(self):
        self.assertEqual(Path(shutil.which("sh")).resolve(), Path(shutil.which("bash")).resolve())
        self.knobs(SBATCH_PARTITION="gpu", SLURM_JOB_ID="7")
        code, out = self.submit("--array", "4%2")
        self.assertEqual((code, out["action"]), (0, "submitted"))
        seen = {key for call in self.slurm.calls() for key in call["env"]}
        self.assertEqual(seen, {"SLURM_TIME_FORMAT"})
        self.assertEqual(self.submit("--array", "4%2")[1]["error"], "live")
        self.slurm.set(int(out["job_id"]), "COMPLETED", "RUNNING")
        tasks = self.entry()["current"]["tasks"]
        self.assertEqual([tasks[key] for key in ("completed", "running", "pending")], [1, 1, 2])
        self.assertEqual(self.shk("cancel", "--task", "train-a")[1]["action"], "cancelled")


class RemoteProgramTests(unittest.TestCase):
    """shellcheck -s sh runs in pre-commit; here dash parses each program."""

    @unittest.skipUnless(shutil.which("dash"), "needs dash")
    def test_programs_are_posix_sh_without_single_quotes(self):
        for path in PROGRAMS:
            self.assertNotIn("'", path.read_text())
            subprocess.run(["dash", "-n", str(path)], check=True)


if __name__ == "__main__":
    unittest.main()
