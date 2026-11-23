"""shk inside a Marlowe batch job, in process, against Marlowe-shaped Slurm 25.05.2 text.

The context is the one a job script gives shk on a Marlowe compute node: host n31, the job's
SLURM_JOB_ID and SLURM_CLUSTER_NAME=slurm, Marlowe's on-cluster predicate true
(fakes.marlowe_site), and a working directory with no configuration at or above it, as a run
directory on scratch has none. requeue --self runs the real control.sh behind the Marlowe
prelude against the argv-exact fake scontrol of test_control; allocation runs allocation.sh
against the fake scontrol of test_allocation for a real sleeping worker. Job and node records
have the shapes read on Marlowe; listpids and pidinfo follow the 25.05.2 source
(tests/fakes/marlowe.py). Marlowe's sh is bash, so the requeue cases also run under bash as
sh.
"""

import io
import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout

from fakes import SRC, marlowe_site
from fakes.expired_slurm import CannedSlurm
from fakes.marlowe import account, job_record, listpids, node, pidinfo, queue
from test_allocation import FAKE as ALLOCATION_FAKE
from test_control import ControlFlow
from test_flows import NEXT, NOW
from test_marlowe_submit import NAME

sys.path.insert(0, str(SRC))
from sherlock_kit import cli
from sherlock_kit.config import discover

TOKEN = "tok-1"
# The process of control.sh and its ancestors (the fake expands @PPID@): no task of the step.
PROGRAM = ("@PPID@", "batch", "-", "-")


def run(argv, ctx):
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = cli.main(list(argv), context=ctx)
    return code, json.loads(out.getvalue())


class InJob(ControlFlow):
    """A Marlowe compute node inside a job, in a run directory that holds no configuration."""

    def config(self, **extra):
        """Write none: nothing at or above the run directory configures shk."""

    def setUp(self):
        super().setUp()
        self.enterContext(marlowe_site(on_cluster=True))
        self.run = self.tmp / "run"
        self.run.mkdir()
        self.assertIsNone(discover(cwd=self.run), "a configuration at or above the run directory")

    def shk(self, *argv, site="marlowe", executor="auto", hostname="n31", env=None):
        """One command in process; env defaults to the job's, with this process's SLURM_JOB_ID."""
        if env is None:
            env = {
                "SLURM_JOB_ID": os.environ.get("SLURM_JOB_ID", ""),
                "SLURM_CLUSTER_NAME": "slurm",
            }
        ctx = cli.Context(
            executor=executor,
            site=site,
            env=env,
            hostname=hostname,
            clock=lambda: NOW,
            cwd=self.run,
        )
        return run(argv, ctx)

    def assertSiteAfterExecutor(self, report):
        keys = list(report)
        self.assertEqual(keys[keys.index("executor") + 1], "site", report)
        self.assertEqual((report["executor"], report["site"]), ("local", "marlowe"))


class MarloweSelfRequeue(InJob):
    """requeue --self as a job script calls it, through control.sh behind the Marlowe prelude."""

    def requeue(self, *extra, executor="auto"):
        return self.shk("requeue", "--self", "--expect-name", NAME, *extra, executor=executor)

    def test_a_running_job_on_preempt_is_requeued(self):
        self.allocation("44")
        self.scontrol({
            "listpids 44": {"out": listpids("44", PROGRAM)},
            "show job 44 --oneliner": {"out": job_record("44", NAME, token=TOKEN) + "\n"},
            "requeue 44": {},
        })
        for executor in ("auto", "local"):
            with self.subTest(executor=executor):
                self.calls_file.unlink(missing_ok=True)
                code, report = self.requeue(executor=executor)
                self.assertEqual(
                    (code, report.get("action"), report.get("job_id")),
                    (0, "requeued", "44"),
                    report,
                )
                self.assertIsNone(report["submission_token"])
                self.assertSiteAfterExecutor(report)
                self.assertEqual(
                    self.calls(),
                    [["listpids", "44"], ["show", "job", "44", "--oneliner"], ["requeue", "44"]],
                )

    def test_an_array_element_is_requeued_as_its_task(self):
        self.allocation("51", "50", "0")
        record = job_record("51", NAME, array="50", task="0", token=TOKEN)
        self.scontrol({
            "listpids 51": {"out": listpids("51", PROGRAM)},
            "show job 51 --oneliner": {"out": record + "\n"},
            "requeue 50_0": {},
        })
        code, report = self.requeue("--expect-token", TOKEN)
        self.assertEqual(
            (code, report.get("action"), report.get("job_id")), (0, "requeued", "50_0"), report
        )
        self.assertSiteAfterExecutor(report)
        self.assertEqual(
            self.calls(),
            [["listpids", "51"], ["show", "job", "51", "--oneliner"], ["requeue", "50_0"]],
        )

    def test_a_partition_outside_the_marlowe_table_is_refused_before_dispatch(self):
        self.allocation("44")
        for partition in ("batch", "hero"):  # Marlowe partitions the packaged table omits
            with self.subTest(partition=partition):
                self.calls_file.unlink(missing_ok=True)
                self.scontrol({
                    "listpids 44": {"out": listpids("44", PROGRAM)},
                    "show job 44 --oneliner": {
                        "out": job_record("44", NAME, token=TOKEN, Partition=partition) + "\n"
                    },
                    "requeue 44": {},
                })
                code, report = self.requeue()
                self.assertEqual((code, report["message"]), (3, "control: SHK_REFUSED partition"))
                self.assertEqual(
                    self.calls(), [["listpids", "44"], ["show", "job", "44", "--oneliner"]]
                )

    def test_a_failed_requeue_is_unknown_and_its_next_replays_without_configuration(self):
        self.allocation("44")
        self.scontrol({
            "listpids 44": {"out": listpids("44", PROGRAM)},
            "show job 44 --oneliner": {"out": job_record("44", NAME, token=TOKEN) + "\n"},
            "requeue 44": {
                "err": "Requested operation is presently disabled for job 44\n",
                "rc": 1,
            },
        })
        code, report = self.requeue()
        self.assertEqual((code, report["action"]), (5, "unknown"), report)
        self.assertIn("scontrol requeue exit 1", report["detail"])
        self.assertIn("Requested operation is presently disabled for job 44", report["detail"])
        options = ["--site", "marlowe", "--executor", "local", "status"]
        command = shlex.join(["shk", "--project-dir", str(self.run), *options])
        self.assertEqual(report["next"], f"{command} --job-id 44 --details (not before {NEXT})")
        # The follow-up, as a login-node session runs it later: its own global options only.
        argv = shlex.split(report["next"].partition(" (not before ")[0])[1:]
        canned = CannedSlurm(
            self.tmp,
            queue=[queue("44", "44", "RUNNING", NAME, token=TOKEN)],
            accounting=[account("44", "44", "RUNNING", NAME, token=TOKEN)],
            jobs=[job_record("44", NAME, token=TOKEN)],
        )
        self.knobs(**canned.env())
        args = cli.build_parser().parse_args(argv)
        ctx = cli.Context(
            project_dir=args.project_dir,
            executor=args.executor,
            site=args.site,
            env={},
            hostname="login-01",
            clock=lambda: NOW,
        )
        code, report = run(argv, ctx)
        self.assertEqual(code, 0, report)
        self.assertSiteAfterExecutor(report)
        entry = report["names"][0]
        self.assertEqual((entry["state"], entry["current"]["anomalies"]), ("live", []))
        self.assertTrue(report["details"]["complete"], report["details"])


