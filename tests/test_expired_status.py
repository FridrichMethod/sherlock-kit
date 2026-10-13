"""Expired IDs must use durable accounting without poisoning live queue evidence."""

import json
import os
import pwd
import shlex
import subprocess
import sys
from pathlib import Path

from fakes import SRC
from test_flows import Flow

FIXTURE = Path(__file__).parent / "fakes/expired_slurm.py"


def expiry_fixture(directory):
    uid = os.getuid()
    queue = [
        f"55_0|55|RUNNING|normal|1:00|1|2026-10-10T12:00:00|2026-10-10T12:00:01|Unknown|sh01|{uid}|tok|live-array",
        f"88|88|RUNNING|normal|1:00|1|2026-10-10T12:00:00|2026-10-10T12:00:01|Unknown|sh01|{uid}|tok|unrelated-own",
        f"55_9|99|RUNNING|normal|1:00|1|2026-10-10T12:00:00|2026-10-10T12:00:01|Unknown|sh01|{uid + 1}|tok|foreign-job",
    ]
    accounting = [
        f"{job}|{physical}|sherlock|{physical}|COMPLETED|normal|614|1|cpu=1|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:10:15|0|0:0|sh01|{uid}|tok|{name}"
        for job, physical, name in [
            ("44", "44", "expired-single"),
            ("66_0", "66", "expired-array"),
            ("66_1", "67", "expired-array"),
        ]
    ]
    accounting.extend([
        f"44|44|sherlock|900|COMPLETED|normal|614|1|cpu=1|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:10:15|0|0:0|sh01|{uid + 1}|tok|foreign-accounting",
        f"88|88|sherlock|901|COMPLETED|normal|614|1|cpu=1|2026-10-10T12:00:00|2026-10-10T12:00:01|2026-10-10T12:10:15|0|0:0|sh01|{uid}|tok|unrelated-accounting",
    ])
    jobs = {
        "55": f"JobId=55 ArrayJobId=55 ArrayTaskId=0 JobName=live-array UserId=user({uid}) Comment=tok JobState=RUNNING"
    }
    state = directory / "expiry-state.json"
    state.write_text(
        json.dumps({"queue": queue, "accounting": accounting, "jobs": jobs, "calls": []})
    )
    tools = directory / "expiry-bin"
    tools.mkdir()
    for tool in ("squeue", "sacct", "scontrol"):
        command = shlex.join([sys.executable, "-B", str(FIXTURE), tool])
        path = tools / tool
        path.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
        path.chmod(0o755)
    return {
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "FAKE_EXPIRY_STATE": str(state),
    }, state


def assert_expiry_report(test, report, state):
    test.assertTrue(report["details"]["complete"])
    test.assertEqual([row["job_id"] for row in report["details"]["queue"]], ["55_0"])
    test.assertEqual([row["job_id"] for row in report["details"]["jobs"]], ["55"])
    test.assertEqual(
        {row["name"] for row in report["details"]["accounting"]},
        {"expired-single", "expired-array"},
    )
    entries = {entry["name"]: entry for entry in report["names"]}
    test.assertEqual(entries["expired-single"]["state"], "done")
    test.assertEqual(entries["expired-array"]["state"], "done")
    test.assertEqual(entries["expired-array"]["current"]["cost"]["cpu_seconds"], 1228)
    calls = json.loads(state.read_text())["calls"]
    queue = next(call["argv"] for call in calls if call["tool"] == "squeue")
    test.assertFalse(any(arg.startswith("--jobs=") for arg in queue))
    test.assertEqual(queue[queue.index("-u") + 1], pwd.getpwuid(os.getuid()).pw_name)
    test.assertEqual([call["argv"][2] for call in calls if call["tool"] == "scontrol"], ["55"])
    test.assertIn(
        "--jobs=44,66,55", next(call["argv"] for call in calls if call["tool"] == "sacct")
    )


class ExpiredStatusFlows(Flow):
    def setUp(self):
        super().setUp()
        env, self.state = expiry_fixture(self.tmp)
        self.knobs(**env)

    def test_expired_single_and_array_mixed_with_live_are_complete_and_scoped(self):
        code, report = self.shk(
            "status", "--job-id", "44", "--job-id", "66", "--job-id", "55", "--details"
        )
        self.assertEqual(code, 0)
        assert_expiry_report(self, report, self.state)

    def test_only_expired_ids_do_not_query_unrelated_live_control_records(self):
        code, report = self.shk("status", "--job-id", "44", "--job-id", "66", "--details")
        self.assertEqual(code, 0)
        self.assertTrue(report["details"]["complete"])
        self.assertEqual(report["details"]["queue"], [])
        self.assertEqual(report["details"]["jobs"], [])
        self.assertEqual(
            {row["name"] for row in report["details"]["accounting"]},
            {"expired-single", "expired-array"},
        )
        calls = json.loads(self.state.read_text())["calls"]
        self.assertFalse(any(call["tool"] == "scontrol" for call in calls))

    def test_genuine_queue_rpc_failure_remains_incomplete(self):
        data = json.loads(self.state.read_text())
        data["queue_rc"] = 1
        self.state.write_text(json.dumps(data))
        code, report = self.shk("status", "--job-id", "44", "--job-id", "66", "--details")
        self.assertEqual(code, 0)
        self.assertFalse(report["details"]["complete"])
        self.assertTrue(report["details"]["accounting"])
        self.assertTrue(all(not entry["current"]["cost"]["final"] for entry in report["names"]))

    def test_source_cli_process_has_the_same_expiry_contract(self):
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "sherlock_kit",
                "--executor",
                "local",
                "status",
                "--job-id",
                "44",
                "--job-id",
                "66",
                "--job-id",
                "55",
                "--details",
            ],
            env={**os.environ, "PYTHONPATH": str(SRC)},
            cwd=self.tmp,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        assert_expiry_report(self, json.loads(result.stdout), self.state)
