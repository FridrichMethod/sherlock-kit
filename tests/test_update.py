"""shk update through update.sh against an argv-exact fake scontrol, under dash and bash as sh.

Records follow the field order of the Slurm 25.11 formatter (src/scontrol/info_job.c at tag
slurm-25-11-9-1), not records observed on Sherlock: the array identity and ArrayTaskThrottle
before JobName, then TimeLimit and TimeMin, Partition, ReqTRES, Features, DelayBoot and Prefer,
then the free text (Command, SubmitLine, WorkDir, Comment) and the optional TresPer* fields
last. `show job <ArrayJobId>` answers with every record of the array. Values are synthetic.
"""

import json
import os
import shutil
import sys
from unittest.mock import patch

from fakes import SRC, install_fake_ssh
from test_control import NAME, ControlFlow
from test_flows import NEXT

sys.path.insert(0, str(SRC))
from sherlock_kit import executor

# Answers exact argv strings; a list of replies is used call by call and its last one repeats.
FAKE = """
import json, os, sys
from pathlib import Path
replies = json.loads(Path(os.environ["FAKE_SCONTROL"]).read_text())
argv = " ".join(sys.argv[1:])
log = Path(os.environ["FAKE_SCONTROL_CALLS"])
earlier = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
with log.open("a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
answers = replies.get(argv)
if answers is None:
    sys.stderr.write(f"fake scontrol: unexpected argv {argv}\\n")
    sys.exit(97)
answers = answers if isinstance(answers, list) else [answers]
reply = answers[min(sum(" ".join(call) == argv for call in earlier), len(answers) - 1)]
sys.stdout.write(reply.get("out", ""))
sys.stderr.write(reply.get("err", ""))
sys.exit(reply.get("rc", 0))
"""
GPU_TRES = "cpu=1,mem=2G,node=1,billing=1,gres/gpu=1"
SAME_CLASS = "normal,bigmem,dev,service,gpu,bioe,stat,btrippe,possu"  # not preemptible


def record(job="44", *, array=None, task=None, throttle=None, **changes):
    """One record in the Slurm 25.11 oneliner field order; a value of None leaves it out."""
    fields = {
        "JobId": job,
        "ArrayJobId": array,
        "ArrayTaskId": task,
        "ArrayTaskThrottle": throttle,
        "JobName": NAME,
        "UserId": f"demo({os.getuid()})",
        "GroupId": "lab(1000)",
        "MCS_label": "N/A",
        "Priority": "1000",
        "Nice": "0",
        "Account": "lab",
        "QOS": "normal",
        "JobState": "PENDING",
        "Reason": "Priority",
        "Dependency": "(null)",
        "Requeue": "0",
        "Restarts": "0",
        "BatchFlag": "1",
        "Reboot": "0",
        "ExitCode": "0:0",
        "RunTime": "00:00:00",
        "TimeLimit": "02:00:00",
        "TimeMin": "N/A",
        "SubmitTime": "2026-10-10T05:00:00",
        "EligibleTime": "2026-10-10T05:00:00",
        "AccrueTime": "2026-10-10T05:00:00",
        "StartTime": "Unknown",
        "EndTime": "Unknown",
        "Deadline": "N/A",
        "SuspendTime": "None",
        "SecsPreSuspend": "0",
        "LastSchedEval": "2026-10-10T05:00:01",
        "Scheduler": "Main",
        "Partition": "normal",
        "AllocNode:Sid": "login1:1234",
        "ReqNodeList": "(null)",
        "ExcNodeList": "(null)",
        "NodeList": "(null)",
        "NumNodes": "1",
        "NumCPUs": "1",
        "NumTasks": "1",
        "CPUs/Task": "1",
        "ReqB:S:C:T": "0:0:*:*",
        "ReqTRES": "cpu=1,mem=2G,node=1,billing=1",
        "AllocTRES": "(null)",
        "Socks/Node": "*",
        "NtasksPerN:B:S:C": "0:0:*:*",
        "CoreSpec": "*",
        "MinCPUsNode": "1",
        "MinMemoryNode": "2G",
        "MinTmpDiskNode": "0",
        "Features": "(null)",
        "DelayBoot": "00:00:00",
        "Prefer": None,
        "OverSubscribe": "OK",
        "Contiguous": "0",
        "Licenses": "(null)",
        "LicensesAlloc": "(null)",
        "Network": "(null)",
        "Command": "/home/demo/job.sh",
        "SubmitLine": "sbatch --time=120 job.sh",
        "WorkDir": "/home/demo",
        "Comment": "tok-1",
        "StdErr": "/home/demo/slurm.err",
        "StdIn": "/dev/null",
        "StdOut": "/home/demo/slurm.out",
        "TresPerTask": "cpu=1",
        **changes,
    }
    return " ".join(f"{key}={value}" for key, value in fields.items() if value is not None)


