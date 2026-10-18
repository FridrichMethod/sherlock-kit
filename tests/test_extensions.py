"""Versioned JSON and controller extensions through real fixed POSIX shell programs."""

import io
import json
import os
import sys
from contextlib import redirect_stderr

from fakes import SRC
from fakes.expired_slurm import CannedSlurm
from test_flows import Flow

sys.path.insert(0, str(SRC))
from sherlock_kit import cli


class ExtensionFlows(Flow):
    def tool(self, name, body):
        directory = self.tmp / "tools"
        directory.mkdir(exist_ok=True)
        script = directory / name
        script.write_text("#!/bin/sh\n" + body)
        script.chmod(0o755)
        self.knobs(PATH=f"{directory}{os.pathsep}{os.environ['PATH']}")
        return script

    def test_versioned_json_success_and_errors(self):
        self.assertEqual(self.submit("--dry-run")[1].get("schema_version"), 1)
        self.assertEqual(self.submit(partition="mystery")[1].get("schema_version"), 1)
        self.assertEqual(cli._render({"text": "raw"}, 0), "raw")

    def test_preflight_uses_test_only_without_creating_run_or_job(self):
        self.tool("sbatch", 'cat >/dev/null\nprintf "%s\\n" "$@"\nprintf "quota accepted\\n" >&2\n')
        code, report = self.submit("--preflight")
        self.assertEqual((code, report["action"], report["job_submitted"]), (0, "preflight", False))
        self.assertIn("--test-only", report["preflight"]["stdout"])
        self.assertEqual(report["preflight"]["stderr"], "quota accepted\n")
        self.assertEqual(self.slurm.state()["jobs"], [])
        self.assertFalse(self.remote.exists())

    def test_preflight_refusal_retains_evidence_and_is_never_unknown(self):
        self.tool("sbatch", 'printf "quota refused\\n" >&2\nexit 7\n')
        code, report = self.submit("--preflight")
        self.assertEqual((code, report["action"], report["job_submitted"]), (3, "preflight", False))
        self.assertEqual(
            report["preflight"], {"returncode": 7, "stdout": "", "stderr": "quota refused\n"}
        )
        self.assertFalse(self.remote.exists())

    def test_hold_and_token_are_cli_owned_and_frozen(self):
        code, report = self.submit("--hold", "--submission-token", "public-123")
        self.assertEqual(code, 0)
        self.assertIn("--hold", report["argv"])
        self.assertIn("--comment=public-123", report["argv"])
        frozen = json.loads(next(self.shk_dir().glob("*.json")).read_text())
        self.assertEqual((frozen["hold"], frozen["submission_token"]), (True, "public-123"))
        for option in ("--hold", "-H", "--ho", "--comment=x", "--comm=x", "-vH"):
            (self.tmp / "bad.sh").write_text(f"#!/bin/sh\n#SBATCH {option}\necho work\n")
            self.assertEqual(self.submit("--dry-run", script="bad.sh")[0], 3)
        self.assertEqual(self.submit("--dry-run", "--submission-token", "bad|token")[0], 1)

    def detail_tools(self, accounting=None, queue="", jobs=()):
        uid = os.getuid()
        name = "legacy-job"
        accounting = (
            accounting
            if accounting is not None
            else (
                f"44|44|sherlock|100|COMPLETED|normal|30|2|cpu=2|2026-10-10T12:00:00|"
                f"2026-10-10T12:00:01|2026-10-10T12:00:31|0|0:0|sh01|{uid}|tok|{name}\n"
                f"44|44|sherlock|101|COMPLETED|normal|40|2|cpu=2|2026-10-10T12:00:00|"
                f"2026-10-10T12:00:32|2026-10-10T12:01:12|0|0:0|sh01|{uid}|tok|{name}\n"
            )
        )
        self.canned = CannedSlurm(
            self.tmp, queue=queue.splitlines(), accounting=accounting.splitlines(), jobs=jobs
        )
        self.knobs(**self.canned.env())

    def test_details_preserve_distinct_accounting_identities_and_legacy_names(self):
        self.detail_tools()
        code, report = self.shk("status", "--job-id", "44", "--details")
        self.assertEqual(code, 0)
        details = report["details"]
        self.assertTrue(details["complete"])
        self.assertEqual([r["db_index"] for r in details["accounting"]], ["100", "101"])
        self.assertEqual(details["accounting"][0]["job_id_raw"], "44")
        self.assertEqual(details["accounting"][0]["uid"], os.getuid())
        self.assertEqual(details["accounting"][0]["comment"], "tok")
        self.assertIn("2026-10-10T12:00:01", details["accounting"][0]["raw"])
        self.assertEqual(report["names"][0]["current"]["cost"]["cpu_seconds"], 140)

    def test_details_missing_and_partial_evidence_are_explicit(self):
        self.detail_tools(accounting="")
        code, report = self.shk("status", "--job-id", "44", "--details")
        self.assertEqual(code, 0)
        self.assertFalse(report["details"]["complete"])
        self.assertEqual(report["details"]["missing_job_ids"], ["44"])
        self.detail_tools(accounting="bad row\n")
        self.assertFalse(
            self.shk("status", "--job-id", "44", "--details")[1]["details"]["complete"]
        )

    def test_details_enforce_uid_even_when_slurm_returns_other_users(self):
        self.detail_tools(
            accounting=f"44|44|sherlock|100|COMPLETED|normal|1|1|cpu=1|Unknown|Unknown|Unknown|0|0:0|sh01|{os.getuid() + 1}|tok|private\n"
        )
        code, report = self.shk("status", "--job-id", "44", "--details")
        self.assertEqual(code, 0)
        self.assertEqual(report["details"]["accounting"], [])
        self.assertEqual(report["names"], [])
        for job in ("44,45", "44;evil", "0", "44_[0-1]"):
            self.assertEqual(self.shk("status", "--job-id", job, "--details")[0], 1)

    def test_resources_use_slurm_configured_and_allocated_memory(self):
        self.tool(
            "scontrol",
            'printf "NodeName=sh01 State=MIXED Partitions=owners,gpu ActiveFeatures=GPU_H100 CPUAlloc=4 CPUEfctv=32 CPUTot=32 RealMemory=128000 AllocMem=64000 FreeMem=1 CfgTRES=cpu=32,mem=125G,gres/gpu=4,gres/gpu:h100=4 AllocTRES=cpu=4,mem=62500M,gres/gpu=1,gres/gpu:h100=1\\n"\n',
        )
        code, report = self.shk("resources", "--partition", "owners")
        self.assertEqual(code, 0)
        node = report["nodes"][0]
        self.assertEqual(
            (node["free_cpus"], node["free_memory_mb"], node["free_gpus"]), (28, 65500, 3)
        )
        self.assertTrue(report["complete"])
        self.assertEqual(node["features"], ["GPU_H100"])
        self.assertIn("CfgTRES", node["raw"])

    def test_resources_do_not_guess_missing_allocation(self):
        self.tool(
            "scontrol",
            'printf "NodeName=sh01 State=IDLE Partitions=normal CPUTot=32 FreeMem=99999 CfgTRES=cpu=32,mem=100G\\n"\n',
        )
        code, report = self.shk("resources", "--partition", "normal")
        self.assertEqual(code, 0)
        self.assertFalse(report["complete"])
        self.assertIsNone(report["nodes"][0]["free_memory_mb"])
        self.assertIsNone(report["nodes"][0]["free_cpus"])

    def test_release_verifies_exact_identity_and_user_hold(self):
        record = self.tmp / "control-call"
        uid = os.getuid()
        self.tool(
            "scontrol",
            f'case "$1" in show) printf "JobId=44 JobName=expected UserId=user({uid}) Comment=token JobState=PENDING Reason=JobHeldUser Priority=0\\n" ;; release) printf "%s\\n" "$@" >"{record}" ;; esac\n',
        )
        code, report = self.shk(
            "release", "--job-id", "44", "--expect-name", "expected", "--expect-token", "token"
        )
        self.assertEqual((code, report["action"], report["job_id"]), (0, "released", "44"))
        self.assertEqual(record.read_text(), "release\n44\n")
        for name, token in [("wrong", "token"), ("expected", "wrong")]:
            record.unlink(missing_ok=True)
            self.assertEqual(
                self.shk(
                    "release", "--job-id", "44", "--expect-name", name, "--expect-token", token
                )[0],
                3,
            )
            self.assertFalse(record.exists())

    def test_release_lost_reply_is_unknown_and_no_retry(self):
        self.tool(
            "scontrol",
            f'case "$1" in show) printf "JobId=44 JobName=expected UserId=user({os.getuid()}) Comment=token JobState=PENDING Reason=JobHeldUser Priority=0\\n" ;; release) exit 1 ;; esac\n',
        )
        code, report = self.shk(
            "release", "--job-id", "44", "--expect-name", "expected", "--expect-token", "token"
        )
        self.assertEqual((code, report["action"]), (5, "unknown"))
        self.assertIn("--job-id 44", report["next"])

    def test_requeue_refuses_env_only_allocation_or_ssh(self):
        self.knobs(SLURM_JOB_ID="44")
        self.tool("scontrol", "exit 0\n")
        self.assertEqual(
            self.shk("requeue", "--self", "--expect-name", "expected", "--expect-token", "token")[
                0
            ],
            3,
        )

    def test_cli_mutually_exclusive_selectors(self):
        parser = cli.build_parser()
        for argv in [
            ("status", "--job-id", "44", "--name", "name"),
            (
                "submit",
                "--task",
                "x",
                "--script",
                "x",
                "--partition",
                "normal",
                "--cpus",
                "1",
                "--mem",
                "1G",
                "--time",
                "10m",
                "--dry-run",
                "--preflight",
            ),
        ]:
            with (
                self.subTest(argv=argv),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(argv)


class GuardAndEvidenceFlows(Flow):
    tool = ExtensionFlows.tool
    detail_tools = ExtensionFlows.detail_tools

    def test_self_requeue_requires_own_process_uid_identity_and_owners(self):
        self.knobs(SLURM_JOB_ID="44")
        record = self.tmp / "requeue-call"
        uid = os.getuid()
        script = self.tool(
            "scontrol",
            f'case "$1" in listpids) printf "%s 44 batch 0 0\\n" "$PPID" ;; show) printf "JobId=44 JobName=expected UserId=user({uid}) Comment=token JobState=RUNNING Partition=owners Requeue=1\\n" ;; requeue) printf "%s\\n" "$@" >"{record}" ;; esac\n',
        )
        code, report = self.shk(
            "requeue", "--self", "--expect-name", "expected", "--expect-token", "token"
        )
        self.assertEqual((code, report.get("action")), (0, "requeued"))
        self.assertEqual(record.read_text(), "requeue\n44\n")
        original = script.read_text()
        for old, new in [
            ("Partition=owners", "Partition=normal"),
            ("Requeue=1", "Requeue=0"),
            ("JobState=RUNNING", "JobState=PENDING"),
            (f"({uid})", f"({uid + 1})"),
            ("Comment=token", "Comment=changed"),
            ("JobId=44", "JobId=45"),
        ]:
            script.write_text(original.replace(old, new))
            record.unlink(missing_ok=True)
            self.assertEqual(
                self.shk(
                    "requeue", "--self", "--expect-name", "expected", "--expect-token", "token"
                )[0],
                3,
            )
            self.assertFalse(record.exists())

    def test_array_self_requeues_physical_element_not_parent(self):
        self.knobs(SLURM_JOB_ID="45", SLURM_ARRAY_JOB_ID="44", SLURM_ARRAY_TASK_ID="2")
        record = self.tmp / "requeue-call"
        self.tool(
            "scontrol",
            f'case "$1" in listpids) printf "%s 45 batch 0 0\\n" "$PPID" ;; show) printf "JobId=45 ArrayJobId=44 ArrayTaskId=2 JobName=expected UserId=user({os.getuid()}) Comment=token JobState=RUNNING Partition=owners Requeue=1\\n" ;; requeue) printf "%s\\n" "$@" >"{record}" ;; esac\n',
        )
        self.assertEqual(
            self.shk("requeue", "--self", "--expect-name", "expected", "--expect-token", "token")[
                0
            ],
            0,
        )
        self.assertEqual(record.read_text(), "requeue\n45\n")

    def test_release_refuses_other_uid_system_hold_running_and_multiple_records(self):
        record = self.tmp / "release-call"
        uid = os.getuid()
        script = self.tool(
            "scontrol",
            f'case "$1" in show) printf "JobId=44 JobName=expected UserId=user({uid}) Comment=token JobState=PENDING Reason=JobHeldUser Priority=0\\n" ;; release) touch "{record}" ;; esac\n',
        )
        original = script.read_text()
        for old, new in [
            (f"({uid})", f"({uid + 1})"),
            ("JobHeldUser", "JobHeldAdmin"),
            ("PENDING", "RUNNING"),
            ("Priority=0", "Priority=1"),
            ("JobId=44", "JobId=45"),
            ("Priority=0\\n", "Priority=0\\nJobId=44\\n"),
        ]:
            script.write_text(original.replace(old, new))
            self.assertEqual(
                self.shk(
                    "release",
                    "--job-id",
                    "44",
                    "--expect-name",
                    "expected",
                    "--expect-token",
                    "token",
                )[0],
                3,
            )
            self.assertFalse(record.exists())

    def test_details_retains_held_job_requests_and_missing_tool_status(self):
        uid = os.getuid()
        queue = f"44|44|PENDING|normal|0:00|4|2026-10-10T12:00:00|Unknown|Unknown|(null)|{uid}|tok|legacy-job\n"
        job = f"JobId=44 JobName=legacy-job UserId=user({uid}) Comment=tok JobState=PENDING Reason=JobHeldUser Priority=0 NumCPUs=4 NumTasks=1 CPUs/Task=4 ReqB:S:C:T=0:0:*:* ReqTRES=cpu=4,mem=2G Features=CPU_GEN:ROM Requeue=0 TimeLimit=00:20:00"
        self.detail_tools(accounting="", queue=queue, jobs=[job])
        code, report = self.shk("status", "--job-id", "44", "--details")
        self.assertEqual(code, 0)
        self.assertTrue(report["details"]["complete"])
        self.assertEqual(report["details"]["jobs"][0]["fields"]["ReqTRES"], "cpu=4,mem=2G")
        self.assertEqual(report["details"]["jobs"][0]["fields"]["NumTasks"], "1")
        self.assertEqual(report["details"]["jobs"][0]["fields"]["CPUs/Task"], "4")
        self.assertIsNone(report["details"]["queue"][0]["allocated_cpus"])
        self.assertIsNone(report["details"]["queue"][0]["started_at"])
        self.tool("sacct", "exit 1\n")
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertFalse(report["details"]["complete"])
        self.assertFalse(report["names"][0]["current"]["cost"]["final"])

    def test_details_full_accounting_missing_cost_evidence_is_not_final(self):
        self.detail_tools(
            accounting=f"44|44|sherlock|100|COMPLETED|normal||2|cpu=2|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:00:31|0|0:0|sh01|{os.getuid()}|tok|legacy-job\n"
        )
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertFalse(report["details"]["complete"])
        self.assertIsNone(report["details"]["accounting"][0]["elapsed_seconds"])
        self.assertFalse(report["names"][0]["current"]["cost"]["final"])

    def test_resources_partial_tool_failure_retains_nodes(self):
        self.tool(
            "scontrol",
            'printf "NodeName=sh01 State=IDLE Partitions=normal CPUEfctv=32 CPUAlloc=0 CfgTRES=cpu=32,mem=100G AllocTRES=cpu=0,mem=0\\n"\nexit 1\n',
        )
        code, report = self.shk("resources", "--partition", "normal")
        self.assertEqual(code, 0)
        self.assertFalse(report["complete"])
        self.assertEqual(report["nodes"][0]["free_memory_mb"], 102400)


class NativeFormatFlows(Flow):
    tool = ExtensionFlows.tool
    detail_tools = ExtensionFlows.detail_tools

    def test_idle_node_empty_alloc_tres_means_zero_allocation(self):
        self.tool(
            "scontrol",
            'printf "NodeName=sh01 State=IDLE Partitions=normal CPUEfctv=32 CPUAlloc=0 CfgTRES=cpu=32,mem=100G AllocTRES=\\n"\n',
        )
        code, report = self.shk("resources", "--partition", "normal")
        self.assertEqual(code, 0)
        self.assertTrue(report["complete"])
        self.assertEqual(report["nodes"][0]["free_memory_mb"], 102400)
        self.assertEqual(report["nodes"][0]["free_gpus"], 0)

    def test_control_details_ignore_unrelated_own_job(self):
        self.detail_tools(
            queue=f"44|44|PENDING|normal|0:00|4|Unknown|Unknown|Unknown|(null)|{os.getuid()}|tok|legacy-job\n",
            accounting="",
            jobs=[f"JobId=45 JobName=unrelated UserId=user({os.getuid()}) Comment=tok"],
        )
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertEqual(report["details"]["jobs"], [])
        self.assertFalse(report["details"]["complete"])


class IdentityChronologyFlows(Flow):
    tool = ExtensionFlows.tool
    detail_tools = ExtensionFlows.detail_tools

    def test_physical_id_selects_logical_array_element(self):
        self.detail_tools(
            accounting=f"44_2|45|sherlock|100|COMPLETED|owners|1|1|cpu=1|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:00:02|0|0:0|sh01|{os.getuid()}|tok|legacy-job\n"
        )
        report = self.shk("status", "--job-id", "45", "--details")[1]
        self.assertFalse(report["details"]["complete"])
        self.assertEqual(report["details"]["accounting"][0]["job_id"], "44_2")

    def test_accounting_state_follows_chronology_not_print_order(self):
        uid = os.getuid()
        newest = f"44|44|sherlock|101|COMPLETED|owners|40|2|cpu=2|2026-10-10T12:00:00|2026-10-10T12:00:32|2026-10-10T12:01:12|0|0:0|sh01|{uid}|tok|legacy-job\n"
        older = f"44|44|sherlock|100|PREEMPTED|owners|30|2|cpu=2|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:00:31|0|0:15|sh01|{uid}|tok|legacy-job\n"
        self.detail_tools(accounting=newest + older)
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertEqual(report["names"][0]["state"], "done")
        self.assertEqual(report["names"][0]["current"]["cost"]["cpu_seconds"], 140)

    def test_running_queue_end_is_prediction_not_actual(self):
        self.detail_tools(
            accounting="",
            queue=f"44|44|RUNNING|normal|1:00|4|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:20:01|sh01|{os.getuid()}|tok|legacy-job\n",
            jobs=[f"JobId=44 JobName=legacy-job UserId=user({os.getuid()}) Comment=tok"],
        )
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertIsNone(report["details"]["queue"][0]["ended_at"])

    def test_preflight_keeps_trailing_blank_stdout_lines(self):
        self.tool("sbatch", 'cat >/dev/null\nprintf "accepted\\n\\n"\n')
        code, report = self.submit("--preflight")
        self.assertEqual(code, 0)
        self.assertEqual(report["preflight"]["stdout"], "accepted\n\n")


class EvidenceCompletenessRegressions(Flow):
    tool = ExtensionFlows.tool
    detail_tools = ExtensionFlows.detail_tools

    def accounting(self, *, tres="cpu=1,gres/gpu=1", restarts=0, job="44", physical="44"):
        return f"{job}|{physical}|sherlock|100|COMPLETED|owners|60|1|{tres}|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:01:01|{restarts}|0:0|sh01|{os.getuid()}|tok|legacy-job\n"

    def test_missing_allocated_tres_prevents_complete_and_final_cost(self):
        self.detail_tools(accounting=self.accounting(tres=""))
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertFalse(report["details"]["complete"])
        self.assertIsNone(report["details"]["accounting"][0]["allocated_tres"])
        self.assertFalse(report["names"][0]["current"]["cost"]["final"])

    def test_restart_history_gap_prevents_complete_and_final_cost(self):
        self.detail_tools(accounting=self.accounting(restarts=2))
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertFalse(report["details"]["complete"])
        self.assertEqual(report["names"][0]["current"]["tasks"]["incomplete_history"], [0])
        self.assertFalse(report["names"][0]["current"]["cost"]["final"])

    def test_queue_identity_coverage_required_despite_successful_scontrol(self):
        self.detail_tools(
            accounting="",
            queue=f"44|44|PENDING|normal|0:00|4|Unknown|Unknown|Unknown|(null)|{os.getuid()}|tok|legacy-job\n",
            # The meta record left the controller after squeue; its sibling still answers.
            jobs=[
                f"JobId=45 ArrayJobId=44 ArrayTaskId=0 JobName=legacy-job UserId=user({os.getuid()})"
            ],
        )
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertFalse(report["details"]["complete"])
        self.assertEqual(report["details"]["missing_control_job_ids"], ["44"])
        self.assertFalse(report["names"][0]["current"]["cost"]["final"])

    def test_exact_array_element_selection_does_not_infer_failed_siblings(self):
        for selector in ("45", "44_2"):
            self.detail_tools(accounting=self.accounting(job="44_2", physical="45"))
            report = self.shk("status", "--job-id", selector, "--details")[1]
            self.assertFalse(report["details"]["complete"])
            entry = report["names"][0]
            self.assertEqual(entry["state"], "unknown")
            self.assertEqual(entry["scope"], "partial_array")
            self.assertIsNone(entry["current"]["array"]["count"])
            self.assertIsNone(entry["current"]["tasks"]["missing"])
            self.assertFalse(entry["current"]["cost"]["final"])
            self.assertEqual(report["details"]["selection"]["partial_array_ids"], ["44"])


class PendingEvidenceRegressions(Flow):
    tool = ExtensionFlows.tool
    detail_tools = ExtensionFlows.detail_tools

    def test_held_pending_unallocated_tres_can_be_structurally_complete(self):
        uid = os.getuid()
        self.detail_tools(
            accounting=f"44|44|sherlock|100|PENDING|owners|0|0||2026-10-10T12:00:00|Unknown|Unknown|0|0:0|None assigned|{uid}|tok|legacy-job\n",
            queue=f"44|44|PENDING|owners|0:00|4|2026-10-10T12:00:00|Unknown|Unknown|(null)|{uid}|tok|legacy-job\n",
            jobs=[
                f"JobId=44 JobName=legacy-job UserId=user({uid}) Comment=tok JobState=PENDING Reason=JobHeldUser Priority=0 ReqTRES=cpu=4,mem=2G Requeue=1"
            ],
        )
        report = self.shk("status", "--job-id", "44", "--details")[1]
        self.assertTrue(report["details"]["complete"])
        self.assertTrue(report["details"]["allocation_evidence_complete"])
        self.assertFalse(report["names"][0]["current"]["cost"]["final"])

    def test_history_gap_marks_all_summaries_nonfinal(self):
        uid = os.getuid()
        records = "".join(
            f"{job}|{job}|sherlock|{job}|COMPLETED|owners|60|1|cpu=1,gres/gpu=1|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:01:01|{restarts}|0:0|sh01|{uid}|tok|{name}\n"
            for job, restarts, name in [("44", 0, "a-complete"), ("45", 2, "z-gap")]
        )
        self.detail_tools(accounting=records)
        report = self.shk("status", "--job-id", "44", "--job-id", "45", "--details")[1]
        self.assertFalse(report["details"]["complete"])
        self.assertTrue(all(not entry["current"]["cost"]["final"] for entry in report["names"]))
