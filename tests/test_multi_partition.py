"""Submission to a set of compatible partitions (--partition P,P) and the status of such jobs."""

import io
import json
import os
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

from fakes import SRC
from fakes.expired_slurm import CannedSlurm
from test_details import UID, DetailsFlow, account, queue, record
from test_flows import STAMP, Flow
from test_slurm import N, a, q
from test_spec import EXPORT, HEAD, NAME, SINGLE_LOGS, TAIL, Case

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, partitions, slurm, spec
from sherlock_kit.errors import ShkError

GPU_SET = "gpu,bioe,stat"
MEMBERS = ["gpu", "bioe", "stat"]
CURRENT_0_7_1 = [
    "job_id",
    "partition",
    "array",
    "tasks",
    "exit_codes",
    "max_restart",
    "cost",
    "anomalies",
]


class SetChecks(Case):
    """Offline checks of a comma-separated --partition value, in the documented order."""

    def unknown(self, name):
        with self.assertRaises(ShkError) as caught:
            partitions.profile(name)
        return caught.exception

    def test_a_compatible_set_is_accepted_with_and_without_gpus(self):
        for gpus in (0, 1):
            with self.subTest(gpus=gpus):
                built = self.build(partition=GPU_SET, gpus=gpus)
                single = self.build(partition="gpu", gpus=gpus)
                self.assertEqual((built.name, built.run_dir), (NAME, single.run_dir))
                self.assertEqual(built.argv[3], f"--partition={GPU_SET}")
                self.assertEqual(
                    built.argv,
                    [
                        f"--partition={GPU_SET}" if w == "--partition=gpu" else w
                        for w in single.argv
                    ],
                )
                resources = built.document["resources"]
                self.assertEqual(
                    (resources["partition"], list(resources["partitions"])), (GPU_SET, MEMBERS)
                )
                self.assertEqual(list(resources)[-1], "partitions")
                self.assertEqual(
                    {key: value for key, value in resources.items() if key != "partitions"},
                    {**single.document["resources"], "partition": GPU_SET},
                )
                self.assertFalse(built.profile.borrowed)
                self.assertEqual(built.profile.courtesy, "")
        gpu_argv = [
            *HEAD,
            f"--partition={GPU_SET}",
            "--cpus-per-task=2",
            "--no-requeue",
            *TAIL,
            *SINGLE_LOGS,
            "-G",
            "1",
            EXPORT,
            "--dependency=singleton",
        ]
        self.assertEqual(self.build(partition=GPU_SET, gpus=1).argv, gpu_argv)

    def test_the_set_keeps_the_order_given_and_partitions_is_the_last_resources_key(self):
        built = self.build(partition="stat,gpu", gpus=1, prefer="GPU_SKU:L40S")
        resources = built.document["resources"]
        self.assertEqual(list(resources)[-2:], ["prefer", "partitions"])
        self.assertEqual(
            (resources["partition"], list(resources["partitions"])), ("stat,gpu", ["stat", "gpu"])
        )
        self.assertIn('"partition":"stat,gpu","partitions":["stat","gpu"]', built.text)
        self.assertEqual(built.text, spec.canonical(json.loads(built.text)))

    def test_cpu_partitions_combine_by_their_profile_fields(self):
        built = self.build(partition="normal,bigmem,service")
        self.assertEqual(
            list(built.document["resources"]["partitions"]), ["normal", "bigmem", "service"]
        )
        self.assertEqual(self.build(partition="normal,gpu").document["resources"]["gpus"], 0)

    def test_an_unlisted_member_is_the_existing_unknown_partition_refusal(self):
        for value, first in (
            ("gpu,", ""),
            (",gpu", ""),
            ("gpu,,bioe", ""),
            ("gpu,mystery", "mystery"),
            ("gpu,mystery,other", "mystery"),
            ("mystery,gpu,gpu", "mystery"),  # unlisted before repeated
            ("dev,mystery", "mystery"),  # unlisted before batch
        ):
            with self.subTest(value=value):
                error = self.refused(3, "unknown_partition", partition=value)
                existing = self.unknown(first)
                self.assertEqual((error.message, error.hint), (existing.message, existing.hint))

    def test_a_repeated_member_is_spec_invalid(self):
        for value, member in (("gpu,bioe,gpu", "gpu"), ("gpu,gpu", "gpu"), ("dev,stat,dev", "dev")):
            with self.subTest(value=value):
                error = self.refused(1, "spec_invalid", partition=value)  # repeated before batch
                self.assertIn(f"names {member} twice", error.message)

    def test_a_member_without_batch_jobs_is_the_existing_batch_refusal(self):
        existing = self.refused(3, "batch_not_allowed", partition="dev")
        for value in ("gpu,dev", "dev,owners", "dev,btrippe"):  # batch before compatibility
            with self.subTest(value=value):
                error = self.refused(3, "batch_not_allowed", partition=value)
                self.assertEqual((error.message, error.hint), (existing.message, existing.hint))

    def test_borrowed_members_are_named(self):
        for value, borrowed in (
            ("gpu,btrippe", "btrippe"),
            ("possu,gpu", "possu"),
            ("btrippe,possu", "btrippe, possu"),
            ("owners,btrippe", "btrippe"),  # borrowed before the flags
        ):
            with self.subTest(value=value):
                error = self.refused(
                    3, "partitions_incompatible", partition=value, courtesy_ack=True
                )
                self.assertIn(f"include the borrowed {borrowed}:", error.message)
                self.assertNotIn("preemptible=", error.message)
                self.assertIn("submit incompatible partitions separately", error.hint)

    def test_differing_preemptible_or_requeue_lists_every_member_with_both_values(self):
        error = self.refused(3, "partitions_incompatible", partition="gpu,owners", gpus=1)
        self.assertTrue(
            error.message.endswith(
                "gpu (preemptible=no, requeue=no), owners (preemptible=yes, requeue=yes)"
            ),
            error.message,
        )
        self.assertIn("submit incompatible partitions separately", error.hint)
        error = self.refused(3, "partitions_incompatible", partition="owners,normal,bioe")
        self.assertIn(
            "owners (preemptible=yes, requeue=yes), normal (preemptible=no, requeue=no), "
            "bioe (preemptible=no, requeue=no)",
            error.message,
        )

    def test_gpus_need_every_member_and_requeue_names_the_set(self):
        error = self.refused(3, "gpus_not_allowed", partition="normal,gpu", gpus=1)
        self.assertEqual(error.message, "partition normal does not allow GPUs")
        error = self.refused(3, "gpus_not_allowed", partition="normal,gpu,bigmem", gpus=1)
        self.assertEqual(error.message, "partitions normal, bigmem do not allow GPUs")
        error = self.refused(3, "requeue_not_allowed", partition="gpu,bioe", requeue=True)
        self.assertEqual(error.message, "partition gpu,bioe does not allow requeue")
        defaulted = self.build(partition="gpu,bioe")
        self.assertIn("--no-requeue", defaulted.argv)
        self.assertFalse(defaulted.document["resources"]["requeue"])
        self.refused(1, "spec_invalid", partition="normal,gpu", gpus=-1)  # 0.7.1 check order

    def test_profile_still_refuses_a_comma_value_unchanged(self):
        self.assertEqual(self.unknown("gpu,bioe").token, "unknown_partition")