def lines(*records):
    return "".join(line + "\n" for line in records)


def partition(name, max_time="2-00:00:00"):
    """A 25.11 `show partition --oneliner` reply, shortened after MaxTime."""
    return (
        f"PartitionName={name} AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL AllocNodes=ALL "
        "Default=NO QoS=N/A DefaultTime=02:00:00 DisableRootJobs=NO ExclusiveUser=NO "
        f"ExclusiveTopo=NO GraceTime=0 Hidden=NO MaxNodes=UNLIMITED MaxTime={max_time} "
        "MinNodes=0 LLN=NO MaxCPUsPerNode=UNLIMITED State=UP TotalCPUs=64 TotalNodes=2\n"
    )


def show(job="44"):
    return f"show job {job} --oneliner"


class UpdateFlow(ControlFlow):
    def setUp(self):
        super().setUp()
        self.logs = 0

    def scontrol(self, replies, tool="scontrol"):
        (self.tmp / "scontrol.json").write_text(json.dumps(replies))
        path = self.tools / tool
        path.write_text(f"#!{sys.executable}\n{FAKE}")
        path.chmod(0o755)

    def fresh(self):
        """A new call log: each case then sees, and the fake counts, only its own calls."""
        self.logs += 1
        self.calls_file = self.tmp / f"scontrol-calls-{self.logs}.jsonl"
        os.environ["FAKE_SCONTROL_CALLS"] = str(self.calls_file)

    def update(self, *extra, job="44", name=NAME):
        return self.shk("update", "--job-id", job, "--expect-name", name, *extra)

    def dispatches(self):
        return [call for call in self.calls() if call[0] == "update"]

    def refused(self, why, *extra, job="44", name=NAME):
        code, report = self.update(*extra, job=job, name=name)
        self.assertEqual(
            (code, report.get("error"), report.get("message")),
            (3, "remote_refused", f"update: SHK_REFUSED {why}"),
            report,
        )
        self.assertEqual(self.dispatches(), [])

    def unknown(self, report, job="44"):
        self.assertEqual(
            report,
            {
                "schema_version": 1,
                "ok": False,
                "action": "unknown",
                "name": NAME,
                "detail": report["detail"],  # each caller checks the text
                "next": f"shk --project-dir {self.tmp} --executor local status "
                f"--job-id {job} --details (not before {NEXT})",
            },
        )
        return report["detail"]


