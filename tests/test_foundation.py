"""Offline behavioral boundaries for the policy and transport foundation."""
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import sherlock_kit as kit
import sherlock_partitions as partitions
from sherlock_artifacts import TransferError, build_manifest
from sherlock_orchestration import SafetyError

PRODUCER = dict(attempt="1" * 32, cluster="sherlock", principal="fixture", code_digest="a" * 64,
                input_digest="a" * 64, runtime_digest="a" * 64, policy_digest="a" * 64)


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

    def test_two_consumers_share_one_authentication_attempt(self):
        os.environ["FAKE_SSH_MODE"] = "auth"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        script = (
            "import json,sys; import sherlock_kit as k; "
            "c=k.TransportConfig(ssh_binary=sys.argv[1],backoff_file=sys.argv[2]); "
            "print(json.dumps(k.run_remote(c,['hostname']).__dict__))"
        )
        processes = [subprocess.Popen([sys.executable, "-c", script, str(self.fake), str(self.config.backoff_file)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                     for _ in range(2)]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            results.append(json.loads(stdout))
        self.assertEqual([result["status"] for result in results], ["auth_required", "auth_required"])
        self.assertEqual(sum(result["dispatched"] for result in results), 1)
        self.assertEqual(len(self.calls()), 1)

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

    def state_environment(self):
        home = self.root / "temporary-home"
        home.mkdir(mode=0o700, exist_ok=True)
        env = {**os.environ, "HOME": str(home), "XDG_STATE_HOME": str(self.root / "xdg-state")}
        env.pop("SHERLOCK_KIT_STATE_ROOT", None)
        return env

    def test_state_root_env_shares_auth_cooldown_without_home_or_xdg_writes(self):
        env = self.state_environment()
        state = self.root / "shared-state"
        env.update(SHERLOCK_KIT_STATE_ROOT=str(state), FAKE_SSH_MODE="auth")
        with patch.dict(os.environ, env, clear=True):
            config = kit.TransportConfig(ssh_binary=str(self.fake))
            self.assertEqual(kit.run_remote(config, ["hostname"]).status, "auth_required")
            other = kit.TransportConfig(control_host="other-login", ssh_binary=str(self.fake))
            held = kit.run_remote(other, ["hostname"], mutation=True)
            self.assertFalse(held.dispatched)
            self.assertEqual(held.status, "auth_required")
            path = state / "auth-backoff.json"
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in state.iterdir()}
            self.assertEqual(kit.doctor(config, remote=True)["control"]["status"], "auth_required")
            self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in state.iterdir()}, before)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(Path(env["HOME"]).iterdir()), [])
        self.assertFalse(Path(env["XDG_STATE_HOME"]).exists())

    def test_explicit_backoff_has_priority_even_over_invalid_env_root(self):
        env = self.state_environment()
        env.update(SHERLOCK_KIT_STATE_ROOT="relative-root", FAKE_SSH_MODE="auth")
        with patch.dict(os.environ, env, clear=True):
            config = kit.TransportConfig(ssh_binary=str(self.fake), backoff_file=self.config.backoff_file)
            self.assertEqual(kit.run_remote(config, ["hostname"]).status, "auth_required")
        self.assertTrue(Path(self.config.backoff_file).is_file())
        self.assertEqual(list(Path(env["HOME"]).iterdir()), [])
        self.assertFalse(Path(env["XDG_STATE_HOME"]).exists())

    def test_unset_state_root_preserves_xdg_and_home_defaults(self):
        env = self.state_environment()
        env["FAKE_SSH_MODE"] = "auth"
        with patch.dict(os.environ, env, clear=True):
            config = kit.TransportConfig(ssh_binary=str(self.fake))
            self.assertEqual(kit.run_remote(config, ["hostname"]).status, "auth_required")
        self.assertTrue((Path(env["XDG_STATE_HOME"]) / "sherlock-kit/auth-backoff.json").is_file())
        self.assertEqual(list(Path(env["HOME"]).iterdir()), [])
        env.pop("XDG_STATE_HOME")
        with patch.dict(os.environ, env, clear=True):
            config = kit.TransportConfig(ssh_binary=str(self.fake))
            self.assertEqual(kit.run_remote(config, ["hostname"]).status, "auth_required")
        self.assertTrue((Path(env["HOME"]) / ".local/state/sherlock-kit/auth-backoff.json").is_file())

    def test_invalid_env_root_never_falls_back_or_dispatches(self):
        env = self.state_environment()
        state = self.root / "never-created"
        env["SHERLOCK_KIT_STATE_ROOT"] = str(state)
        with patch.dict(os.environ, env, clear=True):
            config = kit.TransportConfig(ssh_binary=str(self.fake))
            for value in ("", "relative", "~/state", str(state) + "\n", str(state) + "\t",
                          str(state) + "\x1b", str(state) + "\x7f", str(state / "../other")):
                with self.subTest(value=value), patch.dict(os.environ, {"SHERLOCK_KIT_STATE_ROOT": value}):
                    with self.assertRaises(ValueError):
                        kit.TransportConfig(ssh_binary=str(self.fake))
                    result = kit.run_remote(config, ["mutating-program"], mutation=True)
                    self.assertEqual(result.status, "not_sent")
                    self.assertFalse(result.dispatched)
                    for remote in (False, True):
                        self.assertEqual(kit.doctor(config, remote=remote)["control"]["status"], "configuration_mismatch")
        self.assertEqual(self.calls(), [])
        self.assertFalse(state.exists())
        self.assertFalse(Path(env["XDG_STATE_HOME"]).exists())
        self.assertEqual(list(Path(env["HOME"]).iterdir()), [])

    def test_cli_doctor_invalid_state_root_exits_cleanly_without_writes(self):
        env = self.state_environment()
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PATH"] = str(self.root) + os.pathsep + env["PATH"]
        public = self.root / "public-doctor"
        public.mkdir(mode=0o755)
        for value in ("", "relative", str(self.root / "../other"), str(public)):
            for remote in ([], ["--remote"]):
                with self.subTest(root=value, remote=remote):
                    result = subprocess.run([sys.executable, "-m", "sherlock_kit", "doctor", *remote],
                        env={**env, "SHERLOCK_KIT_STATE_ROOT": value}, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 2)
                    self.assertTrue(result.stderr.startswith("shk:"), result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertEqual(result.stdout, "")
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(public.iterdir()), [])
        self.assertFalse(Path(env["XDG_STATE_HOME"]).exists())
        self.assertEqual(list(Path(env["HOME"]).iterdir()), [])

    def test_local_doctor_needs_no_posix_ownership_api(self):
        with patch.object(kit.os, "getuid", None):
            config = kit.TransportConfig(ssh_binary=str(self.fake), backoff_file=self.config.backoff_file)
            self.assertEqual(kit.doctor(config)["mode"], "local")
            self.assertEqual(self.calls(), [])
            path = Path(config.backoff_file)
            path.write_text(json.dumps({"schema_version": 1, "retry_after": 0}))
            path.chmod(0o600)
            self.assertEqual(kit.doctor(config, remote=True)["control"]["status"], "configuration_mismatch")
            self.assertEqual(self.calls(), [])

    def test_state_root_refuses_links_and_public_parent_before_mkdir(self):
        env = self.state_environment()
        target = self.root / "untouched"
        target.mkdir(mode=0o700)
        linked = self.root / "linked"
        linked.symlink_to(target, target_is_directory=True)
        public = self.root / "public"
        public.mkdir(mode=0o755)
        for value in (linked, linked / "new-root", public):
            with self.subTest(root=value), patch.dict(os.environ, {**env, "SHERLOCK_KIT_STATE_ROOT": str(value)}, clear=True):
                with self.assertRaises(ValueError):
                    kit.TransportConfig(ssh_binary=str(self.fake))
        with self.assertRaises(ValueError):
            kit.TransportConfig(ssh_binary=str(self.fake), backoff_file=linked / "nested/backoff.json")
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(list(public.iterdir()), [])
        self.assertEqual(self.calls(), [])

    def test_doctor_with_env_root_does_not_create_auth_state(self):
        env = self.state_environment()
        state = self.root / "diagnostic-state"
        env["SHERLOCK_KIT_STATE_ROOT"] = str(state)
        with patch.dict(os.environ, env, clear=True):
            config = kit.TransportConfig(ssh_binary=str(self.fake))
            self.assertEqual(kit.doctor(config)["mode"], "local")
            self.assertEqual(self.calls(), [])
            self.assertEqual(kit.doctor(config, remote=True)["control"]["status"], "complete")
        self.assertFalse(state.exists())
        self.assertFalse(Path(env["XDG_STATE_HOME"]).exists())
        self.assertEqual(list(Path(env["HOME"]).iterdir()), [])

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

    def test_cli_routes_occupancy_to_typed_operations(self):
        arguments = ["occupancy", "--config", "private.json", "--partition", "normal"]
        with patch("sherlock_commands.main", return_value=7) as operations:
            self.assertEqual(kit.main(arguments), 7)
        operations.assert_called_once_with(arguments)

    def test_remote_program_permission_denied_never_arms_cooldown(self):
        """A remote program's permission failure is relayed text, not a transport authentication failure."""
        expected = {("remote-denied", False): ("failed", 1), ("remote-denied", True): ("unknown", 1),
                    ("relayed-denied", False): ("complete", 0), ("relayed-denied", True): ("complete", 0)}
        for (mode, mutation), (status, returncode) in expected.items():
            with self.subTest(mode=mode, mutation=mutation):
                os.environ["FAKE_SSH_MODE"] = mode
                result = kit.run_remote(self.config, ["python3", "-c", "synthetic"], mutation=mutation)
                self.assertEqual((result.status, result.returncode), (status, returncode))
                self.assertTrue(result.dispatched)
                self.assertTrue(kit.auth_failure(result.stderr))
                if mode == "relayed-denied":
                    self.assertTrue(result.stdout.startswith("SHK_UNKNOWN:"))
                self.assertFalse(Path(self.config.backoff_file).exists())
                self.assertFalse(kit.cooldown_active(self.config))
        os.environ["FAKE_SSH_MODE"] = "echo"
        self.assertEqual(kit.run_remote(self.config, ["hostname"]).status, "complete")
        self.assertEqual(len(self.calls()), 5)

    def test_transport_auth_failure_arms_cooldown_only_at_exit_255(self):
        os.environ["FAKE_SSH_MODE"] = "auth"
        result = kit.run_remote(self.config, ["hostname"])
        self.assertEqual((result.status, result.returncode), ("auth_required", 255))
        self.assertTrue(kit.cooldown_active(self.config))

    def test_auth_failure_record_and_cooldown_public_api(self):
        for text in ("Permission denied (publickey,keyboard-interactive).", "Too many authentication failures",
                     "rsync: connection unexpectedly closed\nKerberos credentials cache not found"):
            self.assertTrue(kit.auth_failure(text), text)
        for text in ("Connection to host closed", "protocol version mismatch -- is your shell clean?", ""):
            self.assertFalse(kit.auth_failure(text), text)
        self.assertFalse(kit.cooldown_active(self.config))
        self.assertTrue(kit.record_auth_failure(self.config))
        path = Path(self.config.backoff_file)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        payload = json.loads(path.read_text())
        self.assertEqual(payload["schema_version"], 1)
        self.assertGreater(payload["retry_after"], time.time())
        self.assertTrue(kit.cooldown_active(self.config))
        held = kit.run_remote(self.config, ["hostname"], mutation=True)
        self.assertEqual((held.status, held.dispatched), ("auth_required", False))
        self.assertEqual(self.calls(), [])
        # Fail closed: malformed or unsafe state counts as an active cooldown and cannot be armed.
        path.write_text("[]")
        path.chmod(0o600)
        self.assertTrue(kit.cooldown_active(self.config))
        unsafe = self.root / "unsafe"
        unsafe.mkdir(mode=0o700)
        config = kit.TransportConfig(ssh_binary=str(self.fake), backoff_file=unsafe / "backoff.json")
        unsafe.chmod(0o755)
        self.assertTrue(kit.cooldown_active(config))
        self.assertFalse(kit.record_auth_failure(config))
        self.assertEqual(list(unsafe.iterdir()), [])

    def bundle(self):
        source = self.root / "bundle"
        source.mkdir()
        (source / "result.json").write_text('{"value":42}\n')
        stage = self.root / "stage"
        stage.mkdir(mode=0o700)
        return build_manifest(source, PRODUCER), stage

    def test_data_transfer_auth_failure_arms_shared_cooldown_before_second_dispatch(self):
        manifest, stage = self.bundle()
        os.environ["FAKE_SSH_MODE"] = "auth"
        with self.assertRaises(TransferError) as caught:
            kit.data_transfer(self.config, "/synthetic/output", stage, manifest)
        error = caught.exception
        self.assertIsInstance(error, SafetyError)
        self.assertEqual(error.returncode, 255)
        self.assertIn("permission denied", error.stderr_tail.lower())
        self.assertNotIn("\n", error.stderr_tail)
        self.assertNotIn(".shk-files-", str(error))
        self.assertTrue(kit.cooldown_active(self.config))
        self.assertEqual(stat.S_IMODE(Path(self.config.backoff_file).stat().st_mode), 0o600)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        # The data host, not the control host, is addressed with the bounded prefix;
        # --protect-args keeps the remote path out of the remote shell's argv.
        self.assertEqual(calls[0][calls[0].index("sherlock-dtn") + 1:][:3], ["rsync", "--server", "--sender"])
        self.assertNotIn("sherlock-plain", calls[0])
        for flag in ("-T", "-oBatchMode=yes", "-oConnectTimeout=15", "-oStrictHostKeyChecking=yes"):
            self.assertIn(flag, calls[0])
        with self.assertRaisesRegex(SafetyError, "cooldown"):
            kit.data_transfer(self.config, "/synthetic/output", stage, manifest)
        held = kit.run_remote(self.config, ["hostname"])
        self.assertEqual((held.status, held.dispatched), ("auth_required", False))
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".shk-files-")], [])

    def test_data_transfer_non_auth_failures_do_not_arm_cooldown(self):
        manifest, stage = self.bundle()
        for mode, returncode in (("disconnect", 255), ("echo", 2)):
            with self.subTest(mode=mode):
                os.environ["FAKE_SSH_MODE"] = mode
                with self.assertRaises(TransferError) as caught:
                    kit.data_transfer(self.config, "/synthetic/output", stage, manifest)
                self.assertEqual(caught.exception.returncode, returncode)
                self.assertFalse(kit.auth_failure(caught.exception.stderr_tail))
                self.assertFalse(Path(self.config.backoff_file).exists())
                self.assertFalse(kit.cooldown_active(self.config))
        self.assertEqual(len(self.calls()), 2)
        for source in ("relative/output", "/output\nnext", "", 42):
            with self.subTest(source=source), self.assertRaises(SafetyError):
                kit.data_transfer(self.config, source, stage, manifest)
        with self.assertRaises(SafetyError):
            kit.data_transfer("sherlock-dtn", "/synthetic/output", stage, manifest)
        self.assertEqual(len(self.calls()), 2)

    def test_policy_identity_carries_partitions_hash_and_metadata_line_unchanged(self):
        identity = kit.policy_identity()
        expected = hashlib.sha256((ROOT / "src/sherlock_kit_data/partitions.json").read_bytes()).hexdigest()
        self.assertEqual(identity["partitions_sha256"], expected)
        self.assertEqual(identity["partitions_sha256"], partitions.partitions_sha256())
        self.assertEqual(set(identity), {"schema_version", "code_revision", "policy_sha256", "install_mode", "partitions_sha256"})
        lines = kit.policy_projection().splitlines()
        self.assertEqual(lines[0], kit.BEGIN)
        self.assertEqual(lines[1], f"<!-- source: SHERLOCK.md; schema_version: 1; policy_sha256: {identity['policy_sha256']} -->")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(kit.main(["policy", "--identity"]), 0)
        self.assertEqual(json.loads(output.getvalue())["partitions_sha256"], expected)

    def frozen_install(self, overrides=None, drop=()):
        """Simulate a frozen install: packaged data without a source checkout."""
        data = self.root / "frozen-data"
        data.mkdir(exist_ok=True)
        (data / "SHERLOCK.md").write_bytes((ROOT / "SHERLOCK.md").read_bytes())
        identity = {"schema_version": 1, "code_revision": "f" * 40, "install_mode": "frozen",
                    "policy_sha256": hashlib.sha256((ROOT / "SHERLOCK.md").read_bytes()).hexdigest(),
                    "partitions_sha256": partitions.partitions_sha256(), **(overrides or {})}
        for key in drop:
            identity.pop(key)
        (data / "build_identity.json").write_text(json.dumps(identity))
        return identity, patch.multiple(kit, _source_root=lambda: None, resources=SimpleNamespace(files=lambda package: data))

    def test_frozen_identity_validates_partitions_hash(self):
        identity, frozen = self.frozen_install()
        with frozen:
            self.assertEqual(kit.policy_identity(), identity)
        for case, options in (("mismatch", {"overrides": {"partitions_sha256": "0" * 64}}), ("missing", {"drop": ("partitions_sha256",)})):
            with self.subTest(case=case):
                _, frozen = self.frozen_install(**options)
                with frozen, self.assertRaisesRegex(ValueError, "partition"):
                    kit.policy_identity()

    def test_pin_partitions_hash_compared_only_when_advertised(self):
        identity, frozen = self.frozen_install()
        legacy = {key: value for key, value in identity.items() if key != "partitions_sha256"}
        cases = {"complete": (identity, "complete"), "legacy": (legacy, "complete"),
                 "mismatch": ({**identity, "partitions_sha256": "0" * 64}, "configuration_mismatch"),
                 "malformed": ({**identity, "partitions_sha256": 7}, "configuration_mismatch")}
        pin = self.root / "pin.json"
        with frozen:
            for name, (payload, expected) in cases.items():
                with self.subTest(pin=name):
                    pin.write_text(json.dumps(payload))
                    self.assertEqual(kit.doctor(self.config, advertised_identity=pin)["advertised_identity"], expected)


if __name__ == "__main__":
    unittest.main()