class MarloweSelfRequeueBashAsSh(MarloweSelfRequeue):
    """Marlowe's sh is bash (docs/validation/observed-marlowe.md): the same cases under it."""

    def setUp(self):
        super().setUp()
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("needs bash")
        shim = self.tmp / "bash-as-sh"
        shim.mkdir()
        (shim / "sh").symlink_to(bash)
        self.knobs(PATH=f"{shim}{os.pathsep}{os.environ['PATH']}")


class MarloweAllocation(InJob):
    """allocation --pid --job-id for a worker the batch shell started, on node n31."""

    def setUp(self):
        super().setUp()
        self.allocation("44")
        self.worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(self.stop_worker)
        tool = self.tools / "scontrol"
        tool.write_text(f"#!{sys.executable}\n{ALLOCATION_FAKE}")
        tool.chmod(0o755)
        self.knobs(FAKE_ALLOCATION=str(self.tmp / "facts.json"), FAKE_CALLS=str(self.calls_file))

    def stop_worker(self):
        self.worker.terminate()
        self.worker.wait(timeout=5)

    def prove(self, record, node_line, executor="auto"):
        facts = {
            "job": {"44": record},
            "node": {"n31": node_line},
            "pidinfo": {str(self.worker.pid): pidinfo("44")},
            "hosts": {"n31": ["n31"]},
        }
        (self.tmp / "facts.json").write_text(json.dumps(facts))
        self.calls_file.unlink(missing_ok=True)
        pid = str(self.worker.pid)
        return self.shk("allocation", "--pid", pid, "--job-id", "44", executor=executor)

    def test_a_gpu_job_on_an_untyped_node_names_no_gpu_model(self):
        record = job_record("44", NAME, cpus=8, gpus=1)
        line = node("n31", "MIXED", cpus=8, memory_mb=65536, gpus=1)
        for executor in ("auto", "local"):
            with self.subTest(executor=executor):
                code, report = self.prove(record, line, executor)
                self.assertEqual(code, 0, report)
                self.assertSiteAfterExecutor(report)
                job = report["job"]
                self.assertEqual(
                    (job["partition"], job["nodes"], job["allocated_cpus"], job["allocated_gpus"]),
                    ("preempt", ["n31"], 8, 1),
                )
                self.assertEqual(
                    (report["process"]["hostname"], report["process"]["job_membership_verified"]),
                    ("n31", True),
                )
                self.assertEqual(
                    report["gpu_sku_candidates"]["nodes"],
                    [
                        {
                            "node": "n31",
                            "candidates": [],
                            "ambiguous": False,
                            "source": {
                                "ActiveFeatures": "location=local",
                                "Gres": "gpu:8(S:0-1)",
                                "CfgTRES": "cpu=112,mem=1950000M,billing=112,gres/gpu=8",
                            },
                        }
                    ],
                )
                self.assertEqual(
                    self.calls(),
                    [
                        ["show", "job", "44", "--oneliner"],
                        ["show", "hostnames", "n31"],
                        ["show", "node", "n31", "--oneliner"],
                        ["pidinfo", str(self.worker.pid)],
                    ],
                )

    def test_a_cpu_only_job_has_no_gpu(self):
        code, report = self.prove(job_record("44", NAME), node("n31", cpus=1, memory_mb=1024))
        self.assertEqual(code, 0, report)
        self.assertSiteAfterExecutor(report)
        job = report["job"]
        self.assertEqual(
            (job["allocated_cpus"], job["allocated_gpus"], job["allocated_gpu_tres"]), (1, 0, 0)
        )