class UpdateCases:
    """Each case runs update.sh for real; mixed into a dash flow and a bash-as-sh flow."""

    def test_time_is_lowered_with_the_records_before_and_after(self):
        self.scontrol({
            show(): [{"out": lines(record())}, {"out": lines(record(TimeLimit="01:00:00"))}],
            "update JobId=44 TimeLimit=60": {},
        })
        code, report = self.update("--time", "1h")
        before = {
            "job_id": "44",
            "array_task_id": None,
            "state": "PENDING",
            "TimeLimit": "02:00:00",
            "TimeMin": "N/A",
        }
        expected = {
            "schema_version": 1,
            "ok": True,
            "action": "updated",
            "job_id": "44",
            "name": NAME,
            "submission_token": None,
            "field": "time",
            "requested": {"TimeLimit": "60"},
            "before": [before],
            "after": [{**before, "TimeLimit": "01:00:00"}],
            "slurm_messages": None,
            "executor": "local",
            "queried_at": "2026-10-10T12:00:00Z",
            "next_check_after": NEXT,
        }
        self.assertEqual((code, report), (0, expected))
        self.assertEqual(list(report), list(expected))
        self.assertEqual(
            self.calls(),
            [
                ["show", "job", "44", "--oneliner"],
                ["update", "JobId=44", "TimeLimit=60"],
                ["show", "job", "44", "--oneliner"],
            ],
        )

    def test_seconds_round_up_and_days_count_in_the_time_limit(self):
        cases = (
            ("1-00:00:00", "90m", "TimeLimit=90", "01:30:00"),
            ("02:00:00", "1:59:01", "TimeLimit=120", None),  # rounds up to the current limit
            ("7-00:00:00", "47h", "TimeLimit=2820", "1-23:00:00"),
        )
        for limit, given, sent, shown in cases:
            with self.subTest(given=given):
                self.fresh()
                after = record(TimeLimit=shown or limit)
                self.scontrol({
                    show(): [{"out": lines(record(TimeLimit=limit))}, {"out": lines(after)}],
                    f"update JobId=44 {sent}": {},
                })
                code, report = self.update("--time", given)
                self.assertEqual(
                    (code, report["action"]), (0, "unchanged" if shown is None else "updated")
                )
                self.assertEqual(len(self.dispatches()), 0 if shown is None else 1)

    def test_a_gpu_job_moves_within_a_gpu_set_in_any_stored_order(self):
        gpu_job = {"ReqTRES": GPU_TRES, "TresPerNode": "gres/gpu:1"}
        self.scontrol({
            show(): [
                {"out": lines(record(Partition="gpu", **gpu_job))},
                {"out": lines(record(Partition="bioe,gpu", **gpu_job))},  # re-sorted by tier
            ],
            "show partition gpu --oneliner": {"out": partition("gpu")},
            "show partition bioe --oneliner": {"out": partition("bioe", "7-00:00:00")},
            "update JobId=44 Partition=gpu,bioe": {},
        })
        code, report = self.update("--partition", "gpu,bioe", "--expect-token", "tok-1")
        self.assertEqual((code, report["action"]), (0, "updated"), report)
        self.assertEqual(
            (report["requested"], report["submission_token"]), ({"Partition": "gpu,bioe"}, "tok-1")
        )
        self.assertEqual(
            [(found["Partition"], found["Requeue"]) for found in report["after"]],
            [("bioe,gpu", "0")],
        )
        self.assertEqual(
            self.calls()[1:3],
            [
                ["show", "partition", "gpu", "--oneliner"],
                ["show", "partition", "bioe", "--oneliner"],
            ],
        )

    def test_constraint_and_prefer_go_together_and_each_alone(self):
        allowed, preferred = (
            "(CPU_GEN:SKX|CPU_GEN:CLX)&IB:HDR",
            "[GPU_SKU:A100_SXM4*2|GPU_SKU:H100]",
        )
        cases = (
            (("--constraint", allowed, "--prefer", preferred), [allowed, preferred]),
            (("--constraint", allowed), [allowed, None]),
            (("--prefer", preferred), [None, preferred]),
        )
        for argv, (features, prefer) in cases:
            with self.subTest(argv=argv):
                self.fresh()
                elements = [f"Features={features}"] if features else []
                elements += [f"Prefer={prefer}"] if prefer else []
                after = record(Features=features or "(null)", Prefer=prefer)
                self.scontrol({
                    show(): [{"out": lines(record())}, {"out": lines(after)}],
                    " ".join(["update", "JobId=44", *elements]): {},
                })
                code, report = self.update(*argv)
                self.assertEqual((code, report["action"]), (0, "updated"), report)
                self.assertEqual(self.dispatches(), [["update", "JobId=44", *elements]])
                self.assertEqual(
                    [(found["Features"], found["Prefer"]) for found in report["before"]],
                    [("(null)", None)],
                )
                self.assertEqual(report["after"][0]["Features"], features if features else "(null)")

    def test_a_whole_pending_array_is_one_meta_record(self):
        meta = {"array": "50", "task": "0-3%2", "throttle": "2"}
        self.scontrol({
            show("50"): [
                {"out": lines(record("50", **meta))},
                {"out": lines(record("50", **meta, TimeLimit="00:30:00"))},
            ],
            "update JobId=50 TimeLimit=30": {},
        })
        code, report = self.update("--time", "30m", job="50")
        self.assertEqual((code, report["action"]), (0, "updated"), report)
        self.assertEqual(
            [(found["job_id"], found["array_task_id"]) for found in report["after"]],
            [("50", "0-3%2")],
        )

    def test_every_pending_split_record_of_an_array_is_updated(self):
        def array(partition):
            return lines(
                record("50", array="50", task="2-3", Partition=partition),
                record("52", array="50", task="1", Partition=partition),
                record("51", array="50", task="0", Partition=partition),
            )

        self.scontrol({
            show("50"): [{"out": array("normal")}, {"out": array("bigmem")}],
            "show partition bigmem --oneliner": {"out": partition("bigmem")},
            "update JobId=50 Partition=bigmem": {},
        })
        code, report = self.update("--partition", "bigmem", job="50")
        self.assertEqual((code, report["action"]), (0, "updated"), report)
        self.assertEqual([found["job_id"] for found in report["before"]], ["50", "52", "51"])
        self.assertEqual({found["Partition"] for found in report["after"]}, {"bigmem"})

    def test_a_throttle_applied_to_every_record_is_updated_whatever_scontrol_exits(self):
        # Finished split records answer "Job has already finished": scontrol exits 1, yet the
        # throttle lives on the meta record and every record prints its current value.
        def array(throttle):
            return lines(
                record("50", array="50", task=f"3-9%{throttle}", throttle=throttle),
                record("53", array="50", task="2", throttle=throttle, JobState="RUNNING"),
                record("52", array="50", task="1", throttle=throttle, JobState="COMPLETED"),
                record("51", array="50", task="0", throttle=throttle, JobState="COMPLETED"),
            )

        self.scontrol({
            show("50"): [{"out": array("2")}, {"out": array("4")}],
            "update JobId=50 ArrayTaskThrottle=4": {
                "out": "50_3-9: job_submit: throttle noted\n",
                "err": "50_0-1: Job has already finished\n",
                "rc": 1,
            },
        })
        code, report = self.update("--throttle", "4", job="50")
        self.assertEqual((code, report["action"]), (0, "updated"), report)
        self.assertEqual(
            report["slurm_messages"],
            "50_0-1: Job has already finished\n50_3-9: job_submit: throttle noted",
        )
        self.assertEqual({found["ArrayTaskThrottle"] for found in report["after"]}, {"4"})
        self.assertEqual(
            [found["state"] for found in report["before"]],
            ["PENDING", "RUNNING", "COMPLETED", "COMPLETED"],
        )

    def test_throttle_limits_of_a_borrowed_array_only_go_down(self):
        btrippe = {"array": "50", "task": "0-9", "Partition": "btrippe"}
        for now, given, outcome in (("2", "1", "updated"), (None, "4", "updated")):
            with self.subTest(now=now, given=given):
                self.fresh()
                after = record("50", **btrippe, throttle=given)
                self.scontrol({
                    show("50"): [
                        {"out": lines(record("50", **btrippe, throttle=now))},
                        {"out": lines(after)},
                    ],
                    f"update JobId=50 ArrayTaskThrottle={given}": {},
                })
                code, report = self.update("--throttle", given, job="50")
                self.assertEqual((code, report["action"]), (0, outcome), report)
        self.fresh()
        self.scontrol({show("50"): {"out": lines(record("50", **btrippe, throttle="2"))}})
        self.refused("borrowed", "--throttle", "4", job="50")

    def test_a_request_already_shown_is_unchanged_without_dispatch(self):
        array = {"array": "50", "task": "0-9%2", "throttle": "2"}
        features = {"Features": "CPU_GEN:SKX", "Prefer": "CPU_GEN:CLX"}
        cases = (
            ("44", record(), ("--time", "2h"), {"TimeLimit": "120"}),
            (
                "44",
                record(Partition="gpu,bioe"),
                ("--partition", "bioe,gpu"),
                {"Partition": "bioe,gpu"},
            ),
            (
                "44",
                record(**features),
                ("--constraint", "CPU_GEN:SKX"),
                {"Features": "CPU_GEN:SKX"},
            ),
            (
                "44",
                record(**features),
                ("--constraint", "CPU_GEN:SKX", "--prefer", "CPU_GEN:CLX"),
                features,
            ),
            ("50", record("50", **array), ("--throttle", "2"), {"ArrayTaskThrottle": "2"}),
        )
        for job, found, argv, requested in cases:
            with self.subTest(argv=argv):
                self.fresh()
                self.scontrol({
                    show(job): {"out": lines(found)},
                    "show partition gpu --oneliner": {"out": partition("gpu")},
                    "show partition bioe --oneliner": {"out": partition("bioe")},
                })
                code, report = self.update(*argv, job=job)
                self.assertEqual((code, report["action"]), (0, "unchanged"), report)
                self.assertEqual(
                    (report["requested"], report["after"], report["slurm_messages"]),
                    (requested, [], None),
                )
                self.assertEqual(len(report["before"]), 1)
                self.assertEqual(self.dispatches(), [])

    def test_every_refusal_reason_comes_before_any_dispatch(self):
        uid = os.getuid()
        failed = {"err": "slurm_load_jobs error: Invalid job id specified\n", "rc": 1}
        hetjob = record().replace("JobId=44 ", "JobId=44 HetJobId=44 HetJobOffset=0 ", 1)
        owners = {"Partition": "owners", "Requeue": "1"}
        cases = (
            ("ambiguous", {"out": ""}, ("--time", "1h")),
            ("ambiguous", {"out": "", "err": "scontrol: warning: a note\n"}, ("--time", "1h")),
            ("ambiguous", {"out": lines(hetjob)}, ("--time", "1h")),
            (
                "ambiguous",
                {"out": lines(record(array="44", task="0-3", throttle="x"))},
                ("--throttle", "2"),
            ),
            ("id", {"out": lines(record("45"))}, ("--time", "1h")),
            ("id", {"out": lines(record(), record())}, ("--time", "1h")),
            ("id", {"out": lines(record(array="44", task="1-3"), record("45"))}, ("--time", "1h")),
            ("name", {"out": lines(record(JobName="shk.demo.other.0123456789"))}, ("--time", "1h")),
            ("name", {"out": lines(record(JobName=NAME + "x"))}, ("--time", "1h")),
            ("uid", {"out": lines(record(UserId=f"demo({uid + 1})"))}, ("--time", "1h")),
            ("token", {"out": lines(record())}, ("--time", "1h", "--expect-token", "tok-2")),
            (
                "token",
                {"out": lines(record(Comment=None))},
                ("--time", "1h", "--expect-token", "tok-1"),
            ),
            ("state", {"out": lines(record(JobState="RUNNING"))}, ("--time", "1h")),
            ("state", {"out": lines(record(JobState="RUNNING"))}, ("--partition", "bigmem")),
            ("state", {"out": lines(record(JobState="RUNNING"))}, ("--prefer", "CPU_GEN:SKX")),
            (
                "state",
                {
                    "out": lines(
                        record(array="44", task="1-3"),
                        record("45", array="44", task="0", JobState="RUNNING"),
                    )
                },
                ("--time", "1h"),
            ),
            ("time", {"out": lines(record(TimeLimit="UNLIMITED"))}, ("--time", "1h")),
            ("time", {"out": lines(record(TimeLimit="Partition_Limit"))}, ("--time", "1h")),
            ("time", {"out": lines(record())}, ("--time", "3h")),
            ("timemin", {"out": lines(record(TimeMin="01:30:00"))}, ("--time", "1h")),
            ("requeue", {"out": lines(record(**owners))}, ("--partition", "normal")),
            ("preemptible", {"out": lines(record(Partition="owners"))}, ("--partition", "normal")),
            ("preemptible", {"out": lines(record(Partition="hns"))}, ("--partition", "normal")),
            (
                "gpus",
                {"out": lines(record(Partition="gpu", ReqTRES=GPU_TRES))},
                ("--partition", "normal"),
            ),
            (
                "gpus",
                {"out": lines(record(Partition="gpu", TresPerJob="gres/gpu:1"))},
                ("--partition", "bigmem"),
            ),
            ("array", {"out": lines(record())}, ("--throttle", "2")),
        )
        for why, reply, argv in cases:
            with self.subTest(why=why, argv=argv):
                self.fresh()
                self.scontrol({show(): reply})
                self.refused(why, *argv)
        self.scontrol({show(): failed})
        code, report = self.update("--time", "1h")
        self.assertEqual(
            (code, report["message"]),
            (3, "update: SHK_REFUSED query slurm_load_jobs error: Invalid job id specified"),
        )
        # A split task is addressed by its own id: an update would touch that record alone.
        self.scontrol({show("51"): {"out": lines(record("51", array="50", task="0"))}})
        self.refused("id", "--time", "1h", job="51")
        self.assertEqual(self.dispatches(), [])

    def test_each_requested_partition_is_read_and_must_fit_the_time_limit(self):
        long = {"TimeLimit": "3-00:00:00"}
        cases = (
            ("maxtime", record(**long), partition("normal"), "normal"),
            ("maxtime", record(TimeLimit="UNLIMITED"), partition("normal"), "normal"),
            (
                "partition_query",
                record(),
                {"err": "Partition normal not found\n", "rc": 1},
                "normal",
            ),
            ("partition_query", record(), "", "normal"),
        )
        for why, found, info, member in cases:
            with self.subTest(why=why, info=info):
                self.fresh()
                self.scontrol({
                    show(): {"out": lines(found)},
                    f"show partition {member} --oneliner": info
                    if isinstance(info, dict)
                    else {"out": info},
                })
                code, report = self.update("--partition", member)
                self.assertEqual((code, report["error"]), (3, "remote_refused"), report)
                self.assertTrue(report["message"].startswith(f"update: SHK_REFUSED {why}"))
                self.assertEqual(self.dispatches(), [])
        self.fresh()
        self.scontrol({
            show(): [
                {"out": lines(record(**long))},
                {"out": lines(record(**long, Partition="bigmem"))},
            ],
            "show partition bigmem --oneliner": {"out": partition("bigmem", "UNLIMITED")},
            "update JobId=44 Partition=bigmem": {},
        })
        self.assertEqual(self.update("--partition", "bigmem")[1]["action"], "updated")

    def test_a_failed_update_that_left_the_records_unchanged_is_unknown(self):
        self.scontrol({
            show(): {"out": lines(record())},
            "update JobId=44 TimeLimit=60": {
                "err": "Access/permission denied for job 44\n",
                "rc": 1,
            },
        })
        code, report = self.update("--time", "1h")
        self.assertEqual(code, 5)
        self.assertEqual(
            self.unknown(report),
            "update outcome not confirmed: scontrol update exit 1: Access/permission denied for "
            "job 44; records 44 do not show TimeLimit=60",
        )
        self.assertEqual(len(self.dispatches()), 1)

    def test_a_partial_reread_is_unknown_and_names_the_records(self):
        before = lines(record("50", array="50", task="1-3"), record("51", array="50", task="0"))
        after = lines(
            record("50", array="50", task="1-3", Partition="bigmem"),
            record("51", array="50", task="0", JobState="RUNNING"),
        )
        self.scontrol({
            show("50"): [{"out": before}, {"out": after}],
            "show partition bigmem --oneliner": {"out": partition("bigmem")},
            "update JobId=50 Partition=bigmem": {},
        })
        code, report = self.update("--partition", "bigmem", job="50")
        self.assertEqual(code, 5)
        self.assertEqual(
            self.unknown(report, job="50"),
            "update outcome not confirmed: scontrol update exit 0; records 51 do not show "
            "Partition=bigmem",
        )

    def test_a_failed_or_empty_reread_is_unknown(self):
        cases = (
            (
                {
                    "err": "slurm_load_jobs error: Socket timed out on send/recv operation\n",
                    "rc": 1,
                },
                "update outcome not confirmed: scontrol update exit 0: slurm_load_jobs error: "
                "Socket timed out on send/recv operation; the re-read failed",
            ),
            (
                {"out": ""},
                "update outcome not confirmed: scontrol update exit 0; the re-read found no record",
            ),
        )
        for reread, detail in cases:
            with self.subTest(reread=reread):
                self.fresh()
                self.scontrol({
                    show(): [{"out": lines(record())}, reread],
                    "update JobId=44 TimeLimit=60": {},
                })
                code, report = self.update("--time", "1h")
                self.assertEqual((code, self.unknown(report)), (5, detail))
                self.assertEqual(len(self.dispatches()), 1)

    def test_slurm_text_cut_mid_line_never_hides_the_first_reread_record(self):
        # The array's meta record comes first in the re-read and is the one that did not move.
        err = "50_1-3: Access/permission denied (" + "x" * 2100 + ")\n"
        self.scontrol({
            show("50"): [
                {
                    "out": lines(
                        record("50", array="50", task="1-3"), record("51", array="50", task="0")
                    )
                },
                {
                    "out": lines(
                        record("50", array="50", task="1-3"),
                        record("51", array="50", task="0", Partition="bigmem"),
                    )
                },
            ],
            "show partition bigmem --oneliner": {"out": partition("bigmem")},
            "update JobId=50 Partition=bigmem": {"err": err, "rc": 1},
        })
        code, report = self.update("--partition", "bigmem", job="50")
        self.assertEqual(code, 5)
        self.assertEqual(
            self.unknown(report, job="50"),
            f"update outcome not confirmed: scontrol update exit 1: {err[:1999]}; records 50 "
            "do not show Partition=bigmem",
        )

    def test_long_slurm_text_is_cut_and_the_reread_still_confirms(self):
        # 68-byte lines: the cut falls inside a line, whichever stream carries the text.
        notes = "".join(f"job_submit: note {i:02d} {'y' * 47}\n" for i in range(40))
        for stream in ("err", "out"):
            with self.subTest(stream=stream):
                self.fresh()
                self.scontrol({
                    show(): [
                        {"out": lines(record())},
                        {"out": lines(record(TimeLimit="01:00:00"))},
                    ],
                    "update JobId=44 TimeLimit=60": {stream: notes},
                })
                code, report = self.update("--time", "1h")
                self.assertEqual((code, report["action"]), (0, "updated"), report)
                self.assertEqual(
                    (report["slurm_messages"], [found["TimeLimit"] for found in report["after"]]),
                    (notes[:1999], ["01:00:00"]),
                )

    def test_free_text_never_shadows_a_field_or_the_array_identity(self):
        # Command, SubmitLine, WorkDir and Comment come after the fixed fields; the name is
        # pinned between the leading fields and UserId.
        name = "train Partition=owners Requeue=1"
        noise = "rerun of ArrayJobId=1 ArrayTaskThrottle=3 Partition=owners Requeue=1 "
        noise += "Prefer=CPU_GEN:SKX"
        found = record(
            JobName=name,
            WorkDir="/home/demo/a TimeLimit=9-00:00:00 Features=CPU_GEN:SKX",
            Comment=noise,
        )
        cases = (
            (("--partition", "bigmem"), "Partition=bigmem"),
            (("--prefer", "CPU_GEN:SKX"), "Prefer=CPU_GEN:SKX"),  # no Prefer is set yet
            (("--time", "1h"), "TimeLimit=60"),
        )
        for argv, element in cases:
            with self.subTest(argv=argv):
                self.fresh()
                self.scontrol({
                    show(): {"out": lines(found)},
                    "show partition bigmem --oneliner": {"out": partition("bigmem")},
                    f"update JobId=44 {element}": {},
                })
                code, report = self.update(*argv, name=name)
                self.assertEqual(code, 5, report)  # dispatched, and the re-read shows no change
                self.assertEqual(self.dispatches(), [["update", "JobId=44", element]])
        self.fresh()
        self.scontrol({show(): {"out": lines(found)}})
        self.refused("array", "--throttle", "3", name=name)
        # The records in the reply carry the real fields.
        self.fresh()
        self.scontrol({
            show(): [
                {"out": lines(found)},
                {"out": lines(found.replace("Partition=normal", "Partition=bigmem"))},
            ],
            "show partition bigmem --oneliner": {"out": partition("bigmem")},
            "update JobId=44 Partition=bigmem": {},
        })
        code, report = self.update("--partition", "bigmem", name=name)
        self.assertEqual((code, report["action"]), (0, "updated"), report)
        self.assertEqual(
            (report["before"][0]["Partition"], report["after"][0]["Partition"]),
            ("normal", "bigmem"),
        )

    def test_the_slurm_environment_never_reaches_scontrol(self):
        self.knobs(SLURM_JOB_ID="7", SQUEUE_FORMAT="%i", SLURM_CONF="/etc/slurm/slurm.conf")
        probe = self.tools / "scontrol-env"
        self.scontrol(
            {
                show(): [{"out": lines(record())}, {"out": lines(record(TimeLimit="01:00:00"))}],
                "update JobId=44 TimeLimit=60": {},
            },
            tool=probe.name,
        )
        wrapped = self.tools / "scontrol"
        wrapped.write_text(
            f'#!/bin/sh\nenv | grep -E "^(SLURM|SQUEUE)_" >>"{self.tmp}/env"\nexec "{probe}" "$@"\n'
        )
        wrapped.chmod(0o755)
        self.assertEqual(self.update("--time", "1h")[0], 0)
        self.assertEqual(
            set((self.tmp / "env").read_text().splitlines()), {"SLURM_CONF=/etc/slurm/slurm.conf"}
        )


