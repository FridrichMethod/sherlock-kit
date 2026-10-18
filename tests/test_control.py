"""Exact release, self requeue and preflight through control.sh against argv-checking fakes.

Records follow the Slurm 25.11.8 `scontrol show job --oneliner` shape observed on Sherlock,
with synthetic ids, users and hosts. An array's meta physical id (the element that keeps
JobId == ArrayJobId) answers `show job <id>` with its own record AND every sibling, while
`show job <ArrayJobId>_<ArrayTaskId>` returns exactly one record; `scontrol listpids` takes
only a physical id and prints the physical JOBID.
"""

import json
import os
import sys
from types import MappingProxyType
from unittest.mock import patch

from fakes import SRC
from test_flows import Flow

sys.path.insert(0, str(SRC))
from sherlock_kit import executor, partitions

NAME = "shk.demo.train-a.0123456789"
FAKE = """
import json, os, sys
from pathlib import Path
replies = json.loads(Path(os.environ["FAKE_SCONTROL"]).read_text())
argv = " ".join(sys.argv[1:])
with open(os.environ["FAKE_SCONTROL_CALLS"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
reply = replies.get(argv)
if reply is None:
    sys.stderr.write(f"fake scontrol: unexpected argv {argv}\\n")
    sys.exit(97)
sys.stdout.write(reply.get("out", "").replace("@PPID@", str(os.getppid())))
sys.stderr.write(reply.get("err", ""))
sys.exit(reply.get("rc", 0))
"""


def job_record(job, *, array=None, task=None, partition="owners", token="tok-1", **changes):
    """One 25.11.8 oneliner job record (synthetic values)."""
    fields = {
        "JobId": job,
        "ArrayJobId": array,
        "ArrayTaskId": task,
        "JobName": NAME,
        "UserId": f"demo({os.getuid()})",
        "GroupId": "lab(1000)",
        "Priority": "1000",
        "Account": "lab",
        "QOS": "normal",
        "JobState": "RUNNING",
        "Reason": "None",
        "Dependency": "(null)",
        "Requeue": "1",
        "Restarts": "0",
        "BatchFlag": "1",
        "TimeLimit": "00:25:00",
        "SubmitTime": "2026-10-10T05:00:00",
        "StartTime": "2026-10-10T05:00:01",
        "Partition": partition,
        "AllocNode:Sid": "login1:1234",
        "NodeList": "node01",
        "BatchHost": "node01",
        "NumNodes": "1",
        "NumCPUs": "1",
        "NumTasks": "1",
        "CPUs/Task": "1",
        "ReqTRES": "cpu=1,mem=2G,node=1,billing=1",
        "AllocTRES": "cpu=1,mem=2G,node=1,billing=1",
        "Licenses": "fs_scratch",
        "Command": "/home/demo/job.sh",
        "WorkDir": "/home/demo",
        "Comment": token,
        "TresPerTask": "cpu=1",
        **changes,
    }
    return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


def listpids(job, step="batch"):
    return (
        "PID      JOBID    STEPID   LOCALID GLOBALID\n"
        f"@PPID@    {job} {step}    0       0        \n"
        f"-1       {job} extern   0       0        \n"
    )


class ControlFlow(Flow):
    def setUp(self):
        super().setUp()
        self.tools = self.tmp / "tools"
        self.tools.mkdir()
        self.calls_file = self.tmp / "scontrol-calls.jsonl"
        self.knobs(
            PATH=f"{self.tools}{os.pathsep}{os.environ['PATH']}",
            FAKE_SCONTROL=str(self.tmp / "scontrol.json"),
            FAKE_SCONTROL_CALLS=str(self.calls_file),
        )
        self.allocation()

    def allocation(self, job=None, array=None, task=None):
        """The SLURM_* identity this process inherits; unset means outside an allocation."""
        values = {"SLURM_JOB_ID": job, "SLURM_ARRAY_JOB_ID": array, "SLURM_ARRAY_TASK_ID": task}
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def scontrol(self, replies):
        """A fake scontrol that answers only these exact argv strings."""
        (self.tmp / "scontrol.json").write_text(json.dumps(replies))
        tool = self.tools / "scontrol"
        tool.write_text(f"#!{sys.executable}\n{FAKE}")
        tool.chmod(0o755)

    def calls(self):
        if not self.calls_file.exists():
            return []
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def requeue(self, *extra):
        return self.shk("requeue", "--self", "--expect-name", NAME, *extra)

    def release(self, *extra):
        return self.shk("release", "--job-id", "44", "--expect-name", NAME, *extra)