class CommandLine(Case):
    """The real submit parser: help text and a repeated flag."""

    def setUp(self):
        super().setUp()
        (self.tmp / "sherlock-kit.toml").write_text(
            f'project = "demo"\nremote_root = "{self.tmp / "remote"}"\n'
        )

    def main(self, *argv):
        ctx = cli.Context(project_dir=self.tmp, executor="local", env={}, hostname="lab")
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            try:
                code = cli.main(list(argv), modules=[slurm], context=ctx)
            except SystemExit as stop:
                code = stop.code
        return code, out.getvalue()

    def test_help_says_how_to_give_a_set(self):
        code, text = self.main("submit", "--help")
        self.assertEqual(code, 0)
        words = " ".join(text.split())
        self.assertIn("one partition, or compatible partitions as one value P,P;", words)
        self.assertIn("a repeated flag is not a list (the last one wins)", words)

    def test_a_repeated_flag_keeps_the_last_value_as_in_0_7_1(self):
        argv = ["submit", "--task", "train-a", "--script", str(self.tmp / "job.sh")]
        argv += ["--cpus", "2", "--mem", "4G", "--time", "90m", "--dry-run"]
        code, text = self.main(*argv, "--partition", "gpu", "--partition", "bioe")
        report = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(report["spec"]["resources"]["partition"], "bioe")
        self.assertNotIn("partitions", report["spec"]["resources"])
        self.assertIn("--partition=bioe", report["argv"])


