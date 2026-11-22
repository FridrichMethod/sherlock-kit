"""Reads and controls on Marlowe with Marlowe-shaped Slurm 25.05.2 text, and one whole session.

Plain status runs query.sh against the fake Slurm with jobs on `preempt`; detailed status runs
details.sh against canned rows and `scontrol show job` records of the 25.05.2 shape
(tests/fakes/marlowe.py); resources reads 25.05.2 node lines with untyped GRES; release and
update run control.sh and update.sh against an argv-exact fake scontrol. The session runs every
step of a Marlowe workflow through the fake ssh, as from a workstation.
"""

import json
import shutil
import subprocess
import sys
import unittest

import test_resources
from fakes import MARLOWE_ACCOUNT, SRC, install_fake_ssh
from fakes.marlowe import account, job_record, node, queue, tres
from test_details import DetailsFlow
from test_flows import Flow
from test_marlowe_submit import NAME, OnMarlowe
from test_update import UpdateFlow, lines, show

sys.path.insert(0, str(SRC))
from sherlock_kit import allocation, cli, details, partitions, sites, slurm

GPU_TRES = tres(8, 1)  # cpu=8,mem=64G,node=1,billing=8,gres/gpu=1
TOKEN = "tok-1"
SHERLOCK_WORDS = ("sherlock-plain", "sherlock-dtn", "/etc/agents", "sh_dev", "Sherlock")


class MarloweStatus(OnMarlowe, Flow):
    """Plain status of jobs on preempt: the Marlowe table knows the partition and its requeue."""

    def status(self):
        code, report = self.shk("status", "--task", "train-a")
        self.assertEqual((code, report["site"]), (0, "marlowe"), report)
        entry = report["names"][0]
        self.assertEqual(entry["current"]["anomalies"], [])
        return entry

    def test_pending_and_running(self):
        job = self.slurm.add(NAME, "PENDING", partition="preempt", tres=GPU_TRES)
        entry = self.status()
        self.assertEqual((entry["state"], entry["current"]["tasks"]["pending"]), ("live", 1))
        self.assertEqual(entry["current"]["partition"], "preempt")
        self.slurm.set(job, "RUNNING")
        entry = self.status()
        current = entry["current"]
        self.assertEqual((entry["state"], current["tasks"]["running"]), ("live", 1))
        self.assertEqual(current["cost"], {"cpu_seconds": 60, "gpu_seconds": 60, "final": False})

    def test_preempted_and_requeued_is_live_without_an_anomaly(self):
        job = self.slurm.add(NAME, "RUNNING", partition="preempt", tres=GPU_TRES)
        self.slurm.requeue(job)
        entry = self.status()
        current = entry["current"]
        self.assertEqual(entry["state"], "live")
        self.assertEqual(
            (current["max_restart"], current["tasks"]["by_state"]), (1, {"PENDING": 1})
        )
        self.assertEqual(current["cost"]["gpu_seconds"], 30)

    def test_occupancy_of_preempt(self):
        self.slurm.add(NAME, "RUNNING", partition="preempt", tres=GPU_TRES)
        self.slurm.add("other", "PENDING", user="someone", partition="preempt")
        code, report = self.shk("occupancy", "--partition", "preempt")
        self.assertEqual((code, report["site"]), (0, "marlowe"), report)
        found = report["partitions"]["preempt"]
        self.assertEqual(
            found["profile"]["courtesy"], partitions.profile("preempt", sites.MARLOWE).courtesy
        )
        self.assertEqual(found["totals"], {"running_jobs": 1, "running_gpus": 1, "pending_jobs": 1})

    def test_completed(self):
        self.slurm.add(NAME, "COMPLETED", partition="preempt", tres=tres(1))
        entry = self.status()
        self.assertEqual(entry["state"], "done")
        self.assertEqual(entry["current"]["exit_codes"], {"0:0": 1})
        self.assertTrue(entry["current"]["cost"]["final"])

    def test_the_since_bound_names_no_sherlock_database(self):
        for extra in ((), ("--details",)):
            with self.subTest(extra=extra):
                code, report = self.shk("status", "--task", "train-a", "--since", "40", *extra)
                self.assertEqual(
                    (code, report["error"], report["hint"]),
                    (1, "spec_invalid", "shk reads at most 31 days of accounting on every site"),
                )
        self.assertEqual(self.slurm.calls(), [])

    def test_the_anomalies_read_the_table_of_the_site(self):
        read = slurm.parse_read(
            f"SHK_Q|594100|PENDING|preempt|{NAME}\n"
            f"SHK_A|594100|PREEMPTED|preempt|30|8|{GPU_TRES}|2026-11-21T17:05:08|0|0:0|{NAME}\n"
            f"SHK_A|594100|PENDING|preempt|0|0||2026-11-21T17:05:08|1|0:0|{NAME}\n"
            "SHK_RC squeue 0\nSHK_RC sacct 0\nSHK_END\n",
            ("squeue", "sacct"),
        )
        rows = slurm.parse_rows(read.queue, read.acct, lambda name: name == NAME).by_name[NAME]
        for site, found in ((sites.MARLOWE, []), (sites.SHERLOCK, ["unknown_partition"])):
            with self.subTest(site=site.name):
                resolved = slurm.resolve_name(rows, site=site)
                self.assertEqual(resolved["current"]["anomalies"], found)


