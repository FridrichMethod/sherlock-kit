"""Exact release, self requeue and preflight through control.sh against argv-checking fakes.

Records follow the Slurm 25.11.8 `scontrol show job --oneliner` shape observed on Sherlock,
with synthetic ids, users and hosts. An array's meta physical id (the element that keeps
JobId == ArrayJobId) answers `show job <id>` with its own record AND every sibling, while
`show job <ArrayJobId>_<ArrayTaskId>` returns exactly one record; `scontrol listpids` takes
only a physical id and prints the physical JOBID, listing every process of the job, so the fake
lists every ancestor of itself (bash adds a command-substitution subshell that dash does not).
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
out = reply.get("out", "")
if "@PPID@" in out:
    import subprocess
    # Each process between this fake and the test, which is where control.sh runs.
    ancestors, pid = [], os.getppid()
    while pid > 1 and pid != int(os.environ["FAKE_SCONTROL_ROOT"]):
        ancestors.append(str(pid))
        ps = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True)
        pid = int(ps.stdout) if ps.stdout.strip().isdecimal() else 0
    out = "".join(
        "".join(line.replace("@PPID@", own) for own in ancestors) if "@PPID@" in line else line
        for line in out.splitlines(keepends=True)
    )
sys.stdout.write(out)
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
            FAKE_SCONTROL_ROOT=str(os.getpid()),
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
        # The last task keeps the meta record: SLURM_JOB_ID == SLURM_ARRAY_JOB_ID, and the
        # physical id answers with every sibling too, in either order.
        self.allocation("50", "50", "1")
        own = job_record("50", array="50", task="1")
        sibling = job_record("51", array="50", task="0")
        for order, records in enumerate((f"{own}\n{sibling}\n", f"{sibling}\n{own}\n")):
            with self.subTest(order=order):
                self.calls_file.unlink(missing_ok=True)
                self.scontrol({
                    "listpids 50": {"out": listpids("50")},
                    "show job 50 --oneliner": {"out": records},
                    "requeue 50_1": {},
                })
                code, report = self.requeue("--expect-token", "tok-1")
                self.assertEqual(
                    (code, report.get("action"), report.get("job_id")), (0, "requeued", "50_1")
                )
                self.assertEqual(
                    self.calls(),
                    [["listpids", "50"], ["show", "job", "50", "--oneliner"], ["requeue", "50_1"]],
                )

    def test_split_array_element_is_requeued_as_its_task(self):
        self.allocation("51", "50", "0")
        self.scontrol({
            "listpids 51": {"out": listpids("51")},
            "show job 51 --oneliner": {"out": job_record("51", array="50", task="0") + "\n"},
            "requeue 50_0": {},
        })
        code, report = self.requeue("--expect-token", "tok-1")
        self.assertEqual((code, report.get("job_id")), (0, "50_0"))
        self.assertEqual(self.calls()[-1], ["requeue", "50_0"])

    def test_inherited_array_variables_do_not_retarget_a_plain_job(self):
        # A plain sbatch run inside array task 70_3 inherits SLURM_ARRAY_*; Slurm's record of
        # the physical job, which has no ArrayJobId, decides the target.
        self.allocation("80", "70", "3")
        self.scontrol({
            "listpids 80": {"out": listpids("80")},
            "show job 80 --oneliner": {"out": job_record("80") + "\n"},
            "requeue 80": {},
        })
        code, report = self.requeue()
        self.assertEqual((code, report.get("job_id")), (0, "80"), report)
        self.assertEqual(
            self.calls(),
            [["listpids", "80"], ["show", "job", "80", "--oneliner"], ["requeue", "80"]],
        )

    def test_free_text_comments_never_retarget_the_requeue(self):
        # Slurm prints Comment (like SubmitLine and paths) after the real fields, as free text.
        cases = (
            ("51", "50", "0", "sweep ArrayTaskId=[0-99]", "50_0"),
            ("80", None, None, "rerun of ArrayJobId=70 ArrayTaskId=7", "80"),
        )
        for job, array, task, comment, target in cases:
            with self.subTest(job=job):
                self.calls_file.unlink(missing_ok=True)
                self.allocation(job, array, task)
                record = job_record(job, array=array, task=task, token=comment)
                self.scontrol({
                    f"listpids {job}": {"out": listpids(job)},
                    f"show job {job} --oneliner": {"out": record + "\n"},
                    f"requeue {target}": {},
                })
                code, report = self.requeue()
                self.assertEqual((code, report.get("job_id")), (0, target), report)
                self.assertEqual(self.calls()[-1], ["requeue", target])

    def test_free_text_comments_never_satisfy_the_policy_checks(self):
        self.allocation("80")
        record = job_record(
            "80", partition="normal", Requeue="0", token="note Partition=owners Requeue=1"
        )
        self.scontrol({
            "listpids 80": {"out": listpids("80")},
            "show job 80 --oneliner": {"out": record + "\n"},
        })
        code, report = self.requeue()
        self.assertEqual((code, report["message"]), (3, "control: SHK_REFUSED partition"))
        self.assertEqual(len(self.calls()), 2)

    def test_record_must_be_this_physical_allocation(self):
        # Only the record whose JobId is the PID-verified physical id is ever acted on.
        self.allocation("51", "50", "0")
        own = job_record("51", array="50", task="0")
        for case, records in enumerate((job_record("52", array="50", task="0"), f"{own}\n{own}")):
            with self.subTest(case=case):
                self.scontrol({
                    "listpids 51": {"out": listpids("51")},
                    "show job 51 --oneliner": {"out": records + "\n"},
                })
                code, report = self.requeue("--expect-token", "tok-1")
                self.assertEqual((code, report["message"]), (3, "control: SHK_REFUSED ambiguous"))
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
        self.assertEqual(code, 3)
        self.assertEqual(
            report["message"],
            "control: SHK_REFUSED listpids slurm_load_job error: Invalid job id specified",
        )
        self.assertNotIn(["requeue", "44"], self.calls())

    def test_program_refuses_any_target_but_its_own_allocation(self):
        self.scontrol({"listpids 44": {"out": listpids("44")}, "requeue 50": {}})
        local = executor.LocalExecutor()
        for job, array, task, target in (
            ("44", None, None, "50"),
            ("44", None, None, "44_0"),
            ("51", "50", "0", "50_0"),
            ("50", "50", "1", "50_1"),
            (None, None, None, "44"),
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
        for job, array, task in ((None, None, None), ("44_1", None, None), ("4x", "50", "0")):
            with self.subTest(job=job, array=array, task=task):
                self.allocation(job, array, task)
                code, report = self.requeue()
                self.assertEqual((code, report["error"]), (3, "self_only"))
        self.assertEqual(self.calls(), [])

    def test_requeue_follows_the_profile_requeue_flag(self):
        table = {
            name: partitions.Profile(name, False, name == "normal", False, False, True, "")
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

    def test_post_dispatch_failure_is_unknown_with_slurm_evidence(self):
        self.allocation("44")
        self.scontrol({
            "listpids 44": {"out": listpids("44")},
            "show job 44 --oneliner": {"out": job_record("44") + "\n"},
            "requeue 44": {
                "err": "Requested operation is presently disabled for job 44\n",
                "rc": 1,
            },
        })
        code, report = self.requeue()
        self.assertEqual((code, report["action"]), (5, "unknown"))
        self.assertNotIn("sbatch", report["detail"])
        self.assertIn("scontrol requeue exit 1", report["detail"])
        self.assertIn("Requested operation is presently disabled for job 44", report["detail"])
        self.assertIn("status --job-id 44 --details", report["next"])


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

    def test_release_ignores_scontrol_warnings_beside_the_record(self):
        warning = "scontrol: error: _parse_next_key: Parsing error at unrecognized key: Foo\n"
        self.scontrol({
            "show job 44 --oneliner": {"out": self.held(), "err": warning},
            "release 44": {},
        })
        code, report = self.release("--expect-token", "tok-1")
        self.assertEqual((code, report.get("action")), (0, "released"), report)

    def test_release_query_failure_keeps_slurm_evidence(self):
        self.scontrol({
            "show job 44 --oneliner": {
                "err": "slurm_load_jobs error: Invalid job id specified\n",
                "rc": 1,
            }
        })
        code, report = self.release("--expect-token", "tok-1")
        self.assertEqual(
            (code, report["message"]),
            (3, "control: SHK_REFUSED query slurm_load_jobs error: Invalid job id specified"),
        )

    def test_release_definitive_error_after_dispatch_is_unknown_with_evidence(self):
        self.scontrol({
            "show job 44 --oneliner": {"out": self.held()},
            "release 44": {"err": "Access/permission denied for job 44\n", "rc": 1},
        })
        code, report = self.release("--expect-token", "tok-1")
        self.assertEqual((code, report["action"]), (5, "unknown"))
        self.assertTrue(
            report["detail"].startswith("unparsed control reply: scontrol release exit 1")
        )
        self.assertIn("Access/permission denied for job 44", report["detail"])
        self.assertIn("--job-id 44", report["next"])


class PreflightClassification(ControlFlow):
    def sbatch(self, stderr, rc):
        message = self.tmp / "sbatch.err"
        message.write_text(stderr)
        tool = self.tools / "sbatch"
        tool.write_text(
            "#!/bin/sh\n"
            'for last in "$@"; do :; done\n'
            '[ "$last" = --test-only ] || { echo "fake sbatch: not a test-only call" >&2; exit 97; }\n'
            "cat >/dev/null\n"
            f'cat "{message}" >&2\n'
            f"exit {rc}\n"
        )
        tool.chmod(0o755)

    def test_controller_transport_failures_are_retryable_reads(self):
        for stderr in (
            "allocation failure: Unable to contact slurm controller (connect failure)\n",
            "allocation failure: Socket timed out on send/recv operation\n",
            "allocation failure: Zero Bytes were transmitted or received\n",
        ):
            with self.subTest(stderr=stderr):
                self.sbatch(stderr, 1)
                code, report = self.submit("--preflight")
                self.assertEqual((code, report["error"]), (1, "malformed_read"))
                self.assertIn(
                    stderr.strip().removeprefix("allocation failure: "), report["message"]
                )
                self.assertIn("retry once", report["hint"])
        self.assertFalse(self.remote.exists())

    def test_slurm_policy_rejection_stays_a_refusal_with_evidence(self):
        stderr = (
            "sbatch: error: QOSMaxSubmitJobPerUserLimit\n"
            "allocation failure: Job violates accounting/QOS policy "
            "(job submit limit, user's size and/or time limits)\n"
        )
        self.sbatch(stderr, 1)
        code, report = self.submit("--preflight")
        self.assertEqual((code, report["ok"], report["action"]), (3, False, "preflight"))
        self.assertEqual(report["preflight"], {"returncode": 1, "stdout": "", "stderr": stderr})
        self.assertFalse(self.remote.exists())