class SubmitFlows(Flow):
    """Through the real submit.sh and the fake Slurm: the 0.7.1 guards, staging and exit 5."""

    def test_a_set_is_submitted_once_with_singleton_and_a_second_submit_is_live(self):
        code, out = self.submit(partition=GPU_SET)
        self.assertEqual((code, out["action"], out["name"]), (0, "submitted", NAME))
        (sbatch,) = self.slurm.calls("sbatch")
        self.assertEqual(sbatch["argv"][:-1], out["argv"][1:])
        self.assertEqual(sbatch["argv"][2], f"--partition={GPU_SET}")
        self.assertEqual(sbatch["argv"][-2], "--dependency=singleton")
        self.assertEqual(self.slurm.state()["jobs"][0]["partition"], GPU_SET)
        text = (self.shk_dir() / f"{NAME}.{out['job_id']}.json").read_text()
        frozen = json.loads(text)
        self.assertEqual(text, spec.canonical(frozen) + "\n")
        self.assertEqual(
            (frozen["resources"]["partition"], frozen["resources"]["partitions"]),
            (GPU_SET, MEMBERS),
        )
        code, refused = self.submit(partition=GPU_SET)
        self.assertEqual((code, refused["error"]), (3, "live"))
        self.assertEqual(len(self.slurm.calls("sbatch")), 1)

    def test_an_unparsed_sbatch_reply_is_exit_5_and_keeps_the_staged_files(self):
        self.knobs(FAKE_SBATCH_UNPARSED="1")
        code, out = self.submit(partition=GPU_SET)
        self.assertEqual((code, out["action"]), (5, "unknown"))
        paths = sorted(self.shk_dir().iterdir())
        self.assertEqual([path.suffix for path in paths], [".err", ".json", ".sh"])
        for path in paths:
            self.assertRegex(path.stem, rf"\A{re.escape(NAME)}\.{STAMP}\Z")
        self.assertEqual(json.loads(paths[1].read_text())["resources"]["partitions"], MEMBERS)
        entry = self.entry()  # the staged spec is not the spec of a job id: no ran_on
        self.assertEqual((entry["state"], entry["spec_files"]), ("live", 1))
        self.assertNotIn("ran_on", entry["current"])

    def test_dry_run_and_preflight_print_partitions_as_the_last_resources_key(self):
        code, out = self.submit("--dry-run", "--prefer", "GPU_SKU:L40S", partition=GPU_SET)
        self.assertEqual(code, 0)
        self.assertEqual(list(out["spec"]["resources"])[-2:], ["prefer", "partitions"])
        self.assertEqual(self.slurm.calls(), [])
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "sbatch").write_text(
            "#!/bin/sh\n"
            'for last in "$@"; do :; done\n'
            '[ "$last" = --test-only ] || exit 97\n'
            "cat >/dev/null\n"
        )
        (tools / "sbatch").chmod(0o755)
        self.knobs(PATH=f"{tools}{os.pathsep}{os.environ['PATH']}")
        code, out = self.submit("--preflight", partition=GPU_SET)
        self.assertEqual((code, out["action"], out["ok"]), (0, "preflight", True))
        self.assertEqual(list(out["spec"]["resources"])[-1], "partitions")
        self.assertEqual(out["argv"][3], f"--partition={GPU_SET}")
        self.assertFalse(self.remote.exists())

    def test_resources_and_occupancy_still_refuse_a_comma_value_unchanged(self):
        for command in ("resources", "occupancy"):
            with self.subTest(command=command):
                code, out = self.shk(command, "--partition", "gpu,bioe")
                self.assertEqual((code, out["error"]), (3, "unknown_partition"))
                self.assertIn("partition 'gpu,bioe' is not listed", out["message"])
        self.assertEqual(self.slurm.calls(), [])