class MarloweDetails(OnMarlowe, DetailsFlow):
    """status --details and --job-id with 25.05.2 rows, records and Cluster slurm."""

    def test_a_running_gpu_job_keeps_its_marlowe_evidence(self):
        self.canned(
            queue=[queue("594100", "594100", "RUNNING", NAME, cpus=8, token=TOKEN)],
            accounting=[account("594100", "594100", "RUNNING", NAME, cpus=8, gpus=1, token=TOKEN)],
            jobs=[job_record("594100", NAME, token=TOKEN, cpus=8, gpus=1)],
        )
        code, report = self.details("--task", "train-a")
        self.assertEqual((code, report["site"]), (0, "marlowe"), report)
        entry = report["names"][0]
        self.assertEqual((entry["state"], entry["current"]["anomalies"]), ("live", []))
        self.assertEqual(entry["current"]["cost"]["gpu_seconds"], 7)
        details = report["details"]
        self.assertTrue(details["complete"], details)
        fields = details["jobs"][0]["fields"]
        self.assertEqual(
            (fields["Account"], fields["QOS"], fields["Partition"], fields["TresPerJob"]),
            (MARLOWE_ACCOUNT, "normal", "preempt", "gres/gpu:1"),
        )
        self.assertEqual(details["jobs"][0]["comment"], TOKEN)
        self.assertEqual(
            (details["accounting"][0]["cluster"], details["accounting"][0]["comment"]),
            ("slurm", TOKEN),
        )
        self.assertEqual(details["accounting"][0]["allocated_tres"], GPU_TRES)

    def test_a_completed_job_by_id(self):
        self.canned(accounting=[account("594200", "594200", "COMPLETED", NAME)])
        code, report = self.details("--job-id", "594200")
        self.assertEqual((code, report["site"]), (0, "marlowe"), report)
        current = report["names"][0]["current"]
        self.assertEqual(report["names"][0]["state"], "done")
        self.assertEqual(current["cost"], {"cpu_seconds": 7, "gpu_seconds": 0, "final": True})
        self.assertEqual((current["anomalies"], current["evidence_complete"]), ([], True))
        self.assertEqual(report["details"]["accounting"][0]["nodes"], "n31")

    def test_a_preempted_and_requeued_job(self):
        self.canned(
            queue=[queue("594100", "594100", "PENDING", NAME, cpus=8, token=TOKEN)],
            accounting=[
                account("594100", "594100", "PREEMPTED", NAME, cpus=8, gpus=1, elapsed=700),
                account("594100", "594100", "PENDING", NAME, cpus=8, gpus=1, restarts=1),
            ],
            jobs=[
                job_record(
                    "594100", NAME, state="PENDING", token=TOKEN, cpus=8, gpus=1, Restarts="1"
                )
            ],
        )
        code, report = self.details("--task", "train-a")
        self.assertEqual(code, 0, report)
        current = report["names"][0]["current"]
        self.assertEqual(report["names"][0]["state"], "live")
        self.assertEqual((current["max_restart"], current["anomalies"]), (1, []))
        self.assertEqual(current["cost"]["gpu_seconds"], 700)
        self.assertEqual(report["details"]["jobs"][0]["fields"]["Restarts"], "1")
        self.assertTrue(report["details"]["complete"], report["details"])


