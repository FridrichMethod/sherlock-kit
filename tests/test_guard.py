"""Recognizable violation and false-positive boundaries; all checks are offline."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import sherlock_guard as guard


class GuardTests(unittest.TestCase):
    def test_high_frequency_sherlock_watch_variants(self):
        commands = (
            "ssh sherlock-plain 'watch -n2 squeue'",
            "ssh -T -oBatchMode=yes sherlock-plain 'watch -n 5 squeue -u synthetic'",
            "ssh -o BatchMode=yes synthetic@login.sherlock.stanford.edu watch --interval=59.9 sacct -j 123",
            "watch -n1 ssh sherlock squeue",
            "watch --interval 0.5 'ssh -T sherlock-plain sstat -j 123'",
            "ssh synthetic@sh02-ln04.sherlock.stanford.edu /usr/bin/watch -n2 /usr/bin/sinfo",
        )
        for command in commands:
            with self.subTest(command=command):
                decision = guard.decide(command)
                self.assertEqual(decision.status, "deny")
                self.assertEqual(decision.rule, "scheduler_poll_cadence")

    def test_known_helpers_only_in_executable_positions(self):
        for command in (
            "sherlock-scratch-purge-refresh",
            "/synthetic/bin/sherlock-scratch-purge-keepalive /synthetic/data",
            "python3 /synthetic/sherlock-scratch-purge-refresh",
            "ssh sherlock-plain 'bash /synthetic/sherlock-scratch-purge-keepalive'",
        ):
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command).rule, "scratch_purge_helper")
        self.assertEqual(guard.decide("python3 /synthetic/confirmed_purge.py", known_purge_helpers={"confirmed_purge.py"}).status, "deny")

    def test_false_positives_and_opaque_commands_are_unknown(self):
        commands = (
            "ssh sherlock-plain squeue -u synthetic",
            "ssh sherlock-plain 'watch -n60 squeue'",
            "watch -n60 ssh sherlock-plain squeue",
            "watch -n90 ssh sherlock-plain squeue",
            "ssh sherlock-plain 'watch squeue'",  # WATCH_INTERVAL is unobserved
            "watch ssh sherlock-plain squeue",
            "ssh another-site 'watch -n1 squeue'",
            "watch -n1 'ssh another-site squeue'",
            "watch -n1 date",
            "watch -n1 squeue",  # local host identity is not established
            "ssh sherlock-plain 'watch -n1 date'",
            "printf '%s' 'ssh sherlock-plain watch -n1 squeue'",
            "echo sherlock-scratch-purge-refresh",
            "cat /synthetic/sherlock-scratch-purge-keepalive",
            "python3 -c 'print(\"sherlock-scratch-purge-refresh\")'",
            "python3 /synthetic/scientific_scratch_refresh.py",
            "ssh sherlock-plain 'touch $SCRATCH/useful-output'",
            "ssh sherlock-plain 'cp output $SCRATCH/new-output'",
            "ssh sherlock-plain 'while true; do squeue; sleep 1; done'",
            "bash -c 'watch -n1 ssh sherlock-plain squeue'",
            "ssh sherlock-plain 'echo watch -n1 squeue'",
            "shk submit --help",
            "shk reconcile --help",
            "shk fetch --help",
            "sbatch synthetic.sbatch",
            "watch -n1 ssh -O check sherlock-plain",
            "ssh -L 9000:localhost:9000 sherlock-plain watch -n1 squeue",
            "watch -n1 ssh sherlock-plain squeue && echo additional",
            "watch -nNaN ssh sherlock-plain squeue",
            "watch -nInfinity ssh sherlock-plain squeue",
            "watch -nBAD ssh sherlock-plain squeue",
            "watch -n1 ssh sherlock-plain 'squeue; echo sample'",
            "ssh sherlock-plain 'watch -n1 squeue",  # malformed quoting
            "", None, ["ssh", "sherlock"], "a" * (guard.MAX_COMMAND_CHARS + 1),
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(guard.decide(command).status, "unknown")

    def test_both_client_adapters_share_portable_deny(self):
        command = "watch -n1 ssh sherlock-plain squeue"
        expected = None
        for client, tool, key in (("claude", "Bash", "command"), ("codex", "Bash", "command"),
                                  ("codex", "exec_command", "cmd")):
            payload = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {key: command}}
            output = guard.adapt_event(client, payload)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "PreToolUse")
            self.assertNotIn("updatedInput", output["hookSpecificOutput"])
            if expected is None:
                expected = output
            self.assertEqual(output, expected)

    def test_unrelated_events_and_tools_never_deny(self):
        command = "watch -n1 ssh sherlock-plain squeue"
        for payload in (None, [], {}, {"tool_name": "Bash", "tool_input": {"command": command}},
                        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": command}},
                        {"hook_event_name": "PreToolUse", "tool_name": "write_stdin", "tool_input": {"chars": command}},
                        {"hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": command}},
                        {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "bad schema"}):
            for client in ("claude", "codex"):
                self.assertEqual(guard.adapt_event(client, payload), {})

    def test_pure_decision_never_calls_execution_or_reads_files(self):
        with patch("subprocess.run", side_effect=AssertionError("executed command")), patch("builtins.open", side_effect=AssertionError("read local state")):
            self.assertEqual(guard.decide("watch -n1 ssh sherlock-plain squeue").status, "deny")
            self.assertEqual(guard.decide("python3 opaque.py").status, "unknown")

    def run_hook(self, payload):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        return subprocess.run([sys.executable, "-m", "sherlock_guard", "--client", "codex"],
                              input=payload, capture_output=True, cwd=ROOT, env=env, timeout=5)

    def test_real_hook_process_exit_zero_and_bounded_output(self):
        result = self.run_hook(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                          "tool_input": {"command": "watch -n1 ssh sherlock-plain squeue"}}).encode())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertLess(len(result.stdout), 1000)
        self.assertEqual(result.stderr, b"")

    def test_hook_malformed_oversized_and_unknown_are_not_approvals(self):
        for payload in (b"{bad", b"\xff", b"a" * (guard.MAX_INPUT_BYTES + 1), b"[]",
                        b"[" * 2000 + b"]" * 2000):
            result = self.run_hook(payload)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
        result = self.run_hook(json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                                          "tool_input": {"command": "shk doctor"}}).encode())
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")

    def test_adapter_package_has_one_owner_and_shared_engine(self):
        plugin_root = ROOT / "adapters/claude"
        plugin = json.loads((plugin_root / ".claude-plugin/plugin.json").read_text())
        self.assertNotIn("hooks", plugin)
        self.assertFalse((plugin_root / "hooks").exists())
        for client in ("claude", "codex"):
            skill = (ROOT / f"adapters/{client}/skills/sherlock-kit-operate/SKILL.md").read_text()
            self.assertTrue(skill.startswith("---\nname: sherlock-kit-operate\n"))
            self.assertIn("description:", skill.split("---", 2)[1])
            self.assertIn("shk policy", skill)
            self.assertIn("shk submit --help", skill)
            self.assertIn("shk reconcile --help", skill)
            self.assertIn("shk fetch --help", skill)
            self.assertNotIn("sbatch", skill)


if __name__ == "__main__":
    unittest.main()