class FakeSlurmPartitions(Flow):
    """The fake prints what the Slurm 25.11 source says Slurm prints for a partition list."""

    def rows(self):
        def run(*argv):
            done = subprocess.run(argv, capture_output=True, text=True, check=True)
            return done.stdout.splitlines()

        squeue = run("squeue", "-h", "-t", "all", "-o", "%i|%T|%P|%j")
        sacct = run(
            "sacct",
            "-n",
            "-P",
            "--duplicates",
            "-S",
            "now-30days",
            "-E",
            "now",
            "--format=JobID,State,Partition,AllocCPUS,Restarts,JobName",
        )
        return squeue, sacct

    def test_pending_then_started_on_one_member(self):
        job = self.slurm.add("multi", "PENDING", partition=GPU_SET)
        self.assertEqual(
            self.rows(),
            ([f"{job}|PENDING|{GPU_SET}|multi"], [f"{job}|PENDING|{GPU_SET}|0|0|multi"]),
        )
        self.slurm.set(job, "RUNNING", partition="bioe")
        self.assertEqual(
            self.rows(), ([f"{job}|RUNNING|bioe|multi"], [f"{job}|RUNNING|bioe|1|0|multi"])
        )
        self.slurm.set(job, "COMPLETED")
        self.assertEqual(
            self.rows(), ([f"{job}|COMPLETED|bioe|multi"], [f"{job}|COMPLETED|bioe|1|0|multi"])
        )

    def test_a_start_without_a_named_member_runs_on_the_first_listed(self):
        job = self.slurm.add("multi", "RUNNING", partition="stat,gpu")
        self.assertEqual(
            self.rows(), ([f"{job}|RUNNING|stat|multi"], [f"{job}|RUNNING|stat|1|0|multi"])
        )

    def test_cancelled_while_pending_is_one_member_in_squeue_and_the_list_in_sacct(self):
        job = self.slurm.add("multi", "PENDING", partition=GPU_SET)
        subprocess.run(["scancel", "--name=multi"], check=True)
        self.assertEqual(
            self.rows(),
            ([f"{job}|CANCELLED|gpu|multi"], [f"{job}|CANCELLED|{GPU_SET}|0|0|multi"]),
        )

    def test_a_requeue_holds_the_list_again_and_the_earlier_attempt_its_run_partition(self):
        job = self.slurm.add("multi", "PENDING", partition="gpu,bioe")
        self.slurm.set(job, "RUNNING", partition="bioe")
        self.slurm.requeue(job)
        self.assertEqual(
            self.rows(),
            (
                [f"{job}|PENDING|gpu,bioe|multi"],
                [f"{job}|PREEMPTED|bioe|1|0|multi", f"{job}|PENDING|gpu,bioe|0|1|multi"],
            ),
        )

    def test_array_tasks_start_on_different_members_and_the_remainder_keeps_the_list(self):
        job = self.slurm.add("multi", "PENDING", "PENDING", "PENDING", partition="gpu,bioe")
        self.slurm.set(job, "RUNNING", partition="gpu")
        self.slurm.set(job, None, "RUNNING", partition="bioe")
        squeue, sacct = self.rows()
        self.assertEqual(
            squeue,
            [
                f"{job}_0|RUNNING|gpu|multi",
                f"{job}_1|RUNNING|bioe|multi",
                f"{job}_[2]|PENDING|gpu,bioe|multi",
            ],
        )
        self.assertEqual(
            sacct, [f"{job}_0|RUNNING|gpu|1|0|multi", f"{job}_1|RUNNING|bioe|1|0|multi"]
        )

    def test_a_single_partition_prints_as_before_with_alloc_cpus_0_until_a_start(self):
        job = self.slurm.add("plain", "PENDING")
        self.assertEqual(
            self.rows(), ([f"{job}|PENDING|normal|plain"], [f"{job}|PENDING|normal|0|0|plain"])
        )
        self.slurm.set(job, "RUNNING")
        self.assertEqual(
            self.rows(), ([f"{job}|RUNNING|normal|plain"], [f"{job}|RUNNING|normal|1|0|plain"])
        )


