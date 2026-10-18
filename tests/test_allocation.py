"""Public local allocation CLI against real processes and an argv-checking fake scontrol.

Records follow the Slurm 25.11.8 `--oneliner` shapes observed on Sherlock (synthetic ids,
users and hosts): jobs carry AllocTRES and no Gres; steps print NodeList, Nodes (a count),
CPUs, Tasks and TRES; an array element's step is StepId=<ArrayJobId>_<ArrayTaskId>.<step>;
the meta physical id (JobId == ArrayJobId) also returns every sibling record; listpids takes
and prints only physical ids; pidinfo prints the physical job id and its remaining time.
"""

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import SRC

sys.path.insert(0, str(SRC))

FAKE = """
import json, os, sys, time
from pathlib import Path
d = json.loads(Path(os.environ["FAKE_ALLOCATION"]).read_text())
a = sys.argv[1:]
with open(os.environ["FAKE_CALLS"], "a") as f:
    f.write(json.dumps(a) + "\\n")
shapes = {
    ("show", "job"): 4, ("show", "step"): 4, ("show", "node"): 4, ("show", "hostnames"): 3,
}
if (len(a) == 2 and a[0] in ("pidinfo", "listpids")):
    action, target = a
elif shapes.get(tuple(a[:2])) == len(a) and (len(a) == 3 or a[3] == "--oneliner"):
    action, target = ("hosts" if a[1] == "hostnames" else a[1]), a[2]
else:
    sys.stderr.write(f"fake scontrol: unexpected argv {a}\\n")
    sys.exit(97)
time.sleep(d.get("delay", {}).get(action, 0))
if action in d.get("error", {}):
    sys.stderr.write(d["error"][action])
    sys.exit(1)
reply = d[action].get(target)
if reply is None:
    sys.stderr.write("slurm_load_jobs error: Invalid job id specified\\n")
    sys.exit(1)
print("\\n".join(reply) if action == "hosts" else reply)
"""


class AllocationCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="shk-allocation-")
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(self.stop_worker)
        self.host = socket.gethostname().split(".")[0]
        self.uid = os.getuid()
        self.data = {
            "job": {"44": self.job_record("44")},
            "step": {"44.0": self.step_record("44.0")},
            "node": {self.host: self.node_record()},
            "pidinfo": {str(self.worker.pid): self.pidinfo("44")},
            "listpids": {"44.0": self.listpids("44")},
            "hosts": {self.host: [self.host], "demo[1-2]": ["demo1", "demo2"]},
        }
        tool = self.tmp / "scontrol"
        tool.write_text(f"#!{sys.executable}\n{FAKE}")
        tool.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(SRC),
            "FAKE_ALLOCATION": str(self.tmp / "facts.json"),
            "FAKE_CALLS": str(self.tmp / "calls"),
        }

    def job_record(self, job, array=None, task=None, **changes):
        fields = {
            "JobId": job,
            "ArrayJobId": array,
            "ArrayTaskId": task,
            "JobName": "demo",
            "UserId": f"demo({self.uid})",
            "GroupId": "lab(1000)",
            "MCS_label": "N/A",
            "Priority": "1000",
            "QOS": "normal",
            "JobState": "RUNNING",
            "Reason": "None",
            "Dependency": "(null)",
            "Requeue": "0",
            "Restarts": "0",
            "BatchFlag": "1",
            "ExitCode": "0:0",
            "RunTime": "00:00:04",
            "TimeLimit": "00:25:00",
            "SubmitTime": "2026-10-10T05:00:00",
            "StartTime": "2026-10-10T05:00:01",
            "EndTime": "2026-10-10T05:25:01",
            "Partition": "normal",
            "AllocNode:Sid": "login1:1234",
            "ReqNodeList": "(null)",
            "NodeList": self.host,
            "BatchHost": self.host,
            "StepMgrEnabled": "Yes",
            "NumNodes": "1",
            "NumCPUs": "4",
            "NumTasks": "1",
            "CPUs/Task": "4",
            "ReqB:S:C:T": "0:0:*:*",
            "ReqTRES": "cpu=4,mem=2G,node=1,billing=4,gres/gpu=2",
            "AllocTRES": "cpu=4,mem=2G,node=1,billing=4,gres/gpu=2",
            "Socks/Node": "*",
            "MinMemoryNode": "2G",
            "Features": "(null)",
            "Licenses": "fs_scratch",
            "LicensesAlloc": "fs_scratch:1",
            "Network": "(null)",
            "Command": "/home/demo/job.sh",
            "SubmitLine": "sbatch --parsable --job-name=demo --export=ALL,SHK_NAME=demo job.sh",
            "WorkDir": "/home/demo",
            "Comment": "tok-1",
            "StdOut": "/home/demo/slurm.out",
            "TresPerJob": "gres/gpu:2",
            "TresPerTask": "cpu=4",
            **changes,
        }
        return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)

    def step_record(self, step, **changes):
        fields = {
            "StepId": step,
            "UserId": str(self.uid),
            "StartTime": "2026-10-10T05:00:09",
            "TimeLimit": "UNLIMITED",
            "State": "RUNNING",
            "Partition": "normal",
            "NodeList": self.host,
            "Nodes": "1",
            "CPUs": "2",
            "Tasks": "2",
            "Name": "bash",
            "Network": "(null)",
            "TRES": "cpu=2,mem=1G,node=1,gres/gpu=1",
            "ResvPorts": "(null)",
            "CPUFreqReq": "Default",
            "Dist": "Cyclic",
            "SrunHost:Pid": f"{self.host}:4242",
            "TresPerTask": "cpu=1",
            **changes,
        }
        return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)

    def node_record(self, **changes):
        fields = {
            "NodeName": self.host,
            "Arch": "x86_64",
            "CoresPerSocket": "10 ",
            "CPUAlloc": "20",
            "CPUEfctv": "20",
            "CPUTot": "20",
            "Gres": "(null)",
            "NodeAddr": self.host,
            "Version": "25.11.8",
            "OS": "Linux 3.10.0-1160.el7.x86_64 #1 SMP Thu Sep 10 04:02:55 UTC 2026 ",
            "RealMemory": "128000",
            "Sockets": "2",
            "Boards": "1",
            "State": "ALLOCATED",
            "ThreadsPerCore": "1",
            "Partitions": "normal ",
            "CfgTRES": "cpu=20,mem=125G,billing=51",
            "AllocTRES": "cpu=20,mem=81248M",
            **changes,
        }
        return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)

    @staticmethod
    def pidinfo(job):
        return f"Slurm JobId={job} ends at Sat Oct 10 05:25:01 2026\nJob remaining time is 1496 seconds"

    def listpids(self, job, step="0"):
        return (
            "PID      JOBID    STEPID   LOCALID GLOBALID\n"
            f"{self.worker.pid}     {job} {step}        0       0        \n"
            f"99999999     {job} {step}        -       -        "
        )

    def stop_worker(self):
        if self.worker.poll() is None:
            self.worker.terminate()
        self.worker.wait(timeout=5)

    def calls(self):
        path = self.tmp / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_cli(self, *extra, job="44", pid=None, mode="local"):
        (self.tmp / "facts.json").write_text(json.dumps(self.data))
        (self.tmp / "calls").unlink(missing_ok=True)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "sherlock_kit",
                "--executor",
                mode,
                "allocation",
                "--pid",
                str(self.worker.pid if pid is None else pid),
                "--job-id",
                job,
                *extra,
            ],
            env=self.env,
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=60,  # a cold start from a network filesystem can take seconds
            check=False,
        )
        self.assertNotEqual(result.returncode, 2, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def assertRefused(self, outcome, message, error="allocation_invalid", code=1):
        found, report = outcome
        self.assertEqual((found, report.get("error")), (code, error), report)
        self.assertIn(message, report["message"])

    def test_worker_pid_affinity_actual_resources_and_hashes(self):
        code, report = self.run_cli("--hostlist", "demo[1-2]")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["process"]["pid"], self.worker.pid)
        self.assertEqual(
            report["process"]["cpu_affinity"], sorted(os.sched_getaffinity(self.worker.pid))
        )
        self.assertTrue(report["process"]["job_membership_verified"])
        self.assertIsNone(report["process"]["step_membership_verified"])
        self.assertEqual((report["job"]["allocated_cpus"], report["job"]["allocated_gpus"]), (4, 2))
        self.assertEqual(report["job"]["fields"]["LicensesAlloc"], "fs_scratch:1")
        self.assertEqual(
            report["job"]["sha256"], hashlib.sha256(self.data["job"]["44"].encode()).hexdigest()
        )
        self.assertEqual(report["hostlists"][-1]["nodes"], ["demo1", "demo2"])
        self.assertTrue(report["complete"])
        self.assertEqual(
            self.calls()[:3],
            [
                ["show", "job", "44", "--oneliner"],
                ["show", "hostnames", self.host],
                ["show", "node", self.host, "--oneliner"],
            ],
        )

    def test_numeric_step_has_separate_resources_and_membership(self):
        code, report = self.run_cli("--step-id", "0")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["job"]["allocated_cpus"], 4)
        self.assertEqual(report["step"]["allocated_cpus"], 2)
        self.assertEqual((report["step"]["tasks"], report["step"]["num_nodes"]), (2, 1))
        self.assertEqual(report["step"]["query_id"], "44.0")
        self.assertTrue(report["process"]["step_membership_verified"])
        self.assertIn(str(self.worker.pid), report["membership_evidence"]["step"]["raw"])
        self.assertIn(["listpids", "44.0"], self.calls())

    def test_array_logical_identity_preserves_physical_id(self):
        self.data["job"] = {"44_2": self.job_record("45", array="44", task="2")}
        self.data["pidinfo"] = {str(self.worker.pid): self.pidinfo("45")}
        code, report = self.run_cli(job="44_2")
        self.assertEqual(code, 0, report)
        self.assertEqual((report["job"]["job_id"], report["job"]["array_task_id"]), ("45", "2"))
        self.assertRefused(self.run_cli(job="44_3"), "Invalid job id specified")
        self.data["job"]["44_3"] = self.data["job"]["44_2"]
        self.assertRefused(self.run_cli(job="44_3"), "exactly one matching scheduler record")

    def test_meta_array_id_selects_its_own_record_among_siblings(self):
        own = self.job_record("50", array="50", task="1")
        sibling = self.job_record("51", array="50", task="0")
        self.data["job"] = {"50": f"{own}\n{sibling}", "50_1": own}
        self.data["pidinfo"] = {str(self.worker.pid): self.pidinfo("50")}
        for selector in ("50", "50_1"):
            with self.subTest(selector=selector):
                code, report = self.run_cli(job=selector)
                self.assertEqual(code, 0, report)
                self.assertEqual(
                    (report["job"]["job_id"], report["job"]["array_task_id"]), ("50", "1")
                )
                self.assertEqual(report["job"]["raw"], own)
        self.data["job"]["50"] = f"{own}\n{own}"
        self.assertRefused(self.run_cli(job="50"), "exactly one matching scheduler record")
        self.data["job"]["50"] = f"{own}\n{sibling} JobState=PENDING"
        self.assertRefused(self.run_cli(job="50"), "duplicate scheduler field JobState")

    def test_array_element_steps_use_the_logical_step_id(self):
        # Slurm prints an element's step as A_T.S; N.S of the meta id lists every element's step.
        self.data["job"] = {
            "50": self.job_record("50", array="50", task="1"),
            "50_0": self.job_record("51", array="50", task="0"),
        }
        self.data["step"] = {
            "50.0": f"{self.step_record('50_0.0')}\n{self.step_record('50_1.0')}",
            "51.0": self.step_record("50_0.0"),
            "50_0.0": self.step_record("50_0.0"),
            "50_1.0": self.step_record("50_1.0"),
        }
        for selector, physical, step in (("50_0", "51", "50_0.0"), ("50", "50", "50_1.0")):
            with self.subTest(selector=selector):
                self.data["pidinfo"] = {str(self.worker.pid): self.pidinfo(physical)}
                self.data["listpids"] = {f"{physical}.0": self.listpids(physical)}
                code, report = self.run_cli("--step-id", "0", job=selector)
                self.assertEqual(code, 0, report)
                self.assertEqual(report["step"]["query_id"], step)
                self.assertIn(["show", "step", step, "--oneliner"], self.calls())
                self.assertIn(["listpids", f"{physical}.0"], self.calls())

    def test_array_keys_in_a_free_text_comment_never_redirect_the_step_proof(self):
        self.data["job"] = {
            "44": self.job_record("44", Comment="rerun of ArrayJobId=40 ArrayTaskId=7")
        }
        self.data["step"] = {"44.0": self.step_record("44.0"), "40_7.0": self.step_record("40_7.0")}
        code, report = self.run_cli("--step-id", "0")
        self.assertEqual(code, 0, report)
        self.assertEqual(
            (report["job"]["array_job_id"], report["step"]["query_id"]), (None, "44.0")
        )
        self.assertNotIn(["show", "step", "40_7.0", "--oneliner"], self.calls())

    def test_cpu_only_actual_resources_and_typed_or_unrelated_tres(self):
        self.data["job"]["44"] = self.job_record("44", AllocTRES="cpu=4,mem=2G,node=1,billing=4")
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(
            (report["job"]["allocated_gpus"], report["job"]["allocated_gpu_tres"]), (0, 0)
        )
        for tres, gpus in (
            ("cpu=4,mem=2G,node=1,gres/gpu:a=1,gres/gpu:b=1", 2),
            ("cpu=4,mem=2G,node=1,gres/gpu=2,gres/gpu:a=2", 2),
            ("cpu=4,mem=2G,node=1,license/fs_scratch@server=1,gres/gpu=1", 1),
        ):
            with self.subTest(tres=tres):
                self.data["job"]["44"] = self.job_record("44", AllocTRES=tres)
                code, report = self.run_cli()
                self.assertEqual((code, report["job"]["allocated_gpus"]), (0, gpus), report)

    def test_missing_counts_remain_null(self):
        self.data["job"]["44"] = self.job_record("44", NumCPUs=None, AllocTRES="mem=2G,node=1")
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertIsNone(report["job"]["allocated_cpus"])

    def test_job_identity_owner_state_duplicate_and_resources_fail_closed(self):
        for changes, message in [
            ({"JobId": "45"}, "exactly one matching scheduler record"),
            ({"UserId": f"other({self.uid + 1})"}, "allocation belongs to another UID"),
            ({"UserId": "garbage"}, "missing or invalid scheduler owner"),
            ({"UserId": "(null)"}, "missing or invalid scheduler owner"),
            ({"UserId": "demo(x)"}, "missing or invalid scheduler owner"),
            ({"JobState": "PENDING"}, "allocation is not live"),
            ({"NumCPUs": "-1"}, "invalid allocated CPUs"),
            ({"AllocTRES": "cpu=5,mem=2G,node=1,gres/gpu=2"}, "allocated CPU fields disagree"),
            ({"AllocTRES": "cpu=4,gres/gpu=bad"}, "invalid actual numeric TRES count"),
            ({"AllocTRES": "cpu=4,gres/gpu=1,gres/gpu:a=2"}, "typed GPU TRES exceed total"),
            ({"AllocTRES": "cpu=4,cpu=4"}, "malformed or duplicated allocated TRES"),
            ({"AllocTRES": "cpu=4,gres/gpu=Unknown"}, "invalid actual numeric TRES count"),
            ({"AllocTRES": "Unknown"}, "unknown actual allocation TRES"),
            ({"AllocTRES": None}, "no actual allocated TRES"),
            ({"NumNodes": "2"}, "allocated node count disagrees with hostlist"),
            ({"NodeList": "(null)"}, "missing allocated nodes"),
        ]:
            with self.subTest(changes=changes):
                self.data["job"]["44"] = self.job_record("44", **changes)
                self.assertRefused(self.run_cli(), message)
        self.data["job"]["44"] = self.job_record("44") + " JobId=44"
        self.assertRefused(self.run_cli(), "duplicate scheduler field JobId")

    def test_pid_host_and_step_fail_closed(self):
        step = self.step_record("44.0")
        cases = [
            ("pidinfo", self.pidinfo("45"), "worker PID is not verified in selected allocation"),
            ("pidinfo", "unrecognized pid evidence", "worker PID is not verified"),
            ("listpids", self.listpids("44").replace(f"{self.worker.pid} ", "1 "), "selected step"),
            ("listpids", self.listpids("45"), "worker PID is not verified in selected step"),
            ("listpids", self.listpids("44") + "\nmalformed", "malformed process membership rows"),
            ("step", step.replace("StepId=44.0", "StepId=44.1"), "exactly one matching"),
            ("step", step.replace("CPUs=2", "CPUs=bad"), "invalid allocated CPUs"),
            ("step", step.replace("UserId=", "UserId=1"), "allocation belongs to another UID"),
            (
                "step",
                step.replace("NodeList=", "NodeList=demo1,").replace("Nodes=1", "Nodes=2"),
                "step nodes are outside parent allocation",
            ),
            ("step", step.replace("CPUs=2", "CPUs=8").replace("cpu=2", "cpu=8"), "exceeds parent"),
            ("step", step.replace("gres/gpu=1", "gres/gpu=3"), "allocated_gpus exceeds parent"),
        ]
        self.data["hosts"][f"demo1,{self.host}"] = ["demo1", self.host]
        for field, value, message in cases:
            with self.subTest(field=field, value=value):
                key = next(iter(self.data[field]))
                old = self.data[field][key]
                self.data[field][key] = value
                self.assertRefused(self.run_cli("--step-id", "0"), message)
                self.data[field][key] = old
        self.data["hosts"][self.host] = ["elsewhere"]
        self.assertRefused(self.run_cli(), "current host is outside or ambiguous in allocation")

    def test_ssh_exited_pid_and_invalid_inputs_are_refused(self):
        self.assertRefused(self.run_cli(mode="ssh"), "--executor local", "local_only", 3)
        for pid in (0, -1):
            self.assertRefused(self.run_cli(pid=pid), "worker PID")
        self.assertRefused(self.run_cli(pid=99999999), "cannot verify worker process 99999999")
        for job in ("44;echo", "44_[1-2]", "0"):
            self.assertRefused(self.run_cli(job=job), "use N or N_TASK", "spec_invalid")
        self.assertRefused(self.run_cli("--step-id", "x"), "invalid numeric step id")
        self.assertRefused(self.run_cli("--hostlist", "bad;command"), "invalid hostlist expression")
        self.assertEqual(self.calls(), [])
        self.stop_worker()
        self.assertRefused(self.run_cli(), "worker process")

    def test_zombie_worker_is_refused(self):
        # Killed but not yet reaped: /proc still has the PID, in state Z.
        os.kill(self.worker.pid, signal.SIGKILL)
        stat = Path(f"/proc/{self.worker.pid}/stat")
        deadline = time.monotonic() + 10
        while True:
            try:
                state = stat.read_text().rpartition(")")[2].split()[0]
            except FileNotFoundError:  # seen on a Sherlock compute node; nothing to observe then
                self.skipTest("the worker was reaped before it could be seen as a zombie")
            if state == "Z":
                break
            self.assertLess(time.monotonic(), deadline, "worker never became a zombie")
            time.sleep(0.01)
        self.assertRefused(self.run_cli(), "worker process exited")

    def test_pidinfo_step_suffixes_are_job_membership(self):
        for suffix in ("", ".batch", ".extern", ".0", ".interactive", ".0+1"):
            with self.subTest(suffix=suffix):
                self.data["pidinfo"][str(self.worker.pid)] = self.pidinfo(f"44{suffix}")
                code, report = self.run_cli()
                self.assertEqual(code, 0, report)

    def test_scheduler_rpc_failures_are_retryable_reads_not_non_membership(self):
        for action in ("job", "pidinfo", "node"):
            with self.subTest(action=action):
                self.data["error"] = {
                    action: "slurm_load_jobs error: Socket timed out on send/recv operation\n"
                }
                self.assertRefused(
                    self.run_cli(), "Socket timed out on send/recv operation", "malformed_read"
                )
        self.data["error"] = {"pidinfo": "slurm_pid2jobid error: Invalid job id specified\n"}
        found, report = self.run_cli()
        self.assertRefused((found, report), "slurm_pid2jobid error: Invalid job id specified")
        self.assertIsNone(report["hint"])

    def test_scheduler_timeout_is_a_read_timeout(self):
        (self.tmp / "sherlock-kit.toml").write_text("command_timeout = 2\n")
        self.data["delay"] = {"pidinfo": 10}
        self.assertRefused(self.run_cli(), "allocation", "timeout", 4)

    def test_caller_hostlists_are_expanded_without_admission_policy(self):
        overlapping = "sh03-01n01,sh03-01n[01-02]"
        self.data["hosts"][overlapping] = ["sh03-01n01", "sh03-01n01", "sh03-01n02"]
        code, report = self.run_cli(*["--hostlist", overlapping] * 33)
        self.assertEqual(code, 0, report)
        self.assertEqual(
            report["hostlists"][-1]["nodes"], ["sh03-01n01", "sh03-01n01", "sh03-01n02"]
        )
        self.assertEqual(self.calls().count(["show", "hostnames", overlapping]), 1)
        self.data["hosts"][overlapping] = ["sh03-01n01", "bad host"]
        self.assertRefused(self.run_cli("--hostlist", overlapping), "malformed expanded hostlist")

    def test_step_proof_does_not_require_node_topology(self):
        self.data["node"][self.host] = self.node_record(
            CPUTot=None, ThreadsPerCore=None, CoresPerSocket=None, Sockets=None
        )
        code, report = self.run_cli("--step-id", "0")
        self.assertEqual(code, 0, report)
        self.assertNotIn("ThreadsPerCore", report["node_records"][0]["fields"])
        self.data["node"][self.host] = self.node_record(NodeName="elsewhere")
        self.assertRefused(self.run_cli(), "exactly one matching scheduler record")

    def test_documented_pidinfo_remaining_time_preserves_complete_raw_evidence(self):
        for identity, auxiliary in (
            (
                "Slurm job id 44.batch ends at Fri Sep 04 15:50:47 2020",
                "slurm_get_rem_time is 31535906",
            ),
            (
                "Slurm JobId=44 ends at Sat Oct 10 05:25:01 2026",
                "Job remaining time is 1192 seconds",
            ),
            ("Slurm JobId=44 ends at Sat Oct 10 05:25:01 2026", "Job remaining time is 0 seconds"),
            ("Slurm JobId=44 ends at Sat Oct 10 05:25:01 2026", None),
        ):
            with self.subTest(identity=identity, auxiliary=auxiliary):
                raw = identity if auxiliary is None else identity + "\n" + auxiliary
                self.data["pidinfo"][str(self.worker.pid)] = raw
                code, report = self.run_cli()
                self.assertEqual(code, 0, report)
                self.assertTrue(report["process"]["job_membership_verified"])
                evidence = report["membership_evidence"]["job"]
                self.assertEqual(evidence["raw"], raw)
                self.assertEqual(evidence["sha256"], hashlib.sha256(raw.encode()).hexdigest())

    def test_pidinfo_auxiliary_cannot_hide_ambiguous_identity_or_malformed_output(self):
        identity = self.pidinfo("44").splitlines()[0]
        auxiliary = "slurm_get_rem_time is 1200"
        for extra in (
            auxiliary + "\n" + identity,
            auxiliary + "\n" + identity.replace("JobId=44", "JobId=45"),
            auxiliary + "\n" + auxiliary,
            auxiliary + "\nJob remaining time is 1200 seconds",
            "slurm_get_rem_time is -1",
            "slurm_get_rem_time is unknown",
            "slurm_get_rem_time is 1.0",
            "Job remaining time is -1 seconds",
            "Job remaining time is 1200",
            "unrecognized extra output",
        ):
            with self.subTest(extra=extra):
                self.data["pidinfo"][str(self.worker.pid)] = identity + "\n" + extra
                self.assertRefused(self.run_cli(), "worker PID is not verified")
        self.data["pidinfo"][str(self.worker.pid)] = auxiliary
        self.assertRefused(self.run_cli(), "worker PID is not verified")

    def test_listpids_header_and_placeholders(self):
        original = self.data["listpids"]["44.0"]
        self.data["listpids"]["44.0"] = original.replace("GLOBALID", "OTHER")
        self.assertRefused(self.run_cli("--step-id", "0"), "unknown process membership table")
        self.data["listpids"]["44.0"] = original.replace(" 0       0  ", " 0       -  ")
        code, report = self.run_cli("--step-id", "0")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["process"]["step_membership_verified"])

    def test_owner_checked_before_process_contents_and_affinity_is_reported(self):
        from types import SimpleNamespace

        from sherlock_kit import allocation
        from sherlock_kit.cli import Context

        with (
            patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=self.uid + 1)),
            patch.object(Path, "read_text") as read,
        ):
            with self.assertRaises(allocation.ShkError):
                allocation.process_identity(self.worker.pid)
            read.assert_not_called()
        (self.tmp / "facts.json").write_text(json.dumps(self.data))
        # A worker may bind its threads while evidence is collected; affinity is not identity.
        with (
            patch.dict(os.environ, self.env),
            patch.object(os, "sched_getaffinity", side_effect=[{0}, {1}]) as affinity,
        ):
            outcome = allocation.allocation(
                allocation_namespace(self.worker.pid), Context(executor="local", cwd=self.tmp)
            )
        self.assertEqual(outcome.payload["process"]["cpu_affinity"], [0])
        self.assertEqual(affinity.call_count, 1)

    def test_process_exit_or_reuse_during_collection(self):
        from sherlock_kit import allocation
        from sherlock_kit.cli import Context

        (self.tmp / "facts.json").write_text(json.dumps(self.data))
        initial = allocation.process_identity(self.worker.pid)
        args = allocation_namespace(self.worker.pid)
        for later in (
            allocation.invalid("worker exited"),
            (initial[0], initial[1] + 1),
            (initial[0] + 1, initial[1]),
        ):
            with (
                self.subTest(later=later),
                patch.dict(os.environ, self.env),
                patch.object(allocation, "process_identity", side_effect=[initial, later]),
                self.assertRaises(allocation.ShkError) as raised,
            ):
                allocation.allocation(args, Context(executor="local", cwd=self.tmp))
            self.assertEqual(raised.exception.token, "allocation_invalid")


def allocation_namespace(pid):
    import argparse

    return argparse.Namespace(pid=str(pid), job_id="44", step_id=None, hostlist=[])