class UpdateDash(UpdateCases, UpdateFlow):
    """The remote programs run under /bin/sh (dash here)."""


class UpdateBashAsSh(UpdateCases, UpdateFlow):
    """Sherlock's sh is bash in POSIX mode: the same checks must hold there."""

    def setUp(self):
        super().setUp()
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("needs bash")
        shim = self.tmp / "bash-as-sh"
        shim.mkdir()
        (shim / "sh").symlink_to(bash)
        self.knobs(PATH=f"{shim}{os.pathsep}{os.environ['PATH']}")


class UpdateOffline(UpdateFlow):
    """Every offline refusal happens before any remote call."""

    def test_offline_refusals_make_no_remote_call(self):
        self.scontrol({})
        long = "A" * 1025
        cases = (
            ((), 1, "spec_invalid"),
            (("--time", "1h", "--partition", "bigmem"), 1, "spec_invalid"),
            (("--throttle", "2", "--constraint", "CPU_GEN:SKX"), 1, "spec_invalid"),
            (("--prefer", "x", "--time", "1h"), 1, "spec_invalid"),
            (("--time", "0m"), 1, "spec_invalid"),
            (("--time", "1d"), 1, "spec_invalid"),
            (("--partition", "mystery"), 3, "unknown_partition"),
            (("--partition", "dev"), 3, "batch_not_allowed"),
            (("--partition", "normal,owners"), 3, "partitions_incompatible"),
            (("--partition", "normal,btrippe"), 3, "partitions_incompatible"),
            (("--partition", "normal,normal"), 1, "spec_invalid"),
            (("--partition", "btrippe"), 3, "borrowed_not_allowed"),
            (("--partition", "possu"), 3, "borrowed_not_allowed"),
            (("--constraint", ""), 1, "spec_invalid"),
            (("--constraint", " "), 1, "spec_invalid"),
            (("--constraint", "CPU_GEN:SKX CPU_GEN:CLX"), 1, "spec_invalid"),
            (("--constraint", "CPU_GEN:SKX,IB:HDR"), 1, "spec_invalid"),
            (("--constraint", "a Partition=gpu"), 1, "spec_invalid"),
            (("--constraint", "(null)"), 1, "spec_invalid"),  # how Slurm prints an unset value
            (("--prefer", "(null)"), 1, "spec_invalid"),
            (("--prefer", "CPU_GEN=SKX"), 1, "spec_invalid"),
            (("--prefer", long), 1, "spec_invalid"),
            (("--prefer", "é"), 1, "spec_invalid"),
            (("--throttle", "0"), 1, "spec_invalid"),
            (("--throttle", "1001"), 1, "spec_invalid"),
            (("--throttle", "05"), 1, "spec_invalid"),
            (("--throttle", "-1"), 1, "spec_invalid"),
            (("--throttle", "x"), 1, "spec_invalid"),
            (("--time", "1h", "--expect-token", "bad|token"), 1, "spec_invalid"),
        )
        for argv, code, error in cases:
            with self.subTest(argv=argv):
                found, report = self.update(*argv)
                self.assertEqual((found, report["error"]), (code, error), report)
        self.assertEqual(self.update("--time", "1h", name="")[1]["error"], "spec_invalid")
        for job in ("44_1", "4x", "0", "", "44,45", "44_[1-3]"):
            with self.subTest(job=job):
                self.assertEqual(self.update("--time", "1h", job=job)[1]["error"], "spec_invalid")
        self.assertEqual(self.calls(), [])
        report = self.update("--partition", "btrippe")[1]
        self.assertEqual(
            report["hint"], "cancel, run shk occupancy, then resubmit with --courtesy-ack"
        )
        report = self.update("--time", "1h", "--partition", "bigmem")[1]
        self.assertIn("one shk update per field", report["hint"])
        # 1024 bytes pass the offline check; the fake then answers no read.
        code, report = self.update("--constraint", "x" * 1024)
        self.assertEqual((code, report["error"]), (3, "remote_refused"))