class StatusByTask(Flow):
    """status --task reads the frozen spec, so a set adds current.ran_on after partition."""

    def submit_set(self, *extra, partition=GPU_SET):
        code, out = self.submit(*extra, partition=partition)
        self.assertEqual((code, out["action"]), (0, "submitted"), out)
        return int(out["job_id"])

    def current(self, code=0):
        return self.entry("train-a", code)["current"]

    def test_pending_shows_the_list_and_an_empty_ran_on_directly_after_partition(self):
        self.submit_set()
        current = self.current()
        self.assertEqual(list(current), [*CURRENT_0_7_1[:2], "ran_on", *CURRENT_0_7_1[2:]])
        self.assertEqual(
            (current["partition"], current["ran_on"], current["anomalies"]), (GPU_SET, [], [])
        )

    def test_running_and_finished_on_one_member(self):
        job = self.submit_set()
        self.slurm.set(job, "RUNNING", partition="bioe")
        current = self.current()
        self.assertEqual((current["partition"], current["ran_on"]), ("bioe", ["bioe"]))
        self.slurm.set(job, "COMPLETED")
        entry = self.entry()
        self.assertEqual(entry["state"], "done")
        self.assertEqual(
            (entry["current"]["partition"], entry["current"]["ran_on"]), ("bioe", ["bioe"])
        )
        self.assertEqual(
            entry["current"]["cost"], {"cpu_seconds": 60, "gpu_seconds": 0, "final": True}
        )

    def test_cancelled_while_pending_has_not_run_whatever_squeue_names(self):
        self.submit_set()
        self.assertEqual(self.shk("cancel", "--task", "train-a")[1]["action"], "cancelled")
        entry = self.entry()
        current = entry["current"]
        self.assertEqual(entry["state"], "failed")
        self.assertEqual((current["partition"], current["ran_on"]), ("gpu", []))
        self.assertEqual(current["anomalies"], [])
        self.assertEqual(current["cost"], {"cpu_seconds": 0, "gpu_seconds": 0, "final": True})

    def test_a_pending_row_with_one_member_left_has_not_run(self):
        job = self.submit_set()
        self.slurm.relist(job, "gpu")
        current = self.current()
        self.assertEqual((current["partition"], current["ran_on"]), ("gpu", []))

    def test_an_array_on_two_members_lists_both_and_a_pending_aggregate_adds_nothing(self):
        job = self.submit_set("--array", "3", partition="gpu,bioe")
        self.assertEqual(self.current()["ran_on"], [])
        self.slurm.set(job, "RUNNING", partition="gpu")
        self.slurm.set(job, None, "RUNNING", partition="bioe")
        current = self.current()
        self.assertEqual((current["ran_on"], current["anomalies"]), (["bioe", "gpu"], []))
        self.assertEqual([current["tasks"][key] for key in ("running", "pending")], [2, 1])
        self.assertIn(current["partition"], ("gpu", "bioe", "gpu,bioe"))  # the first row's text

    def test_ran_on_follows_the_spec_of_the_current_job_only(self):
        first = self.submit_set(partition="gpu")
        self.assertNotIn("ran_on", self.current())
        self.slurm.set(first, "COMPLETED")
        second = self.submit_set("--resubmit", "on a set", partition="gpu,bioe")
        self.slurm.set(second, "RUNNING", partition="bioe")
        self.assertEqual(self.current()["ran_on"], ["bioe"])
        self.slurm.set(second, "COMPLETED")
        self.submit_set("--resubmit", "back to one", partition="stat")
        self.assertNotIn("ran_on", self.current())

    def test_an_unlisted_member_on_any_row_is_unknown_partition(self):
        self.slurm.add(NAME, "RUNNING", "PENDING", partition="gpu,mystery")
        current = self.current(code=6)
        self.assertEqual(current["partition"], "gpu")  # the first row names a listed partition
        self.assertEqual(current["anomalies"], ["unknown_partition"])

    def test_a_preempted_attempt_on_a_non_preemptible_member_is_unexpected(self):
        job = self.submit_set(partition="gpu,bioe")
        self.slurm.set(job, "RUNNING", partition="bioe")
        self.slurm.requeue(job)
        current = self.current(code=6)
        self.assertEqual(current["anomalies"], ["unexpected_preemption"])
        self.assertEqual((current["partition"], current["ran_on"]), ("gpu,bioe", ["bioe"]))
        self.assertEqual(
            (current["max_restart"], current["cost"]),
            (1, {"cpu_seconds": 30, "gpu_seconds": 0, "final": False}),
        )


