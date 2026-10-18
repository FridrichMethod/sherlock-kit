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


class NeverAllocatedTests(DetailsFlow):
    """D4, G4: jobs cancelled while held or pending never had an allocation."""

    def cancelled_while_held(self):
        self.canned(
            queue=[
                queue("7300", "7300", "CANCELLED", name="held-single"),
                queue("7301", "7301", "CANCELLED", name="held-array"),
            ],
            accounting=[
                account("7300", "7300", f"CANCELLED by {UID}", name="held-single"),
                account("7301_[0-1]", "7301", f"CANCELLED by {UID}", name="held-array"),
            ],
            jobs=[
                record("7300", name="held-single", state="CANCELLED"),
                record(
                    "7301", name="held-array", state="CANCELLED", array="7301", task="4294967294"
                ),
            ],
        )
        return self.details("--job-id", "7300", "--job-id", "7301")

    def test_cancelled_never_allocated_jobs_are_complete_with_final_zero_cost(self):
        code, report = self.cancelled_while_held()
        self.assertEqual(code, 0)
        self.assertTrue(report["details"]["allocation_evidence_complete"])
        self.assertTrue(report["details"]["complete"])
        entries = {entry["name"]: entry for entry in report["names"]}
        for name, count in (("held-single", 1), ("held-array", 2)):
            current = entries[name]["current"]
            self.assertEqual(entries[name]["state"], "failed")
            self.assertEqual(current["tasks"]["count"], count)
            self.assertEqual(current["cost"], {"cpu_seconds": 0, "gpu_seconds": 0, "final": True})

    def test_cancelled_queue_rows_carry_no_allocation_facts(self):
        records = self.cancelled_while_held()[1]["details"]["queue"]
        self.assertEqual([row["allocated_cpus"] for row in records], [None, None])
        self.assertEqual([row["started_at"] for row in records], [None, None])
        self.assertEqual([row["ended_at"] for row in records], ["2026-10-10T12:02:26Z"] * 2)


class FailedReadTests(DetailsFlow):
    """D5: a failed squeue or sacct never yields summary states."""

    def test_failed_accounting_read_is_exit_1_with_evidence(self):
        self.canned(accounting=[account("7400", "7400", "COMPLETED")])
        self.fake.update(sacct_rc=1)
        code, report = self.details("--task", "train-a")
        self.assertEqual((code, report["ok"], report["error"]), (1, False, "malformed_read"))
        self.assertNotIn("names", report)
        self.assertFalse(report["details"]["complete"])
        self.assertEqual(report["details"]["returncodes"], {"squeue": 0, "sacct": 1, "scontrol": 0})

    def test_failed_queue_read_is_exit_1_with_accounting_evidence(self):
        self.canned(accounting=[account("7400", "7400", "COMPLETED")])
        self.fake.update(queue_rc=1)
        code, report = self.details("--job-id", "7400")
        self.assertEqual((code, report["error"]), (1, "malformed_read"))
        self.assertEqual([row["job_id"] for row in report["details"]["accounting"]], ["7400"])


class FreeTextTests(DetailsFlow):
    """D13: "|" in a comment or job name is data, not a field separator."""

    def test_pipe_in_another_jobs_comment_does_not_poison_names(self):
        self.canned(
            queue=[
                queue("7800", "7800", "RUNNING", nodes="sh01-01n01"),
                queue("7801", "7801", "RUNNING", nodes="sh01-01n02", name="sweep", comment="a|b"),
            ],
            accounting=[
                account("7800", "7800", "RUNNING"),
                account("7801", "7801", "RUNNING", name="sweep"),
            ],
            jobs=[record("7800", state="RUNNING")],
        )
        code, report = self.details("--all")
        self.assertEqual((code, report["details"]["malformed"]), (0, []))
        self.assertTrue(report["details"]["complete"])
        self.assertEqual(report["names"][0]["current"]["anomalies"], [])

    def test_pipe_in_a_legacy_job_name_is_kept(self):
        self.canned(
            queue=[
                queue(
                    "7810",
                    "7810",
                    "RUNNING",
                    nodes="sh01-01n01",
                    name="seed=1|lr=0.1",
                    comment="x|y",
                )
            ],
            accounting=[account("7810", "7810", "RUNNING", name="seed=1|lr=0.1")],
            jobs=[record("7810", state="RUNNING", name="seed=1|lr=0.1", comment="x|y")],
        )
        code, report = self.details("--job-id", "7810")
        self.assertEqual((code, report["names"][0]["name"]), (0, "seed=1|lr=0.1"))
        self.assertEqual(report["details"]["queue"][0]["comment"], "x|y")
        self.assertTrue(report["details"]["complete"])