class MarloweResources(OnMarlowe, Flow):
    """Untyped GRES and the one feature location=local: GPU counts come from CfgTRES alone."""

    scontrol = test_resources.ResourcesFlows.scontrol

    def setUp(self):
        super().setUp()
        self.scontrol(
            node("n01", "MIXED+PLANNED", cpus=97, memory_mb=776000, gpus=8),
            node("n02", "MIXED", cpus=8, memory_mb=65536, gpus=1),
            node("n03", "MIXED+DRAIN", cpus=8, memory_mb=65536, gpus=1),
            node("n04", "IDLE"),
        )

    def test_node_records(self):
        code, report = self.shk("resources", "--partition", "preempt")
        self.assertEqual((code, report["site"], report["complete"]), (0, "marlowe", True), report)
        first = report["nodes"][0]
        self.assertEqual(
            (first["name"], first["features"], first["partitions"]),
            ("n01", ["location=local"], ["hero", "batch", "preempt"]),
        )
        self.assertEqual(
            (first["configured_cpus"], first["free_cpus"], first["free_memory_mb"]),
            (112, 15, 1174000),
        )
        self.assertEqual((first["configured_gpus"], first["free_gpus"]), (8, 0))
        self.assertEqual(
            [found["schedulable"] for found in report["nodes"]], [True, True, False, True]
        )

    def test_summary_by_sku_is_one_entry_without_a_model(self):
        code, report = self.shk(
            "resources",
            "--partition",
            "preempt",
            "--summary",
            "--by",
            "sku",
            "--by",
            "class",
            "--shape",
            "gpu1:cpus=8,mem=64G,gpus=1",
        )
        self.assertEqual((code, report["site"], report["complete"]), (0, "marlowe", True), report)
        summary = report["summary"]
        group = {
            "nodes": 4,
            "schedulable_nodes": 3,
            "countable_nodes": 3,
            "unschedulable_nodes": 1,
            "unknown_nodes": 0,
            "cpus": {"configured": 448, "allocated": 113, "free": 231},
            "memory_mb": {"configured": 7800000, "allocated": 907072, "free": 5008464},
            "gpus": {"configured": 32, "allocated": 10, "free": 15},
            "slots": {"gpu1": 15},
        }
        expected = {**group, "by_sku": [{"sku": [], **group}], "by_class": [{"class": [], **group}]}
        self.assertEqual(summary["union"], expected)
        self.assertEqual(summary["partitions"], {"preempt": expected})
        self.assertEqual(summary["overlapping_nodes"], 0)

    def test_an_untyped_node_names_no_gpu_model_candidate(self):
        record = {"node": "n31", "fields": details.fields(node("n31"))}
        found = allocation.sku_candidates(record)
        self.assertEqual((found["candidates"], found["ambiguous"]), ([], False))
        self.assertEqual(allocation.gpu_types(record["fields"]), [])


class MarloweControl(OnMarlowe, UpdateFlow):
    """Release and update against 25.05.2 records of jobs on preempt."""

    def held(self, **changes):
        return job_record("44", NAME, state="PENDING", held=True, token=TOKEN, **changes)

    def test_release_of_a_held_job(self):
        self.scontrol({show(): {"out": lines(self.held())}, "release 44": {}})
        code, report = self.shk(
            "release", "--job-id", "44", "--expect-name", NAME, "--expect-token", TOKEN
        )
        self.assertEqual((code, report["action"], report["site"]), (0, "released", "marlowe"))
        self.assertEqual(self.calls(), [["show", "job", "44", "--oneliner"], ["release", "44"]])

    def test_time_update_of_a_held_job(self):
        self.scontrol({
            show(): [{"out": lines(self.held())}, {"out": lines(self.held(TimeLimit="00:05:00"))}],
            "update JobId=44 TimeLimit=5": {},
        })
        code, report = self.update("--time", "5m", "--expect-token", TOKEN, name=NAME)
        self.assertEqual((code, report["action"], report["site"]), (0, "updated", "marlowe"))
        self.assertEqual(
            [
                (found["TimeLimit"], found["TimeMin"])
                for found in report["before"] + report["after"]
            ],
            [("00:10:00", "N/A"), ("00:05:00", "N/A")],
        )

    def test_partition_update_reads_the_marlowe_class(self):
        partition = (
            "PartitionName=preempt AllowGroups=ALL AllowAccounts=ALL DenyQos=class,medium "
            "Default=NO QoS=normal DefaultTime=NONE GraceTime=900 MaxNodes=UNLIMITED "
            "MaxTime=12:00:00 MinNodes=0 State=UP TotalCPUs=3472 TotalNodes=31\n"
        )
        self.scontrol({
            show(): {"out": lines(self.held(gpus=1, cpus=8))},
            "show partition preempt --oneliner": {"out": partition},
        })
        code, report = self.update("--partition", "preempt", name=NAME)
        self.assertEqual((code, report["action"], report["site"]), (0, "unchanged", "marlowe"))
        self.assertEqual(self.dispatches(), [])


