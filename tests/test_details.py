"""Detailed status against canned Slurm 25.11 text in the formats observed on Sherlock."""

import os
import sys

from fakes import SRC
from fakes.expired_slurm import CannedSlurm
from test_flows import Flow

sys.path.insert(0, str(SRC))
from sherlock_kit import spec

UID = os.getuid()
NAME = spec.job_name("demo", "default", "train-a")
SUBMIT = "2026-10-10T12:00:00"


def queue(job, physical, state, *, name=NAME, partition="normal", nodes="", comment="tok"):
    """One details.sh squeue row; pending rows print N/A start and end like Slurm."""
    if state == "PENDING":
        elapsed, start, end = "0:00", "N/A", "N/A"
    elif nodes:
        elapsed, start, end = "1:00", "2026-10-10T12:00:01", "2026-10-10T12:20:01"
    else:  # cancelled before any allocation: %S and %e are the cancel time, %N is empty
        elapsed, start, end = "0:00", "2026-10-10T12:02:26", "2026-10-10T12:02:26"
    return [
        job,
        physical,
        state,
        partition,
        elapsed,
        "1",
        SUBMIT,
        start,
        end,
        nodes,
        str(UID),
        comment,
        name,
    ]


def account(job, physical, state, *, name=NAME, partition="normal", db=None, restarts="0"):
    """One details.sh sacct row; Sherlock stores no comments in accounting."""
    started = state != "PENDING" and not state.startswith("CANCELLED by")
    tres = "billing=1,cpu=1,mem=2G,node=1" if started else ""
    elapsed, cpus = ("60", "1") if started else ("0", "0")
    start = "2026-10-10T12:00:01" if started else ("Unknown" if state == "PENDING" else "None")
    end = "Unknown" if state in ("PENDING", "RUNNING") else "2026-10-10T12:01:01"
    nodes = "sh01-01n01" if started else "None assigned"
    db = db or f"90000000{physical}"
    return [
        *(job, physical, "sherlock", db, state, partition, elapsed, cpus, tres, SUBMIT, start),
        *(end, restarts, "0:0", nodes, str(UID), "", name),
    ]


def record(job, *, name=NAME, state="PENDING", array=None, task=None, comment="tok"):
    """One scontrol show job --oneliner record (fields in Slurm 25.11 order, abridged)."""
    element = f" ArrayJobId={array} ArrayTaskId={task}" if array else ""
    return (
        f"JobId={job}{element} JobName={name} UserId=user({UID}) GroupId=group(5000) "
        f"MCS_label=N/A Priority=0 Nice=0 Account=group QOS=normal JobState={state} "
        f"Reason=None Dependency=(null) Requeue=0 Restarts=0 BatchFlag=1 Reboot=0 "
        f"ExitCode=0:0 RunTime=00:00:00 TimeLimit=00:10:00 TimeMin=N/A "
        f"SubmitTime=2026-10-10T12:00:00 EligibleTime=Unknown StartTime=Unknown "
        f"EndTime=Unknown Partition=normal NodeList= NumNodes=1 NumCPUs=1 NumTasks=1 "
        f"CPUs/Task=1 ReqTRES=cpu=1,mem=2G,node=1,billing=1 AllocTRES=(null) "
        f"Features=(null) Comment={comment} StdIn=/dev/null"
    )


class DetailsFlow(Flow):
    def canned(self, queue=(), accounting=(), jobs=()):
        self.fake = CannedSlurm(self.tmp, queue=queue, accounting=accounting, jobs=jobs)
        self.knobs(**self.fake.env())

    def details(self, *argv):
        return self.shk("status", *argv, "--details")

    def scontrol_ids(self):
        return [call["argv"][2] for call in self.fake.calls("scontrol")]


