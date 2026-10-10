"""Fast acceptance of the separately invoked duration-based soak fixture."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sherlock_registry import program_sha256, task_key

fixture_path = Path(__file__).parent / "fixtures" / "protocol_stress.py"
module_spec = importlib.util.spec_from_file_location("protocol_stress", fixture_path)
stress = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(stress)


class ProtocolStressTests(unittest.TestCase):
    def test_unique_reproducible_case_generation(self):
        cases = [stress.configuration(19, index) for index in range(64)]
        self.assertEqual(cases, [stress.configuration(19, index) for index in range(64)])
        self.assertEqual(len({case["seed"] for case in cases}), 64)
        self.assertEqual({case["family"] for case in cases}, set(stress.FAMILIES))
        self.assertEqual(len(stress.FAMILIES), 7)
        self.assertNotEqual(cases, [stress.configuration(20, index) for index in range(64)])
        for case in cases:
            with self.subTest(index=case["index"]):
                plan = case["task_plan"]
                self.assertEqual(bool(plan), case["family"] == "resolution")
                if case["family"] == "resolution" and case["partition"] == "normal":
                    self.assertEqual([item["top"] for item in plan], [1 if case["anomaly"] else 0] + [0] * (len(plan) - 1))
                self.assertEqual(case["fetch_fault"] is not None, case["family"] == "fetch_death")
                self.assertEqual(case["reject"] is not None, case["family"] == "fetch_reject")

    def test_real_process_registry_rsync_smoke_and_audit_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "report"
            report = stress.run(root, duration_seconds=60, seed=20261007, max_cases=16)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["case_count"], 16)
            self.assertFalse(report["live_integration"])
            self.assertFalse(report["scientific_acceptance"])
            self.assertIn("no child Slurm jobs", report["scheduler_evidence"])
            # Two families kill a real process each: the runner under sbatch and the fetcher at a durability boundary.
            self.assertGreaterEqual(report["crash_count"], 4)
            self.assertEqual(report["module_sha256"]["sherlock_registry.py"], program_sha256())
            self.assertEqual(report["registry_program_sha256"], program_sha256())
            self.assertLess(report["registry_program_bytes"], 100_000)
            self.assertEqual(set(report["module_sha256"]), {"sherlock_registry.py", "sherlock_orchestration.py", "sherlock_artifacts.py", "sherlock_commands.py"})
            self.assertFalse((root / "active-case").exists())
            chain = "0" * 64
            records = []
            for line in (root / "cases.jsonl").read_text().splitlines():
                chain = hashlib.sha256((chain + line).encode()).hexdigest()
                records.append(json.loads(line))
            self.assertEqual(chain, report["case_chain_sha256"])
            self.assertEqual(len(records), 16)
            self.assertEqual({r["config"]["family"] for r in records}, set(stress.FAMILIES))
            self.assertEqual(json.loads((root / "report.json").read_text())["status"], "passed")
            for record in records:
                with self.subTest(index=record["config"]["index"]):
                    self.assertIn(record["config"]["family"], record["outcome"]["invariants"] + ["fetch_reject"])
                    if record["config"]["family"] in {"submit_race", "runner_death", "resolution"}:
                        self.assertIn("marker_integrity", record["outcome"]["invariants"])
                        self.assertTrue(record["outcome"]["attempts"])
                        for attempt in record["outcome"]["attempts"]:
                            self.assertIn("record.json", attempt["files"])
            with self.assertRaisesRegex(AssertionError, "fresh"):
                stress.run(root, max_cases=1)

    def test_failed_case_preserves_counterexample_and_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "failed"
            with patch.object(stress, "protocol_case", side_effect=AssertionError("injected invariant violation")):
                with self.assertRaisesRegex(AssertionError, "injected invariant"):
                    stress.run(root, duration_seconds=1, seed=23, max_cases=5)
            report = json.loads((root / "report.json").read_text())
            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["case_count"], 0)
            self.assertEqual(report["counterexample"]["config"], stress.configuration(23, 0))
            self.assertTrue((root / "active-case").exists())

    def test_input_bounds_reject_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "unused"
            for kwargs in ({"duration_seconds": 601}, {"duration_seconds": 0}, {"max_cases": 40001}):
                with self.subTest(kwargs=kwargs), self.assertRaises(AssertionError):
                    stress.run(root, **kwargs)
            self.assertFalse(root.exists())

    def test_check_registry_rejects_broken_trees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            case = stress.prepare(root)
            config = stress.configuration(7, 0)
            attempt = stress.attempt_id(config, 0)
            self.assertEqual(stress.submit(case, config, attempt), ("submitted", str(config["job"])))
            summary = stress.check_registry(case)
            self.assertEqual([(row["attempt"], row["files"], row["parent"]) for row in summary], [(attempt, ["record.json", "submitted.json"], None)])
            key = task_key(json.loads((case.registry / "attempts" / attempt / "record.json").read_text())["spec"])
            self.assertEqual(summary[0]["key"], key)
            stray = case.registry / "tasks" / ("f" * 64)
            stray.write_text(attempt + "\n")
            stray.chmod(0o600)
            with self.assertRaisesRegex(AssertionError, "task marker"):
                stress.check_registry(case)
            stray.unlink()
            leaked = case.registry / "attempts" / attempt / "not_sent.json"
            leaked.write_text("{}\n")
            leaked.chmod(0o600)
            with self.assertRaisesRegex(AssertionError, "both submitted.json and not_sent.json"):
                stress.check_registry(case)
            leaked.unlink()
            os.chmod(case.registry / "attempts" / attempt / "record.json", 0o640)
            with self.assertRaisesRegex(AssertionError, "not 0600"):
                stress.check_registry(case)


if __name__ == "__main__":
    unittest.main()