class UpdateReplies(UpdateFlow):
    """The arguments update.sh gets and the replies the fake Slurm cannot produce."""

    def setUp(self):
        super().setUp()
        self.replies, self.argv = [], []

        def run_program(_executor, name, args=(), **_kwargs):
            self.argv.append([name, *args])
            status, stdout = self.replies.pop(0)
            return executor.Result(status, stdout)

        patcher = patch.object(executor, "run_program", run_program)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_field_reaches_the_program_as_its_arguments(self):
        borrowed = "btrippe,possu"
        cases = (
            (("--time", "90m"), ["time", "90"]),
            (("--partition", "gpu,bioe"), ["partition", "gpu,bioe", "0", "1", SAME_CLASS]),
            (("--partition", "owners"), ["partition", "owners", "1", "1", "owners"]),
            (("--partition", "bigmem"), ["partition", "bigmem", "0", "0", SAME_CLASS]),
            (("--constraint", "a|b"), ["features", "a|b", ""]),
            (("--prefer", "c&d", "--constraint", "a"), ["features", "a", "c&d"]),
            (("--throttle", "1000"), ["throttle", "1000", borrowed]),
        )
        for argv, remote in cases:
            with self.subTest(argv=argv):
                self.argv.clear()
                self.replies[:] = [("complete", "SHK_UNCHANGED\n")]
                code, report = self.update(*argv, "--expect-token", "tok-1")
                self.assertEqual((code, report["action"]), (0, "unchanged"))
                self.assertEqual(self.argv, [["update", "44", NAME, "tok-1", *remote]])
        self.argv.clear()
        self.replies[:] = [("complete", "SHK_UNCHANGED\n")]
        self.update("--time", "1h")
        self.assertEqual(self.argv, [["update", "44", NAME, "", "time", "60"]])

    def test_a_lost_or_unmarked_reply_is_unknown(self):
        before = f"SHK_BEFORE|{record()}\n"
        cases = (
            (("unknown", before), "no reply"),
            (("complete", before), "0 terminal markers"),
            (("complete", before + "SHK_UNCHANGED\nSHK_DISPATCHED 0\n"), "2 terminal markers"),
        )
        for reply, detail in cases:
            with self.subTest(reply=reply):
                self.replies[:] = [reply]
                code, report = self.update("--time", "1h")
                self.assertEqual((code, self.unknown(report)), (5, detail))
        self.replies[:] = [("complete", before + "SHK_REFUSED state\n")]
        code, report = self.update("--time", "1h")
        self.assertEqual(
            (code, report["error"], report["message"]),
            (3, "remote_refused", "update: SHK_REFUSED state"),
        )


class UpdateOverSsh(UpdateFlow):
    executor = "ssh"

    def test_feature_expressions_survive_the_ssh_command_line(self):
        self.knobs(**install_fake_ssh(self.tmp).env())
        allowed, preferred = "(CPU_GEN:SKX|CPU_GEN:CLX)&IB:HDR", "[GPU_SKU:A100*2|GPU_SKU:H100]"
        after = record(Features=allowed, Prefer=preferred)
        self.scontrol({
            show(): [{"out": lines(record())}, {"out": lines(after)}],
            f"update JobId=44 Features={allowed} Prefer={preferred}": {},
        })
        code, report = self.update("--constraint", allowed, "--prefer", preferred)
        self.assertEqual((code, report["action"], report["executor"]), (0, "updated", "ssh"))
        self.assertEqual(
            self.dispatches(),
            [["update", "JobId=44", f"Features={allowed}", f"Prefer={preferred}"]],
        )
