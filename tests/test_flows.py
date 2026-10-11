"""End-to-end flows: the real remote sh programs (/bin/sh, dash here) against the fake Slurm."""
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
import unittest
from unittest.mock import patch

from fakes import SRC, install_fake_ssh
from fakes.fakeslurm import FakeSlurm

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, executor, partitions, slurm, spec  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
NEXT = "2026-10-10T12:01:00Z"
SCRIPT = "#!/bin/bash -l\n#SBATCH --mail-type=END\necho work\n"
USER = pwd.getpwuid(os.getuid()).pw_name
PROGRAMS = [SRC / "sherlock_kit" / "data" / "remote" / f"{name}.sh" for name in ("query", "submit", "cancel")]


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
        lines = ['project = "demo"', f'remote_root = "{self.remote}"', *(f"{k} = {v}" for k, v in extra.items())]
        (self.tmp / "sherlock-kit.toml").write_text("\n".join(lines) + "\n")

    def knobs(self, **values):
        environment = patch.dict(os.environ, values)
        environment.start()
        self.addCleanup(environment.stop)

    def shk(self, *argv):
        ctx = cli.Context(project_dir=self.tmp, executor=self.executor, env={}, hostname="lab-ubuntu", clock=lambda: NOW)
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(list(argv), modules=[slurm], context=ctx)
        return code, json.loads(out.getvalue())

    def submit(self, *extra, task="train-a", partition="normal", script="job.sh"):
        return self.shk("submit", "--task", task, "--script", str(self.tmp / script), "--partition", partition,
                        "--cpus", "1", "--mem", "2G", "--time", "20m", *extra)

    def entry(self, task="train-a", code=0):
        found, report = self.shk("status", "--task", task)
        self.assertEqual(found, code)
        return report["names"][0]

    def shk_dir(self, task="train-a"):
        return self.remote / "runs" / spec.job_name("demo", "default", task) / ".shk"


class SubmitFlows(Flow):
    def test_submit_stages_files_and_refuses_live_then_done(self):
        self.knobs(SBATCH_ACCOUNT="leaky", SLURM_MEM_PER_NODE="999", SLURM_CONF="/etc/slurm/slurm.conf")
        code, out = self.submit()
        self.assertEqual((code, out["action"], out["next_check_after"], out["executor"]), (0, "submitted", NEXT, "local"))
        name, job = out["name"], out["job_id"]
        self.assertEqual(sorted(p.name for p in self.shk_dir().iterdir()), [f"{name}.{job}.json", f"{name}.{job}.sh"])
        self.assertEqual((self.shk_dir() / f"{name}.{job}.sh").read_text(), SCRIPT)
        self.assertEqual(json.loads((self.shk_dir() / f"{name}.{job}.json").read_text())["argv"], out["argv"])
        sbatch = self.slurm.calls("sbatch")[0]
        self.assertEqual(sbatch["argv"][:-1], out["argv"][1:])
        self.assertRegex(sbatch["argv"][-1], rf"/\.shk/{name}\.20261010T120000Z-[0-9a-f]{{4}}\.sh\Z")
        conf = {"SLURM_CONF": "/etc/slurm/slurm.conf"}
        self.assertEqual([c["env"] for c in self.slurm.calls()], [conf, {**conf, "SLURM_TIME_FORMAT": "standard"}, conf, conf])
        self.assertEqual(self.slurm.state()["jobs"][0]["script"], SCRIPT)
        for extra in ((), ("--resubmit", "again")):
            self.assertEqual(self.submit(*extra)[:2], (3, {"ok": False, "error": "live", "message": f"{name} is live "
                             f"(newest job {job})", "hint": "wait; check shk status at most once per 60 s"}))
        self.slurm.set(int(job), "COMPLETED")
        self.assertEqual(self.submit()[1]["error"], "done")
        code, again = self.submit("--resubmit", "rerun with more data")
        self.assertEqual((code, again["state_before"]), (0, "done"))
        frozen = json.loads((self.shk_dir() / f"{name}.{again['job_id']}.json").read_text())
        self.assertEqual(frozen["resubmit_reason"], "rerun with more data")
        entry = self.entry()
        self.assertEqual((entry["state"], entry["current"]["job_id"], entry["spec_files"]), ("live", again["job_id"], 2))
        self.assertEqual((entry["history"][0]["job_id"], entry["history"][0]["state"]), (job, "done"))

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
        args = ["shk.demo.t.0123456789", str(run), "20261010T120000Z-abcd", "0" * 64, "{}", "--", "sbatch", "--parsable"]
        local = executor.LocalExecutor()
        self.assertEqual(executor.run_program(local, "submit", args, stdin=b"x").stdout, f"SHK_LIVE {live}\n")
        args[0] = "shk.demo.u.0123456789"
        self.assertEqual(executor.run_program(local, "submit", args, stdin=b"#!/bin/sh\n").stdout, "SHK_REFUSED digest\n")
        self.assertEqual(list((run / ".shk").iterdir()), [])
        self.assertEqual(executor.run_program(local, "submit", args[:5] + ["sbatch"]).stdout, "SHK_REFUSED usage\n")
        self.assertEqual(self.slurm.calls("sbatch"), [])

    def test_offline_refusals_and_dry_run_make_no_call(self):
        code, out = self.submit("--dry-run", "--array", "4%2")
        self.assertEqual((code, out["action"], out["spec"]["argv"], out["argv"][4]), (0, "dry_run", out["argv"], "--array=0-3%2"))
        self.assertEqual(out["spec"]["script"]["sha256"], out["script_sha256"])
        (self.tmp / "bad.sh").write_text("#!/bin/sh\n#SBATCH --partition=gpu\necho\n")
        self.assertEqual(self.submit(script="bad.sh")[1]["error"], "sbatch_directive")
        self.assertEqual(self.submit(partition="btrippe")[1]["error"], "courtesy_ack_required")
        self.assertEqual(self.submit("--requeue")[1]["error"], "requeue_not_allowed")
        self.assertEqual((self.slurm.calls(), self.remote.exists()), ([], False))

    def test_borrowed_submit_reports_occupancy_courtesy_and_budget(self):
        self.slurm.add("theirs", "RUNNING", user="alice", partition="btrippe", tres="cpu=4,gres/gpu=2")
        for index in range(2):
            self.slurm.add(f"mine-{index}", "PENDING", partition="btrippe")
        code, out = self.submit("--courtesy-ack", partition="btrippe")
        self.assertEqual(code, 0)
        self.assertEqual(out["occupancy"]["users"]["alice"], {"running_jobs": 1, "running_gpus": 2, "pending_jobs": 0})
        self.assertEqual((out["courtesy"], len(out["warnings"])), (partitions.profile("btrippe").courtesy, 1))