class StatusWithoutSpec(Flow):
    """--all and --name read no spec: no ran_on, and a list of listed members is no anomaly."""

    def test_all_and_name_print_slurm_text_without_ran_on(self):
        code, out = self.submit(partition=GPU_SET)
        self.assertEqual(code, 0)
        for partition in (GPU_SET, "stat"):
            if partition == "stat":
                self.slurm.set(int(out["job_id"]), "RUNNING", partition="stat")
            for argv in (("--all",), ("--name", NAME)):
                with self.subTest(partition=partition, argv=argv):
                    code, report = self.shk("status", *argv)
                    current = report["names"][0]["current"]
                    self.assertEqual(code, 0)
                    self.assertEqual(list(current), CURRENT_0_7_1)
                    self.assertEqual((current["partition"], current["anomalies"]), (partition, []))

    def test_a_single_partition_job_keeps_the_0_7_1_current_keys(self):
        self.submit(partition="gpu")
        current = self.entry()["current"]
        self.assertEqual(list(current), CURRENT_0_7_1)
        self.assertEqual((current["partition"], current["anomalies"]), ("gpu", []))


class DetailedStatus(DetailsFlow):
    """--details and --job-id through details.sh and canned Slurm text; details.py is unchanged."""

    def test_task_details_reads_the_frozen_spec_of_a_set(self):
        code, out = self.submit(partition=GPU_SET)  # the stock fake stages the spec of job 1001
        self.assertEqual((code, out["job_id"]), (0, "1001"))
        started = {"nodes": "sh01-01n01"}
        cases = (
            ("pending", "PENDING", "PENDING", GPU_SET, GPU_SET, {}, []),
            ("running", "RUNNING", "RUNNING", "bioe", "bioe", started, ["bioe"]),
            ("finished", "COMPLETED", "COMPLETED", "bioe", "bioe", started, ["bioe"]),
            ("cancelled", "CANCELLED", f"CANCELLED by {UID}", "stat", GPU_SET, {}, []),
            ("one member left", "PENDING", "PENDING", "gpu", "gpu", {}, []),
        )
        for label, shown, stored, in_queue, in_acct, nodes, ran_on in cases:
            with self.subTest(label):
                self.canned(
                    queue=[queue("1001", "1001", shown, partition=in_queue, **nodes)],
                    accounting=[account("1001", "1001", stored, partition=in_acct)],
                    jobs=[record("1001", state=shown)],
                )
                code, report = self.details("--task", "train-a")
                current = report["names"][0]["current"]
                self.assertEqual(code, 0)
                self.assertEqual(list(current)[:3], ["job_id", "partition", "ran_on"])
                self.assertEqual(
                    (current["partition"], current["ran_on"], current["anomalies"]),
                    (in_queue, ran_on, []),
                )
                details = report["details"]
                self.assertEqual([row["partition"] for row in details["queue"]], [in_queue])
                self.assertEqual([row["partition"] for row in details["accounting"]], [in_acct])
                self.assertIsInstance(self.fake, CannedSlurm)  # it, not the stock fake, answered
                self.assertEqual(
                    [len(self.fake.calls(tool)) for tool in ("squeue", "sacct")], [1, 1]
                )

    def test_job_id_prints_no_ran_on_and_a_list_of_listed_members_is_no_anomaly(self):
        self.canned(
            queue=[queue("7600", "7600", "PENDING", partition=GPU_SET)],
            accounting=[account("7600", "7600", "PENDING", partition=GPU_SET)],
            jobs=[record("7600")],
        )
        for argv in (("--job-id", "7600"), ("--job-id", "7600", "--details")):
            with self.subTest(argv=argv):
                code, report = self.shk("status", *argv)
                current = report["names"][0]["current"]
                self.assertEqual(code, 0)
                self.assertNotIn("ran_on", current)
                self.assertEqual((current["partition"], current["anomalies"]), (GPU_SET, []))

    def test_a_pending_restart_of_a_list_whose_earlier_attempt_is_not_visible(self):
        for partition, anomalies in (
            ("gpu,bioe", ["unexpected_preemption"]),
            ("owners,gpu", []),
            ("gpu,mystery", ["unknown_partition"]),
        ):
            with self.subTest(partition=partition):
                self.canned(
                    queue=[queue("7610", "7610", "PENDING", partition=partition)],
                    accounting=[
                        account("7610", "7610", "PENDING", partition=partition, restarts="1")
                    ],
                    jobs=[record("7610")],
                )
                code, report = self.shk("status", "--job-id", "7610")
                current = report["names"][0]["current"]
                self.assertEqual((code, current["anomalies"]), (6 if anomalies else 0, anomalies))
                self.assertEqual(
                    current["cost"], {"cpu_seconds": 0, "gpu_seconds": 0, "final": False}
                )


