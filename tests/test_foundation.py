"""Offline behavioral boundaries for the policy and transport foundation."""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import sherlock_kit as kit


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.private = self.root / "private"
        self.private.mkdir(mode=0o700)
        self.fake = self.root / "ssh"
        self.fake.write_bytes((ROOT / "tests/fixtures/consumer/fake_ssh.py").read_bytes())
        self.fake.chmod(0o700)
        self.record = self.root / "calls.jsonl"
        self.config = kit.TransportConfig(ssh_binary=str(self.fake),
                                         backoff_file=self.private / "backoff.json")
        self.environment = patch.dict(os.environ, {"FAKE_SSH_RECORD": str(self.record), "FAKE_SSH_MODE": "echo"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def calls(self):
        return [json.loads(line) for line in self.record.read_text().splitlines()] if self.record.exists() else []

    def test_remote_arguments_remain_literal(self):
        argv = ["printf", "", "a b", "'quote'", "$(id)", "a;b", "newline\nnext", "*", "--flag"]
        command = kit.ssh_argv(self.config, argv)
        self.assertEqual(shlex.split(command[-1]), argv)
        for flag in ("-T", "-oBatchMode=yes", "-oRemoteCommand=none", "-oConnectTimeout=15"):
            self.assertIn(flag, command)
        result = kit.run_remote(self.config, argv)
        self.assertEqual(result.status, "complete")
        self.assertEqual(json.loads(result.stdout), argv)

    def test_invalid_transport_input(self):
        for host in ("-oProxyCommand=evil", "host;id", "host\nnext", "", "host:22"):
            with self.assertRaises(ValueError):
                kit.TransportConfig(control_host=host)
        for value in (0, -1, float("nan"), float("inf"), True):
            with self.assertRaises(ValueError):
                kit.TransportConfig(command_timeout_seconds=value)
        for argv in ("hostname", [], [""], ["-option"], ["hostname", "\0"], [42]):
            with self.assertRaises(ValueError):
                kit.ssh_argv(self.config, argv)

    def test_unknown_mutation_never_retries(self):
        os.environ["FAKE_SSH_MODE"] = "disconnect"
        result = kit.run_remote(self.config, ["sbatch", "--parsable", "synthetic.sbatch"], mutation=True)
        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.dispatched)
        self.assertFalse(result.retry_performed)
        self.assertEqual(len(self.calls()), 1)

    def test_auth_failure_backoff_shared_across_endpoints_and_calls(self):
        os.environ["FAKE_SSH_MODE"] = "auth"
        result = kit.run_remote(self.config, ["hostname"])
        self.assertEqual(result.status, "auth_required")
        other = kit.TransportConfig(control_host="other-login", ssh_binary=str(self.fake),
                                    backoff_file=self.config.backoff_file)
        held = kit.run_remote(other, ["hostname"], mutation=True)
        self.assertEqual(held.status, "auth_required")
        self.assertFalse(held.dispatched)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(stat.S_IMODE(Path(self.config.backoff_file).stat().st_mode), 0o600)

    def test_mutation_auth_reply_is_still_unknown(self):
        os.environ["FAKE_SSH_MODE"] = "auth"
        result = kit.run_remote(self.config, ["mutating-program"], mutation=True)
        self.assertEqual(result.status, "unknown")
        self.assertTrue(result.dispatched)

    def test_deadline_missing_executable_and_malformed_state(self):
        os.environ["FAKE_SSH_MODE"] = "timeout"
        config = kit.TransportConfig(ssh_binary=str(self.fake), command_timeout_seconds=0.1,
                                    backoff_file=self.config.backoff_file)
        self.assertEqual(kit.run_remote(config, ["hostname"], mutation=True).status, "unknown")
        missing = kit.TransportConfig(ssh_binary=str(self.root / "missing"), backoff_file=self.config.backoff_file)
        result = kit.run_remote(missing, ["hostname"], mutation=True)
        self.assertEqual(result.status, "not_sent")
        self.assertFalse(result.dispatched)
        path = Path(self.config.backoff_file)
        path.write_text("[]")
        path.chmod(0o600)
        self.assertEqual(kit.run_remote(self.config, ["hostname"]).status, "not_sent")
        self.assertEqual(path.read_text(), "[]")

    def test_local_doctor_no_execution_or_writes(self):
        with patch.object(kit, "_execute", side_effect=AssertionError("doctor executed remote command")):
            result = kit.doctor(self.config)
        self.assertEqual(result["mode"], "local")
        self.assertEqual(list(self.private.iterdir()), [])
        self.assertEqual(self.calls(), [])

    def test_remote_doctor_fixed_read_inventory_and_no_dtn_shell(self):
        result = kit.doctor(self.config, remote=True)
        self.assertEqual(result["control"]["status"], "complete")
        self.assertEqual(result["data"]["remote_capability"], "unverified")
        calls = self.calls()
        self.assertEqual(len(calls), 5)
        self.assertEqual(list(self.private.iterdir()), [])
        for call in calls:
            self.assertIn("-oProxyCommand=false", call)
            if "sherlock-dtn" in call:
                self.assertEqual(call[-1], "sherlock-dtn")
                self.assertIn("-O", call)
            self.assertFalse(any("sbatch" in arg for arg in call))

    def test_remote_doctor_no_master_and_malformed_capability(self):
        os.environ["FAKE_SSH_MODE"] = "no-master"
        result = kit.doctor(self.config, remote=True)
        self.assertEqual(result["control"]["status"], "auth_required")
        self.assertEqual(len(self.calls()), 2)
        self.record.unlink()
        os.environ["FAKE_SSH_MODE"] = "malformed"
        result = kit.doctor(self.config, remote=True)
        self.assertEqual(result["control"]["checks"]["slurm_version"]["status"], "unsupported_capability")

    def test_doctor_honors_backoff_read_only(self):
        os.environ["FAKE_SSH_MODE"] = "auth"
        kit.run_remote(self.config, ["hostname"])
        path = Path(self.config.backoff_file)
        before = path.read_bytes(), path.stat().st_mtime_ns
        calls = len(self.calls())
        result = kit.doctor(self.config, remote=True)
        self.assertEqual(result["control"]["status"], "auth_required")
        self.assertEqual(len(self.calls()), calls)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_policy_digest_and_projection_drift(self):
        policy = kit.policy_text()
        identity = kit.policy_identity()
        self.assertEqual(identity["policy_sha256"], hashlib.sha256((ROOT / "SHERLOCK.md").read_bytes()).hexdigest())
        self.assertEqual(policy, (ROOT / "SHERLOCK.md").read_text())
        self.assertEqual(identity["install_mode"], "development")
        projection = kit.policy_projection()
        self.assertEqual(projection.count(kit.BEGIN), 1)
        self.assertIn(identity["policy_sha256"], projection)
        instruction = self.root / "instructions.md"
        instruction.write_text("Unrelated content\n" + projection + "Other content\n")
        report = kit.doctor(self.config, claude_instructions=instruction)
        self.assertEqual(report["instructions"]["claude"], "complete")
        instruction.write_text(projection.replace("60 seconds", "10 seconds"))
        self.assertEqual(kit.doctor(self.config, claude_instructions=instruction)["instructions"]["claude"], "configuration_mismatch")
        instruction.write_text(projection + projection)
        self.assertEqual(kit.doctor(self.config, claude_instructions=instruction)["instructions"]["claude"], "configuration_mismatch")

    def test_pin_mismatch_is_visible(self):
        pin = self.root / "pin.json"
        pin.write_text(json.dumps({**kit.policy_identity(), "code_revision": "0" * 40}))
        self.assertEqual(kit.doctor(self.config, advertised_identity=pin)["advertised_identity"], "configuration_mismatch")

    def test_cli_no_submit_stub(self):
        with self.assertRaises(SystemExit), patch("sys.stderr", new_callable=io.StringIO):
            kit.main(["submit"])
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(kit.main(["policy", "--identity"]), 0)
            self.assertEqual(json.loads(output.getvalue())["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