class StatusCancelOccupancyFlows(Flow):
    def test_array_status_counts_throttle_held_tasks_as_pending(self):
        out = self.submit("--array", "4%2")[1]
        job = int(out["job_id"])
        self.slurm.set(job, "COMPLETED", "RUNNING")
        current = self.entry()["current"]
        self.assertEqual([current["tasks"][k] for k in ("completed", "running", "pending", "missing")], [1, 1, 2, 0])
        self.assertEqual(current["array"], {"count": 4, "count_known": True})
        self.slurm.set(job, None, "COMPLETED", "COMPLETED", "COMPLETED")
        entry = self.entry()
        self.assertEqual((entry["state"], entry["current"]["cost"]), ("done", {"cpu_seconds": 240, "gpu_seconds": 0, "final": True}))
        self.slurm.add("shk.demo-x.t.0123456789", "RUNNING")
        self.slurm.add("shk.demo.theirs.0123456789", "RUNNING", user="alice")
        code, report = self.shk("status", "--all")
        self.assertEqual((code, [e["name"] for e in report["names"]], report["truncated"]), (0, [out["name"]], False))
        self.assertEqual(self.shk("status", "--name", out["name"], "--since", "0")[1]["error"], "spec_invalid")

    def test_stale_spec_files_submit_like_new_and_anomalies_exit_6(self):
        self.shk_dir("old").mkdir(parents=True)
        name = spec.job_name("demo", "default", "old")
        (self.shk_dir("old") / f"{name}.77.json").write_text('{"resources":{"array":null}}\n')
        self.assertEqual((self.entry("old")["state"], self.entry("old")["spec_files"]), ("stale", 1))
        self.assertEqual(self.submit(task="old")[1]["state_before"], "stale")
        self.slurm.requeue(int(self.submit(task="pre")[1]["job_id"]))
        self.assertEqual(self.entry("pre", code=6)["current"]["anomalies"], ["unexpected_preemption"])

    def test_cancel_is_scoped_to_one_name(self):
        out, other = self.submit()[1], self.submit(task="keep")[1]
        code, cancelled = self.shk("cancel", "--task", "train-a")
        self.assertEqual((code, cancelled["action"], cancelled["jobs"]), (0, "cancelled", [out["job_id"]]))
        self.assertEqual(self.slurm.calls("scancel")[0]["argv"], ["-u", USER, f"--name={out['name']}"])
        states = {job["name"]: job["tasks"][0][-1]["state"] for job in self.slurm.state()["jobs"]}
        self.assertEqual(states, {out["name"]: "CANCELLED", other["name"]: "PENDING"})
        self.assertEqual(self.shk("cancel", "--name", out["name"])[1]["action"], "nothing_live")
        self.knobs(FAKE_SCANCEL_RC="1")
        self.assertEqual(self.shk("cancel", "--task", "keep")[1]["error"], "slurm_rejected")
        self.assertEqual(self.shk("cancel", "--name", "not-a-shk-name")[0], 1)

    def test_occupancy_works_without_a_config(self):
        (self.tmp / "sherlock-kit.toml").unlink()
        self.slurm.add("x", "RUNNING", user="alice", partition="owners", tres="cpu=1,gres/gpu:h100=1")
        self.slurm.add("y", "PENDING", user="bob", partition="owners")
        code, out = self.shk("occupancy", "--partition", "owners", "--partition", "normal")
        self.assertEqual((code, out["next_check_after"]), (0, NEXT))
        self.assertEqual(out["partitions"]["owners"]["totals"], {"running_jobs": 1, "running_gpus": 1, "pending_jobs": 1})
        self.assertEqual((out["partitions"]["normal"]["users"], out["partitions"]["owners"]["profile"]["preemptible"]), ({}, True))
        self.assertEqual(self.shk("occupancy", "--partition", "mystery")[1]["error"], "unknown_partition")


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


class RemoteProgramTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("shellcheck") and shutil.which("dash"), "needs shellcheck and dash")
    def test_programs_are_posix_sh_without_single_quotes(self):
        for path in PROGRAMS:
            self.assertNotIn("'", path.read_text())
            subprocess.run(["dash", "-n", str(path)], check=True)
        subprocess.run(["shellcheck", "-s", "sh", *map(str, PROGRAMS)], check=True)


if __name__ == "__main__":
    unittest.main()