class ControlLookupTests(DetailsFlow):
    """D2, D3, D52: one scontrol call per base job id; records outside the selection ignored."""

    def test_one_call_per_array_returns_each_element_once(self):
        self.canned(
            queue=[
                queue("7100_0", "7101", "RUNNING", nodes="sh01-01n01"),
                queue("7100_1", "7102", "RUNNING", nodes="sh01-01n01"),
                queue("7100_[2-3]", "7100", "PENDING"),
            ],
            accounting=[
                account("7100_0", "7101", "RUNNING"),
                account("7100_1", "7102", "RUNNING"),
                account("7100_[2-3]", "7100", "PENDING"),
            ],
            jobs=[
                record("7100", array="7100", task="2-3"),
                record("7101", array="7100", task="0", state="RUNNING"),
                record("7102", array="7100", task="1", state="RUNNING"),
            ],
        )
        code, report = self.details("--task", "train-a")
        self.assertEqual(code, 0)
        self.assertEqual(self.scontrol_ids(), ["7100"])
        self.assertEqual(
            [job["job_id"] for job in report["details"]["jobs"]], ["7100", "7101", "7102"]
        )
        self.assertTrue(report["details"]["complete"])

    def test_meta_id_selection_lists_split_elements_once(self):
        self.canned(
            queue=[
                queue("7100_0", "7101", "RUNNING", nodes="sh01-01n01"),
                queue("7100_1", "7100", "RUNNING", nodes="sh01-01n01"),
            ],
            accounting=[account("7100_0", "7101", "RUNNING"), account("7100_1", "7100", "RUNNING")],
            jobs=[
                record("7100", array="7100", task="1", state="RUNNING"),
                record("7101", array="7100", task="0", state="RUNNING"),
            ],
        )
        code, report = self.details("--job-id", "7100")
        self.assertEqual(code, 0)
        self.assertEqual(self.scontrol_ids(), ["7100"])
        self.assertEqual(
            sorted(job["job_id"] for job in report["details"]["jobs"]), ["7100", "7101"]
        )
        self.assertTrue(report["details"]["complete"])

    def test_element_started_after_the_queue_read_is_ignored(self):
        self.canned(
            queue=[queue("7100_0", "7100", "PENDING"), queue("7100_1", "7100", "PENDING")],
            accounting=[account("7100_[0-1]", "7100", "PENDING")],
            jobs=[
                record("7100", array="7100", task="1"),
                record("7101", array="7100", task="0", state="RUNNING"),
            ],
        )
        report = self.details("--task", "train-a")[1]["details"]
        self.assertEqual([job["job_id"] for job in report["jobs"]], ["7100"])
        self.assertTrue(report["complete"])

    def test_all_mode_reads_control_records_only_for_listed_names(self):
        other = spec.job_name("other", "default", "x")
        self.canned(
            queue=[
                queue("7200", "7200", "RUNNING", nodes="sh01-01n01"),
                queue("7201", "7201", "RUNNING", nodes="sh01-01n02", name="sh_dev"),
                queue("7202", "7202", "RUNNING", nodes="sh01-01n03", name=other),
            ],
            accounting=[
                account("7200", "7200", "RUNNING"),
                account("7201", "7201", "RUNNING", name="sh_dev"),
                account("7202", "7202", "RUNNING", name=other),
            ],
            jobs=[
                record("7200", state="RUNNING"),
                record("7201", state="RUNNING", name="sh_dev"),
                record("7202", state="RUNNING", name=other),
            ],
        )
        code, report = self.details("--all")
        self.assertEqual(code, 0)
        self.assertEqual(self.scontrol_ids(), ["7200"])
        self.assertEqual([entry["name"] for entry in report["names"]], [NAME])
        self.assertTrue(report["details"]["complete"])
        squeue = self.fake.calls("squeue")[0]["argv"]
        self.assertFalse([arg for arg in squeue if arg.startswith("--name")])

    def test_names_mode_selects_names_in_slurm(self):
        self.canned(
            queue=[queue("7200", "7200", "RUNNING", nodes="sh01-01n01")],
            accounting=[account("7200", "7200", "RUNNING")],
            jobs=[record("7200", state="RUNNING")],
        )
        code, report = self.details("--name", NAME)
        self.assertEqual((code, report["names"][0]["state"]), (0, "live"))
        for tool in ("squeue", "sacct"):
            self.assertIn(f"--name={NAME}", self.fake.calls(tool)[0]["argv"])
        self.assertIn("-S", self.fake.calls("sacct")[0]["argv"])
        self.assertIn("now-30days", self.fake.calls("sacct")[0]["argv"])


class ControlTimeTests(DetailsFlow):
    """D34: scontrol prints site-local times unless told otherwise."""

    def test_control_times_are_utc_like_queue_and_accounting(self):
        self.canned(
            queue=[queue("7200", "7200", "PENDING")],
            accounting=[account("7200", "7200", "PENDING")],
            jobs=[record("7200")],
        )
        self.details("--job-id", "7200")
        for tool in ("squeue", "sacct", "scontrol"):
            env = self.fake.calls(tool)[0]["env"]
            self.assertEqual((env.get("TZ"), env.get("SLURM_TIME_FORMAT")), ("UTC", "standard"))