class Resolution(unittest.TestCase):
    """resolve_name over parsed rows: the verdicts do not depend on Slurm's row order."""

    def current(self, queue=(), acct=(), newest=None, invariants=True):
        rows = slurm.parse_rows(list(queue), list(acct), lambda _name: True)
        found = rows.by_name.get(N, [])
        return slurm.resolve_name(found, newest_spec=newest, invariants=invariants)["current"]

    def test_the_same_rows_in_two_orders_give_the_same_anomalies_and_ran_on(self):
        newest = slurm.SpecFile(300, 3, ("gpu", "bioe"))
        rows = [
            q("300_0", "RUNNING", "gpu"),
            q("300_1", "RUNNING", "bioe"),
            q("300_[2]", "PENDING", "gpu,mystery"),
        ]
        acct = [a("300_0", "RUNNING", partition="gpu"), a("300_1", "RUNNING", partition="bioe")]
        forward = self.current(rows, acct, newest)
        backward = self.current(rows[::-1], acct[::-1], newest)
        for current in (forward, backward):
            self.assertEqual(current["anomalies"], ["unknown_partition"])
            self.assertEqual(current["ran_on"], ["bioe", "gpu"])
        self.assertEqual((forward["partition"], backward["partition"]), ("gpu", "gpu,mystery"))

    def test_an_interrupted_list_counts_when_every_member_is_listed_and_not_preemptible(self):
        for text, invariants, anomalies in (
            ("gpu,bioe", True, ["unexpected_preemption"]),
            ("normal,bigmem,service", True, ["unexpected_preemption"]),
            ("owners,gpu", True, []),
            ("gpu,mystery", True, ["unknown_partition"]),
            ("gpu,mystery", False, []),
        ):
            with self.subTest(text=text, invariants=invariants):
                restart = a("500", "PENDING", restart=1, elapsed=0, cpus=0, tres="", partition=text)
                current = self.current(acct=[restart], invariants=invariants)
                self.assertEqual(current["anomalies"], anomalies)
                self.assertEqual(current["cost"]["cpu_seconds"], 0)

    def test_a_preemption_is_judged_on_the_member_it_happened_on(self):
        for member, anomalies in (("bioe", ["unexpected_preemption"]), ("owners", [])):
            with self.subTest(member=member):
                current = self.current(
                    [q("500", "PENDING", f"{member},gpu")],
                    [
                        a("500", "PREEMPTED", elapsed=30, partition=member),
                        a("500", "PENDING", 1, 0, 0, "", partition=f"{member},gpu"),
                    ],
                )
                self.assertEqual(current["anomalies"], anomalies)
                self.assertEqual(current["cost"]["cpu_seconds"], 60)

    def test_ran_on_needs_a_spec_of_this_job_that_lists_several_partitions(self):
        rows = [q("300", "RUNNING", "bioe")]
        self.assertEqual(
            self.current(rows, newest=slurm.SpecFile(300, 1, tuple(MEMBERS)))["ran_on"], ["bioe"]
        )
        for newest in (None, slurm.SpecFile(300, 1), slurm.SpecFile(299, 1, ("gpu", "bioe"))):
            with self.subTest(newest=newest):
                self.assertNotIn("ran_on", self.current(rows, newest=newest))


if __name__ == "__main__":
    unittest.main()
