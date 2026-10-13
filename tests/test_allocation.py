"""Public local allocation CLI against real processes and fake Slurm binaries."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import SRC

sys.path.insert(0, str(SRC))


class AllocationCLI(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="shk-allocation-")
        self.addCleanup(self.directory.cleanup)
        self.tmp = Path(self.directory.name)
        self.worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(self.stop_worker)
        self.host = socket.gethostname().split(".")[0]
        self.data = {
            "job": f"JobId=44 UserId=user({os.getuid()}) JobState=RUNNING Partition=normal NodeList={self.host} NumNodes=1 NumCPUs=4 AllocTRES=cpu=4,mem=2G,gres/gpu=2,gres/gpu:a=2 Gres=gpu:a:2(IDX:0,1)",
            "step": f"StepId=44.0 UserId=user({os.getuid()}) State=RUNNING Partition=normal Nodes={self.host} NumNodes=1 NumTasks=2 CPUs=2 TRES=cpu=2,gres/gpu=1 Gres=gpu:a:1",
            "node": f"NodeName={self.host} CPUTot=128 ThreadsPerCore=2 CoresPerSocket=32 Sockets=2",
            "pidinfo": "Slurm JobId=44 ends at 2026-10-14T00:00:00",
            "listpids": f"PID JOBID STEPID LOCALID GLOBALID\n{self.worker.pid} 44 0 0 0",
            "hosts": {self.host: [self.host], "demo[1-2]": ["demo1", "demo2"]},
        }
        tool = self.tmp / "scontrol"
        tool.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "d=json.loads(Path(os.environ['FAKE_ALLOCATION']).read_text())\n"
            "a=sys.argv[1:]\n"
            "with open(os.environ['FAKE_CALLS'], 'a') as f: f.write(json.dumps(a)+'\\n')\n"
            "if d.get('fail'): sys.exit(7)\n"
            "if a[0]=='pidinfo': print(d['pidinfo'])\n"
            "elif a[0]=='listpids': print(d['listpids'])\n"
            "elif a[:2]==['show','hostnames']: print('\\n'.join(d['hosts'].get(a[2], [])))\n"
            "else: print(d[a[1]])\n"
        )
        tool.chmod(0o755)
        ssh = self.tmp / "ssh"
        ssh.write_text("#!/bin/sh\nexit 99\n")
        ssh.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.tmp}{os.pathsep}{os.environ['PATH']}",
            "PYTHONPATH": str(SRC),
            "FAKE_ALLOCATION": str(self.tmp / "facts.json"),
            "FAKE_CALLS": str(self.tmp / "calls"),
        }

    def stop_worker(self):
        if self.worker.poll() is None:
            self.worker.terminate()
        self.worker.wait(timeout=5)

    def run_cli(self, *extra, job="44", pid=None, mode="local"):
        (self.tmp / "facts.json").write_text(json.dumps(self.data))
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
            timeout=10,
            check=False,
        )
        self.assertNotEqual(result.returncode, 2, result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_worker_pid_affinity_actual_resources_and_hashes(self):
        code, report = self.run_cli("--hostlist", "demo[1-2]")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["process"]["pid"], self.worker.pid)
        self.assertEqual(
            report["process"]["cpu_affinity"], sorted(os.sched_getaffinity(self.worker.pid))
        )
        self.assertTrue(report["process"]["job_membership_verified"])
        self.assertIsNone(report["process"]["step_membership_verified"])
        self.assertEqual(report["job"]["allocated_gpus"], 2)
        self.assertEqual(
            report["job"]["sha256"], hashlib.sha256(self.data["job"].encode()).hexdigest()
        )
        self.assertEqual(report["hostlists"][-1]["nodes"], ["demo1", "demo2"])
        self.assertTrue(report["complete"])

    def test_numeric_step_has_separate_resources_topology_and_membership(self):
        code, report = self.run_cli("--step-id", "0")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["job"]["allocated_cpus"], 4)
        self.assertEqual(report["step"]["allocated_cpus"], 2)
        self.assertEqual(report["step"]["tasks"], 2)
        self.assertEqual(report["node_records"][0]["fields"]["ThreadsPerCore"], "2")
        self.assertTrue(report["process"]["step_membership_verified"])
        self.assertIn(str(self.worker.pid), report["membership_evidence"]["step"]["raw"])

    def test_array_logical_identity_preserves_physical_id(self):
        self.data["job"] = self.data["job"].replace(
            "JobId=44", "JobId=45 ArrayJobId=44 ArrayTaskId=2"
        )
        self.data["pidinfo"] = self.data["pidinfo"].replace("44", "45")
        code, report = self.run_cli(job="44_2")
        self.assertEqual(code, 0, report)
        self.assertEqual((report["job"]["job_id"], report["job"]["array_task_id"]), ("45", "2"))
        self.assertNotEqual(self.run_cli(job="44_3")[0], 0)

    def test_legacy_and_cpu_only_actual_resources_do_not_invent_tres(self):
        self.data["job"] = self.data["job"].split(" AllocTRES=")[0] + " Gres=(null)"
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["job"]["allocated_gpus"], 0)
        self.assertIsNone(report["job"]["allocated_cpu_tres"])
        self.assertIsNone(report["job"]["allocated_gpu_tres"])
        self.data["job"] = self.data["job"].replace(" Gres=(null)", " AllocTRES=cpu=4,mem=2G")
        self.assertEqual(self.run_cli()[1]["job"]["allocated_gpus"], 0)

    def test_missing_counts_remain_null(self):
        self.data["job"] = self.data["job"].replace(" NumCPUs=4", "").replace("cpu=4,", "")
        code, report = self.run_cli()
        self.assertEqual(code, 0, report)
        self.assertIsNone(report["job"]["allocated_cpus"])

    def test_job_identity_owner_state_duplicate_and_resources_fail_closed(self):
        original = self.data["job"]
        for old, new in [
            ("JobId=44", "JobId=45"),
            (f"({os.getuid()})", f"({os.getuid() + 1})"),
            ("RUNNING", "PENDING"),
            ("JobId=44", "JobId=44 JobId=44"),
            ("NumCPUs=4", "NumCPUs=-1"),
            ("cpu=4,", "cpu=5,"),
            ("gres/gpu=2", "gres/gpu=1"),
            ("gres/gpu=2", "gres/gpu=bad"),
            ("NumNodes=1", "NumNodes=2"),
        ]:
            with self.subTest(new=new):
                self.data["job"] = original.replace(old, new)
                self.assertNotEqual(self.run_cli()[0], 0)
        self.data["job"] = original + "\n" + original
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_pid_host_step_and_topology_fail_closed(self):
        cases = [
            ("pidinfo", "Slurm JobId=45 ends at tomorrow"),
            ("pidinfo", "unrecognized pid evidence"),
            ("pidinfo", self.data["pidinfo"] + "\n" + self.data["pidinfo"]),
            ("listpids", "PID JOBID STEPID LOCALID GLOBALID\n1 44 0 0 0"),
            ("listpids", self.data["listpids"] + "\n" + self.data["listpids"].splitlines()[-1]),
            ("step", self.data["step"].replace("44.0", "44.1")),
            ("step", self.data["step"].replace("CPUs=2", "CPUs=bad")),
            ("node", self.data["node"].replace("ThreadsPerCore=2", "ThreadsPerCore=bad")),
        ]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                old = self.data[field]
                self.data[field] = value
                self.assertNotEqual(self.run_cli("--step-id", "0")[0], 0)
                self.data[field] = old
        self.data["hosts"][self.host] = ["elsewhere"]
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_ssh_exited_pid_and_invalid_inputs_are_refused(self):
        self.assertEqual(self.run_cli(mode="ssh")[0], 3)
        for pid in (0, -1, 99999999):
            self.assertNotEqual(self.run_cli(pid=pid)[0], 0)
        for job in ("44;echo", "44_[1-2]", "0"):
            self.assertNotEqual(self.run_cli(job=job)[0], 0)
        self.stop_worker()
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_scheduler_failure_and_malformed_hostlist_are_refused(self):
        self.data["fail"] = True
        self.assertNotEqual(self.run_cli()[0], 0)
        self.data.pop("fail")
        self.assertNotEqual(self.run_cli("--hostlist", "bad;command")[0], 0)

    def test_generic_multinode_step_and_native_cpu_only_forms(self):
        self.data["job"] = (
            self
            .data["job"]
            .replace(f"NodeList={self.host}", f"NodeList={self.host},demo2")
            .replace("NumNodes=1", "NumNodes=2")
        )
        self.data["hosts"][f"{self.host},demo2"] = [self.host, "demo2"]
        self.data["step"] = (
            self
            .data["step"]
            .replace(
                f"Nodes={self.host} NumNodes=1 NumTasks=2", f"NodeList={self.host} Nodes=1 Tasks=2"
            )
            .replace(f"UserId=user({os.getuid()})", f"UserId={os.getuid()}")
        )
        code, report = self.run_cli("--step-id", "0")
        self.assertEqual(code, 0, report)
        self.assertEqual(report["job"]["num_nodes"], 2)
        self.assertEqual(report["step"]["num_nodes"], 1)
        self.assertEqual(len(report["node_records"]), 1)
        self.data["job"] = (
            self.data["job"].split(" AllocTRES=")[0].replace("NumCPUs=4", "NumCPUs=0")
            + " Gres=(null)"
        )
        self.assertEqual(self.run_cli()[1]["job"]["allocated_cpus"], 0)

    def test_tres_aliases_null_legacy_and_request_only(self):
        original = self.data["job"]
        for field in ("Gres", "GRES", "JOB_GRES"):
            self.data["job"] = original.replace("Gres=gpu:a:2(IDX:0,1)", f"{field}=(null)")
            self.assertEqual(self.run_cli()[1]["job"]["allocated_gpus"], 2)
        self.data["job"] = original.replace("gres/gpu=2,gres/gpu:a=2", "gres/gpu:a=1,gres/gpu:b=1")
        self.assertEqual(self.run_cli()[1]["job"]["allocated_gpus"], 2)
        for replacement in ("gres/gpu=2,gres/gpu=2", "gres/gpu:a=bad", "cpu=4,cpu=4"):
            self.data["job"] = original.replace("gres/gpu=2,gres/gpu:a=2", replacement)
            self.assertNotEqual(self.run_cli()[0], 0)
        self.data["job"] = original + " GRES=gpu:a:1"
        self.assertNotEqual(self.run_cli()[0], 0)
        self.data["job"] = (
            original.split(" AllocTRES=")[0] + " ReqTRES=cpu=4,gres/gpu=2 TresPerNode=gres/gpu:2"
        )
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_older_pidinfo_and_header_malformed_membership(self):
        self.data["pidinfo"] = "Slurm job id 44.batch ends at 2026-10-14T00:00:00"
        self.assertEqual(self.run_cli()[0], 0)
        original = self.data["listpids"]
        for changed in (
            original.replace("GLOBALID", "OTHER"),
            original + "\nmalformed",
            original.replace(" 0 0 0", " 1 0 0"),
        ):
            self.data["listpids"] = changed
            self.assertNotEqual(self.run_cli("--step-id", "0")[0], 0)

    def test_auxiliary_listpids_placeholders_do_not_limit_generic_proof(self):
        self.data["listpids"] = self.data["listpids"].replace(" 0 0 0", " 0 - -1")
        code, report = self.run_cli("--step-id", "0")
        self.assertEqual(code, 0, report)
        self.assertTrue(report["process"]["step_membership_verified"])

    def test_unknown_populated_gpu_tres_is_never_cpu_only(self):
        self.data["job"] = (
            self
            .data["job"]
            .replace("gres/gpu=2,gres/gpu:a=2", "gres/gpu=Unknown")
            .replace("Gres=gpu:a:2(IDX:0,1)", "Gres=(null)")
        )
        self.assertNotEqual(self.run_cli()[0], 0)

    def test_owner_checked_before_process_contents_and_affinity_change(self):
        from types import SimpleNamespace

        from sherlock_kit import allocation
        from sherlock_kit.cli import Context

        with (
            patch.object(Path, "stat", return_value=SimpleNamespace(st_uid=os.getuid() + 1)),
            patch.object(Path, "read_text") as read,
        ):
            with self.assertRaises(allocation.ShkError):
                allocation.process_identity(self.worker.pid)
            read.assert_not_called()
        (self.tmp / "facts.json").write_text(json.dumps(self.data))
        with (
            patch.dict(os.environ, self.env),
            patch.object(os, "sched_getaffinity", side_effect=[{0, 1}, {0}]),
            self.assertRaises(allocation.ShkError),
        ):
            allocation.allocation(
                allocation_namespace(self.worker.pid), Context(executor="local", cwd=self.tmp)
            )

    def test_read_timeout_and_process_exit_during_collection(self):
        from sherlock_kit import allocation, executor
        from sherlock_kit.cli import Context

        with (
            patch.object(executor, "run_program", return_value=executor.Result("timeout")),
            self.assertRaises(allocation.ShkError),
        ):
            allocation.allocation(
                allocation_namespace(self.worker.pid), Context(executor="local", cwd=self.tmp)
            )
        (self.tmp / "facts.json").write_text(json.dumps(self.data))
        initial = allocation.process_identity(self.worker.pid)
        with (
            patch.dict(os.environ, self.env),
            patch.object(
                allocation,
                "process_identity",
                side_effect=[initial, allocation.invalid("worker exited")],
            ),
            self.assertRaises(allocation.ShkError),
        ):
            allocation.allocation(
                allocation_namespace(self.worker.pid), Context(executor="local", cwd=self.tmp)
            )

    def test_process_reuse_and_uid_changes_are_detected(self):
        from sherlock_kit import allocation
        from sherlock_kit.cli import Context

        args = allocation_namespace(self.worker.pid)
        for identities in [
            ((os.getuid(), 1), (os.getuid(), 2)),
            ((os.getuid(), 1), (os.getuid() + 1, 1)),
        ]:
            with (
                patch.object(allocation, "process_identity", side_effect=identities),
                patch.dict(os.environ, self.env),
            ):
                (self.tmp / "facts.json").write_text(json.dumps(self.data))
                with self.assertRaises(allocation.ShkError):
                    allocation.allocation(args, Context(executor="local", cwd=self.tmp))


def allocation_namespace(pid):
    import argparse

    return argparse.Namespace(pid=str(pid), job_id="44", step_id=None, hostlist=[])