class JobIdSummaryTests(DetailsFlow):
    """D6, D28: --job-id summarizes each base job; shk name invariants apply to shk names."""

    def test_legacy_jobs_sharing_a_script_name_are_separate_entries(self):
        self.canned(
            queue=[
                queue("7500", "7500", "RUNNING", nodes="sh01-01n01", name="train.sh"),
                queue("7501", "7501", "PENDING", name="train.sh", partition="normal,owners"),
                queue(
                    "7502", "7502", "RUNNING", nodes="sh01-01n02", name="lab.sh", partition="mylab"
                ),
            ],
            jobs=[
                record("7500", name="train.sh", state="RUNNING"),
                record("7501", name="train.sh"),
                record("7502", name="lab.sh", state="RUNNING"),
            ],
        )
        code, report = self.details("--job-id", "7500", "--job-id", "7501", "--job-id", "7502")
        self.assertEqual(code, 0)
        found = [
            (e["name"], e["current"]["job_id"], e["state"], e["history"]) for e in report["names"]
        ]
        self.assertEqual(
            found,
            [
                ("train.sh", "7500", "live", []),
                ("train.sh", "7501", "live", []),
                ("lab.sh", "7502", "live", []),
            ],
        )
        self.assertEqual([e["current"]["anomalies"] for e in report["names"]], [[], [], []])

    def test_shk_names_keep_their_invariants_in_job_id_mode(self):
        self.canned(
            queue=[
                queue("7510", "7510", "RUNNING", nodes="sh01-01n01"),
                queue("7511", "7511", "PENDING"),
            ],
            jobs=[record("7510", state="RUNNING"), record("7511")],
        )
        code, report = self.details("--job-id", "7510", "--job-id", "7511")
        self.assertEqual(code, 6)
        anomalies = [entry["current"]["anomalies"] for entry in report["names"]]
        self.assertEqual(anomalies, [["duplicate_live"], ["duplicate_live"]])

    def test_preemption_on_a_non_preemptible_partition_exits_6(self):
        self.canned(
            accounting=[
                account("7520", "7520", "PREEMPTED", name="legacy", db="9001"),
                account("7520", "7520", "COMPLETED", name="legacy", db="9002", restarts="1"),
            ]
        )
        code, report = self.details("--job-id", "7520")
        self.assertEqual(
            (code, report["names"][0]["current"]["anomalies"]), (6, ["unexpected_preemption"])
        )

    def test_shk_name_on_an_unknown_partition_exits_6(self):
        self.canned(accounting=[account("7530", "7530", "COMPLETED", partition="mystery")])
        code, report = self.details("--task", "train-a")
        self.assertEqual(
            (code, report["names"][0]["current"]["anomalies"]), (6, ["unknown_partition"])
        )


class ArrayCountTests(DetailsFlow):
    """D7: legacy arrays need not be 0-based or contiguous."""

    def test_array_without_task_zero_or_known_count_is_not_failed(self):
        rows = [
            account(f"7600_{i}", str(7600 + i), "COMPLETED", name="array.sbatch") for i in (1, 2, 3)
        ]
        self.canned(accounting=rows)
        code, report = self.details("--job-id", "7600")
        self.assertEqual(code, 0)
        current = report["names"][0]["current"]
        self.assertEqual(report["names"][0]["state"], "done")
        self.assertEqual((current["tasks"]["count"], current["tasks"]["missing"]), (None, None))
        self.assertEqual((current["array"]["count"], current["tasks"]["completed"]), (None, 3))
        self.assertFalse(current["cost"]["final"])
        self.assertTrue(report["details"]["complete"])
