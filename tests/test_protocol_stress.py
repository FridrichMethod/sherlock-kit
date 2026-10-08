"""Fast acceptance of the separately invoked duration-based soak fixture."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
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
        self.assertNotEqual(cases, [stress.configuration(20, index) for index in range(64)])

    def test_real_process_sqlite_rsync_smoke_and_audit_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "report"
            report = stress.run(root, duration_seconds=30, seed=20261007, max_cases=16)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["case_count"], 16)
            self.assertFalse(report["live_integration"])
            self.assertFalse(report["scientific_acceptance"])
            self.assertGreater(report["crash_count"], 0)
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
            with self.assertRaisesRegex(AssertionError, "fresh"):
                stress.run(root, max_cases=1)

    def test_failed_case_preserves_counterexample_and_exits(self):
        from unittest.mock import patch
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


if __name__ == "__main__":
    unittest.main()