class MarloweSession(OnMarlowe, Flow):
    """One Marlowe workflow from a workstation, every step over the fake ssh to marlowe-plain."""

    executor = "ssh"
    modules = cli.MODULES

    def setUp(self):
        super().setUp()
        self.fake = install_fake_ssh(self.tmp)
        self.knobs(**self.fake.env(FAKE_SSH_BANNER="none"))  # in front of the fake Slurm
        # Fail closed: every dial must reach the fake, never a real master.
        self.assertEqual(shutil.which("ssh"), str(self.fake.path))
        self.local = self.tmp / "local"
        (self.local / "d").mkdir(parents=True)
        (self.local / "d" / "x").write_text("x\n")
        (self.tmp / "results").mkdir()

    def output(self):
        """A run directory with files and the SHA256SUMS that pull verifies."""
        out = self.remote / "out"
        out.mkdir(parents=True)
        (out / "a.txt").write_text("a\n")
        subprocess.run(
            ["sh", "-c", 'cd "$1" && sha256sum a.txt > SHA256SUMS', "sh", out], check=True
        )
        return out

    def test_no_reply_names_sherlock_and_every_executor_reply_names_marlowe(self):
        steps = [
            ("doctor", "--remote"),
            ("submit", "--dry-run"),
            ("submit", "--preflight"),
            ("submit",),
            ("status", "--task", "train-a"),
            ("cancel", "--task", "train-a"),
            ("push", str(self.local), str(self.remote / "inputs")),
        ]
        replies = []
        for step in steps:
            if step[0] == "submit":
                code, reply = self.submit(*step[1:])
            else:
                code, reply = self.shk(*step)
            self.assertEqual(code, 0, (step, reply))
            replies.append((step, reply))
        code, reply = self.shk("pull", str(self.output()), str(self.tmp / "results" / "out"))
        self.assertEqual((code, reply["action"]), (0, "pulled"), reply)
        replies.append((("pull",), reply))
        actions = [reply.get("action") for _, reply in replies]
        self.assertEqual(
            actions,
            [None, "dry_run", "preflight", "submitted", None, "cancelled", "pushed", "pulled"],
        )
        for step, reply in replies:
            with self.subTest(step=step[:2]):
                text = json.dumps(reply)
                self.assertFalse([word for word in SHERLOCK_WORDS if word in text], text)
                if "executor" in reply:
                    keys = list(reply)
                    self.assertEqual(keys[keys.index("executor") + 1], "site")
                    self.assertEqual((reply["executor"], reply["site"]), ("ssh", "marlowe"))
                else:  # offline or transfer replies print no executor, so no site either
                    self.assertIn(step[0], ("submit", "push", "pull"))
                    self.assertNotIn("site", reply)
        self.assertEqual(replies[0][1]["remote"]["site"], "marlowe")
        hosts = {
            call[-1] if "-O" in call else next(word for word in call if not word.startswith("-"))
            for call in self.fake.calls()
        }
        self.assertEqual(hosts, {"marlowe-plain"})
        self.assertEqual((self.remote / "inputs" / "d" / "x").read_text(), "x\n")
        tested = [call for call in self.slurm.calls("sbatch") if call["argv"][-1] == "--test-only"]
        self.assertEqual(len(tested), 2)  # the preflight, then the test before the submit


if __name__ == "__main__":
    unittest.main()
