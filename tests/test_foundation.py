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


if __name__ == "__main__":
    unittest.main()