class SelfRequeue(ControlFlow):
    def test_meta_array_element_requeues_only_its_own_task(self):
        # The last task keeps the meta record: SLURM_JOB_ID == SLURM_ARRAY_JOB_ID.
        self.allocation("50", "50", "1")
        own = job_record("50", array="50", task="1")
        sibling = job_record("51", array="50", task="0")
        self.scontrol({
            "listpids 50": {"out": listpids("50")},
            "show job 50 --oneliner": {"out": f"{own}\n{sibling}\n"},
            "show job 50_1 --oneliner": {"out": own + "\n"},
            "requeue 50_1": {},
        })
        code, report = self.requeue("--expect-token", "tok-1")
        self.assertEqual(
            (code, report.get("action"), report.get("job_id")), (0, "requeued", "50_1")
        )
        self.assertEqual(
            self.calls(),
            [["listpids", "50"], ["show", "job", "50_1", "--oneliner"], ["requeue", "50_1"]],
        )

    def test_split_array_element_is_shown_and_requeued_as_its_task(self):
        self.allocation("51", "50", "0")
        self.scontrol({
            "listpids 51": {"out": listpids("51")},
            "show job 50_0 --oneliner": {"out": job_record("51", array="50", task="0") + "\n"},
            "requeue 50_0": {},
        })
        code, report = self.requeue("--expect-token", "tok-1")
        self.assertEqual((code, report.get("job_id")), (0, "50_0"))
        self.assertEqual(self.calls()[-1], ["requeue", "50_0"])

    def test_array_record_must_be_this_physical_allocation(self):
        self.allocation("51", "50", "0")
        self.scontrol({
            "listpids 51": {"out": listpids("51")},
            "show job 50_0 --oneliner": {"out": job_record("52", array="50", task="0") + "\n"},
        })
        code, report = self.requeue("--expect-token", "tok-1")
        self.assertEqual((code, report["message"]), (3, "control: SHK_REFUSED physical"))
        self.assertNotIn("requeue", [call[0] for call in self.calls()])

    def test_token_is_optional_but_checked_when_given(self):
        self.allocation("44")
        self.scontrol({
            "listpids 44": {"out": listpids("44")},
            "show job 44 --oneliner": {"out": job_record("44", token="(null)") + "\n"},
            "requeue 44": {},
        })
        code, report = self.requeue()
        self.assertEqual(
            (code, report["action"], report["submission_token"]), (0, "requeued", None)
        )
        self.calls_file.unlink()
        code, report = self.requeue("--expect-token", "tok-1")
        self.assertEqual((code, report["message"]), (3, "control: SHK_REFUSED token"))
        self.assertNotIn(["requeue", "44"], self.calls())
        self.assertEqual(self.requeue("--expect-token", "bad|token")[1]["error"], "spec_invalid")

    def test_pid_membership_in_the_physical_job_is_required(self):
        self.allocation("44")
        foreign = (
            "PID      JOBID    STEPID   LOCALID GLOBALID\n4242     44 batch    0       0        \n"
        )
        replies = {
            "listpids 44": {"out": foreign},
            "show job 44 --oneliner": {"out": job_record("44") + "\n"},
            "requeue 44": {},
        }
        self.scontrol(replies)
        code, report = self.requeue("--expect-token", "tok-1")
        self.assertEqual((code, report["message"]), (3, "control: SHK_REFUSED process"))
        self.assertEqual(self.calls(), [["listpids", "44"]])
        # The caller's PID listed under another physical job is not membership either.
        replies["listpids 44"] = {"out": listpids("45")}
        self.scontrol(replies)
        self.assertEqual(self.requeue("--expect-token", "tok-1")[1]["message"], report["message"])
        replies["listpids 44"] = {
            "err": "slurm_load_job error: Invalid job id specified\n",
            "rc": 1,
        }
        self.scontrol(replies)
        code, report = self.requeue("--expect-token", "tok-1")
        self.assertEqual((code, report["message"]), (3, "control: SHK_REFUSED listpids"))
        self.assertNotIn(["requeue", "44"], self.calls())

    def test_program_refuses_any_target_but_its_own_allocation(self):
        self.scontrol({"listpids 44": {"out": listpids("44")}, "requeue 50": {}})
        local = executor.LocalExecutor()
        for job, array, task, target in (
            ("44", None, None, "50"),
            ("44", None, None, "44_0"),
            ("51", "50", "0", "51"),
            ("50", "50", "1", "50"),
        ):
            with self.subTest(job=job, array=array, task=task, target=target):
                self.allocation(job, array, task)
                reply = executor.run_program(
                    local, "control", ["requeue", target, NAME, "", "owners"], mutation=True
                )
                self.assertEqual(reply.stdout, "SHK_REFUSED allocation\n")
        self.assertEqual(self.calls(), [])

    def test_requeue_needs_the_local_executor_and_an_allocation(self):
        self.scontrol({})
        self.executor = "ssh"
        self.allocation("44")
        code, report = self.requeue()
        self.assertEqual((code, report["error"]), (3, "self_only"))
        self.executor = "local"
        for job, array, task in ((None, None, None), ("44_1", None, None), ("51", "50", None)):
            with self.subTest(job=job, array=array, task=task):
                self.allocation(job, array, task)
                code, report = self.requeue()
                self.assertEqual((code, report["error"]), (3, "self_only"))
        self.assertEqual(self.calls(), [])

    def test_requeue_follows_the_profile_requeue_flag(self):
        table = {
            name: partitions.Profile(name, False, name == "normal", False, False, "")
            for name in ("normal", "owners")
        }
        self.allocation("44")
        replies = {
            "listpids 44": {"out": listpids("44")},
            "show job 44 --oneliner": {"out": job_record("44", partition="normal") + "\n"},
            "requeue 44": {},
        }
        self.scontrol(replies)
        with patch.object(partitions, "profiles", return_value=MappingProxyType(table)):
            self.assertEqual(self.requeue()[0], 0)
            replies["show job 44 --oneliner"] = {"out": job_record("44") + "\n"}
            self.scontrol(replies)
            code, report = self.requeue()
        self.assertEqual((code, report["message"]), (3, "control: SHK_REFUSED partition"))


class ExactRelease(ControlFlow):
    def held(self, **changes):
        fields = {"JobState": "PENDING", "Reason": "JobHeldUser", "Priority": "0"}
        return job_record("44", partition="normal", **{**fields, **changes}) + "\n"

    def test_release_still_requires_the_token(self):
        self.scontrol({"show job 44 --oneliner": {"out": self.held(Comment="(null)")}})
        with self.assertRaises(SystemExit) as raised:
            self.release()
        self.assertEqual(raised.exception.code, 2)
        self.assertEqual(self.calls(), [])
