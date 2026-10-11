"""Transport: literal argv, master-only preflight, status mapping, detection."""
from contextlib import suppress
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fakes import SRC, install_fake_ssh

sys.path.insert(0, str(SRC))
from sherlock_kit import config, executor  # noqa: E402
from sherlock_kit.errors import ShkError  # noqa: E402

PREFIX = ["-T", "-oBatchMode=yes", "-oRemoteCommand=none", "-oConnectTimeout=15", "-oConnectionAttempts=1",
          "-oStrictHostKeyChecking=yes", "-oUpdateHostKeys=no", "-oControlMaster=no",
          "-oPermitLocalCommand=no", "-oClearAllForwardings=yes", "-oForwardAgent=no", "-oProxyCommand=false"]
CHECK = ["-O", "check", "-oBatchMode=yes"]
ARGV = ["printf", "", "a b", "'quote'", "$(id)", "a;b", "new\nline", "*", "--flag"]


class SshExecutorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.fake = install_fake_ssh(temporary.name)
        self.ssh = executor.SshExecutor("sherlock-plain", ssh=str(self.fake.path), command_timeout=10)
        self.dtn = executor.SshExecutor("sherlock-dtn", data=True, ssh=str(self.fake.path))

    def knobs(self, **values):
        environment = patch.dict(os.environ, self.fake.env(**values))
        environment.start()
        self.addCleanup(environment.stop)

    def test_literal_argv_exact_prefix_and_banner_removed(self):
        self.knobs(FAKE_SSH_MODE="echo")
        result = self.ssh.run(ARGV)
        self.assertEqual((result.status, result.returncode, result.stderr), ("complete", 0, ""))
        self.assertEqual(json.loads(result.stdout), ARGV)
        check, dial = self.fake.calls()
        self.assertEqual(check, [*CHECK, "sherlock-plain"])
        self.assertEqual(dial[:-1], [*PREFIX, "sherlock-plain"])
        self.assertEqual(shlex.split(dial[-1]), ARGV)

    def test_no_master_never_dials(self):
        self.knobs(FAKE_SSH_MASTER="none")
        for ex, hint in ((self.ssh, "ssh sherlock-plain"), (self.dtn, "ssh -fN sherlock-dtn")):
            with self.subTest(host=ex.host), self.assertRaises(ShkError) as caught:
                ex.run(["hostname"], mutation=True)
            self.assertEqual((caught.exception.code, caught.exception.token), (4, "auth_required"))
            self.assertEqual(caught.exception.hint, hint)
        with self.assertRaises(ShkError):
            self.dtn.rsync_args("/scratch/x")
        self.assertTrue(self.fake.calls())
        self.assertTrue(all(call[:2] == ["-O", "check"] for call in self.fake.calls()))

    def test_unconfigured_master_does_not_ask_for_a_login(self):
        self.knobs(FAKE_SSH_MASTER="unconfigured")
        self.assertEqual(executor.master_status("sherlock-plain", str(self.fake.path))[0], "not_configured")
        with self.assertRaises(ShkError) as caught:
            self.ssh.run(["hostname"])
        self.assertEqual((caught.exception.code, caught.exception.token), (4, "unavailable"))
        self.assertIn("ControlPath", caught.exception.hint)
        self.assertIn("No ControlPath specified", caught.exception.message)
        self.assertTrue(all(call[:2] == ["-O", "check"] for call in self.fake.calls()))

    def test_missing_ssh_is_unreachable(self):
        ex = executor.SshExecutor("sherlock-plain", ssh=str(Path(self.fake.bin) / "absent"))
        with self.assertRaises(ShkError) as caught:
            ex.run(["hostname"])
        self.assertEqual((caught.exception.code, caught.exception.token), (4, "unavailable"))

    def test_status_mapping(self):
        cases = [("auth", False, "auth_required"), ("disconnect", False, "unavailable"),
                 ("disconnect", True, "unknown"), ("hang", False, "timeout"), ("hang", True, "unknown")]
        for mode, mutation, status in cases:
            with self.subTest(mode=mode, mutation=mutation):
                self.knobs(FAKE_SSH_MODE=mode, FAKE_SSH_HANG="5")
                self.assertEqual(self.ssh.run(["true"], mutation=mutation, timeout=0.5).status, status)
        self.knobs(FAKE_SSH_MODE="exec")
        failed = self.ssh.run(["sh", "-c", "echo boom >&2; exit 3"])
        self.assertEqual((failed.status, failed.returncode, failed.stderr), ("failed", 3, "boom\n"))
        dials = [call for call in self.fake.calls() if "-O" not in call]
        self.assertEqual(len(dials), len(cases) + 1)

    def test_stdin_and_run_program(self):
        self.knobs(FAKE_SSH_MODE="exec")
        self.assertEqual(self.ssh.run(["wc", "-c"], stdin=b"abc\ndef").stdout.strip(), "7")
        self.assertEqual(self.ssh.run(["cat"], stdin="text").stdout, "text")
        result = executor.run_program(self.ssh, "doctor", [])
        self.assertEqual(result.status, "complete")
        self.assertIn("SHK_END", result.stdout.splitlines())
        words = shlex.split(self.fake.calls()[-1][-1])
        self.assertEqual((words[:2], words[3]), (["sh", "-c"], "shk-doctor"))
        self.assertEqual(words[2], executor.program_text("doctor"))

    def test_rsync_args(self):
        self.knobs()
        options, target = self.dtn.rsync_args("/scratch/users/u/a b")
        self.assertEqual(options, ["-e", shlex.join([str(self.fake.path), *PREFIX])])
        self.assertEqual(target, "sherlock-dtn:/scratch/users/u/a b")
        for bad in ("relative", "/x\ny", "/x\0y"):
            with self.subTest(path=bad), self.assertRaises(ShkError):
                self.dtn.rsync_args(bad)

    def test_invalid_input_is_refused_before_dialing(self):
        self.knobs()
        for argv in ("hostname", [], [""], ["-x"], ["a\0"], [1]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                self.ssh.run(argv)
        for host in ("-oProxyCommand=x", "a b", "", "host:22"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                executor.SshExecutor(host)
        self.assertEqual(self.fake.calls(), [])


@unittest.skipUnless(shutil.which("ssh"), "OpenSSH client not installed")
class RealOpenSshTests(unittest.TestCase):
    """Real OpenSSH, no network: -F points at a temporary config whose ProxyCommand only leaves a marker."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)
        self.marker = self.tmp / "cold-dial"
        (self.tmp / "config").write_text(
            "Host sherlock-plain\n  HostName shk-test.invalid\n  ControlPath /nonexistent-shk-test/%C\n"
            f"  ProxyCommand /bin/sh -c 'echo cold > {self.marker}; exit 1'\n"
            "Host no-control-path\n  HostName shk-test.invalid\n", encoding="utf-8")

    def wrapper(self, name, claim_master):
        path = self.tmp / name
        check = 'case " $* " in *" -O check "*) exit 0;; esac\n' if claim_master else ""
        path.write_text(f'#!/bin/sh\n{check}exec {shlex.quote(shutil.which("ssh"))} -F '
                        f'{shlex.quote(str(self.tmp / "config"))} "$@"\n', encoding="utf-8")
        path.chmod(0o755)
        return str(path)

    def test_master_gone_after_check_never_cold_dials(self):
        ex = executor.SshExecutor("sherlock-plain", ssh=self.wrapper("stale", True), command_timeout=20)
        for mutation, status in ((False, "unavailable"), (True, "unknown")):
            with self.subTest(mutation=mutation):
                self.assertEqual(ex.run(["true"], mutation=mutation).status, status)
                self.assertFalse(self.marker.exists(), "ssh fell back to a fresh connection")

    def test_master_states_from_the_real_client(self):
        ssh = self.wrapper("plain", False)
        self.assertEqual(executor.master_status("sherlock-plain", ssh)[0], "none")
        state, detail = executor.master_status("no-control-path", ssh)
        self.assertEqual(state, "not_configured")
        self.assertIn("ControlPath", detail)
        self.assertFalse(self.marker.exists())


def _alive(pid: int) -> bool:
    """True while pid runs; a zombie (killed, not yet reaped) or a vanished pid is not alive.

    /proc alone decides: kill(pid, 0) also succeeds for a zombie, which systemd may reap
    between two checks. Without procfs a zombie counts as alive until it is reaped.
    """
    try:
        if not Path("/proc/self").is_dir():
            return os.kill(pid, 0) is None
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] not in ("Z", "X")
    except (FileNotFoundError, ProcessLookupError):
        return False


def _describe(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().strip()
    except OSError as exc:
        return f"no /proc entry ({exc})"


def _kill(pid: int) -> None:
    with suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


class LocalExecutorTests(unittest.TestCase):
    def test_local_runs_argv_directly(self):
        ex = executor.LocalExecutor(command_timeout=10)
        self.assertEqual(ex.kind, "local")
        self.assertEqual(ex.run(["sh", "-c", 'printf %s "$1"', "x", "a b"]).stdout, "a b")
        self.assertEqual(ex.run(["sh", "-c", "exit 255"]).status, "failed")
        self.assertEqual(ex.run(["sh", "-c", "exit 1"], mutation=True).status, "unknown")
        self.assertEqual(ex.run(["sleep", "5"], timeout=0.3).status, "timeout")
        self.assertEqual(ex.run(["sleep", "5"], timeout=0.3, mutation=True).status, "unknown")
        self.assertEqual(ex.run(["definitely-not-a-command-shk"]).status, "unavailable")
        self.assertIn("SHK_END", executor.run_program(ex, "doctor").stdout)
        with self.assertRaises(ShkError) as caught:
            ex.rsync_args("/scratch/x")
        self.assertEqual(caught.exception.code, 3)

    def test_timeout_kills_the_whole_process_group(self):
        ex = executor.LocalExecutor(command_timeout=10)
        started = time.monotonic()
        result = ex.run(["sh", "-c", "sleep 30 & echo $!; wait; echo late"], timeout=0.5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result.status, "timeout")
        grandchild = int(result.stdout)
        self.addCleanup(_kill, grandchild)
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(grandchild), f"sleep outlived the timed-out sh -c: {_describe(grandchild)}")

    def test_check(self):
        result = executor.Result
        self.assertEqual(executor.check(result("complete", "x"), "read").stdout, "x")
        for status, code in (("auth_required", 4), ("unavailable", 4), ("timeout", 4), ("unknown", 5), ("failed", 1)):
            with self.subTest(status=status), self.assertRaises(ShkError) as caught:
                executor.check(result(status, "", "tail\n", 9), "status")
            self.assertEqual(caught.exception.code, code)


class DetectionTests(unittest.TestCase):
    def kind(self, hostname, override=None, **env):
        return executor.detect(None, override, env, hostname).kind

    def test_detection_order(self):
        self.assertEqual(self.kind("sh02-ln04.stanford.edu"), "local")
        self.assertEqual(self.kind("SH03-LN01"), "local")
        self.assertEqual(self.kind("sh02-ln04", "ssh"), "ssh")
        self.assertEqual(self.kind("lab-ubuntu", "local"), "local")
        self.assertEqual(self.kind("lab-ubuntu", "auto", SHK_EXECUTOR="local"), "local")
        self.assertEqual(self.kind("sh02-ln04", None, SHK_EXECUTOR="ssh"), "ssh")
        self.assertEqual(self.kind("sh02-12n05.int", SLURM_JOB_ID="1"), "local")
        self.assertEqual(self.kind("sh02-12n05.int"), "ssh")
        self.assertEqual(self.kind("anything", SLURM_CLUSTER_NAME="sherlock"), "local")
        self.assertEqual(self.kind("marlowe-n01", SLURM_JOB_ID="1", SLURM_CLUSTER_NAME="marlowe"), "ssh")
        self.assertEqual(self.kind("sh02-ln04x"), "ssh")
        self.assertEqual(self.kind("lab-ubuntu"), "ssh")
        self.assertEqual(self.kind("lab-ubuntu", SHK_EXECUTOR=" Local "), "local")
        self.assertEqual(self.kind("sh02-ln04", SHK_EXECUTOR=""), "local")
        with self.assertRaises(ShkError):
            self.kind("lab-ubuntu", SHK_EXECUTOR="remote")

    def test_transfers_never_ssh_from_sherlock(self):
        for override, env, hostname in (("ssh", {}, "sh03-ln01.stanford.edu"),
                                        (None, {"SHK_EXECUTOR": "ssh", "SLURM_CLUSTER_NAME": "sherlock"}, "x"),
                                        ("ssh", {"SLURM_JOB_ID": "7"}, "sh02-12n05")):
            with self.subTest(override=override, env=env, hostname=hostname):
                self.assertEqual(executor.detect(None, override, env, hostname).kind, "ssh")
                data = executor.detect(None, override, env, hostname, data=True)
                self.assertEqual(data.kind, "local")
                with self.assertRaises(ShkError) as caught:
                    data.rsync_args("/scratch/x")
                self.assertEqual((caught.exception.code, caught.exception.token), (3, "transfer_on_sherlock"))

    def test_hosts_and_timeouts_come_from_config(self):
        cfg = config.parse({"control_host": "c", "data_host": "d", "connect_timeout": 7, "command_timeout": 9}, None)
        control = executor.detect(cfg, "ssh", {}, "x")
        data = executor.detect(cfg, "ssh", {}, "x", data=True)
        self.assertEqual((control.host, control.data, control.connect_timeout, control.command_timeout),
                         ("c", False, 7, 9))
        self.assertEqual((data.host, data.data), ("d", True))
        self.assertEqual(executor.detect(None, "ssh", {}, "x").host, "sherlock-plain")


if __name__ == "__main__":
    unittest.main()
