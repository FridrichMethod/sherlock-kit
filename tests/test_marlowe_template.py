"""The Marlowe skill's walltime job-script template, run under bash with a stub shk.

The block is taken from the skill as published: the only fenced block whose first line is
`#!/bin/bash` and that holds `requeue --self`. Its `shk=` line is pointed at a stub that
records its argv and whether the workload's checkpoint existed at the call. The stand-in
workload writes `started` once its TERM handler is installed (so the script's USR1 trap is
set); on TERM it writes `term`, holds its checkpoint until the test writes `release`, then
writes `ckpt` and exits, so a request made before the workload has exited finds no `ckpt`.
SIGUSR1 goes to the batch shell alone, as Slurm's `--signal B:USR1@T` sends it.
Synchronization is by marker files; a correct template passes whatever the timing.
"""

import contextlib
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from fakes import ROOT, SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import cli

SKILL = ROOT / "src/sherlock_kit/data/skills/sherlock-kit-marlowe/SKILL.md"
FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
NAME = "shk.demo-mw.train-a.0123456789"
REQUEUE = ["--site", "marlowe", "--executor", "local", "requeue", "--self", "--expect-name", NAME]
DEADLINE = 10.0
# The stub appends one line per call: 1 or 0 (did ckpt exist), then each argument after \037.
STUB = """#!/bin/sh
if [ -e ckpt ]; then printf 1 >>"$STUB_LOG"; else printf 0 >>"$STUB_LOG"; fi
for arg in "$@"; do printf '\\037%s' "$arg" >>"$STUB_LOG"; done
printf '\\n' >>"$STUB_LOG"
exit "${STUB_EXIT:-0}"
"""
WORK = """
import os, signal, sys, time
from pathlib import Path

def stop(signum, frame):
    Path("term").write_text("")
    while not Path("release").exists():
        time.sleep(0.01)
    Path("ckpt").write_text("ckpt\\n")
    sys.exit(0)

signal.signal(signal.SIGTERM, stop)
Path("started").write_text("")
if "WORK_EXIT" in os.environ:
    sys.exit(int(os.environ["WORK_EXIT"]))
while True:
    signal.pause()
"""


def template() -> str:
    blocks = [
        block
        for block in FENCE.findall(SKILL.read_text(encoding="utf-8"))
        if block.startswith("#!/bin/bash\n") and "requeue --self" in block
    ]
    if len(blocks) != 1:
        raise AssertionError(f"{len(blocks)} bash requeue templates in {SKILL.name}, want 1")
    return blocks[0]


class MarloweTemplateTests(unittest.TestCase):
    def setUp(self):
        self.bash = shutil.which("bash")
        self.assertIsNotNone(self.bash, "bash is a development prerequisite")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        tools = self.tmp / "tools"
        tools.mkdir()
        stub = tools / "shk"
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.work = tools / "work.py"
        self.work.write_text(WORK, encoding="utf-8")
        text = template()
        found = re.search(r"(?m)^max_restarts=(\d+)\b", text)
        self.assertIsNotNone(found, "the template sets max_restarts")
        self.max_restarts = int(found.group(1))
        script, count = re.subn(r"(?m)^shk=\S+", f"shk={shlex.quote(str(stub))}", text)
        self.assertEqual(count, 1, "one shk= assignment in the template")
        self.script = tools / "job.sh"
        self.script.write_text(script, encoding="utf-8")

    def start(self, case: str, restarts=None, **knobs) -> tuple[subprocess.Popen, Path]:
        """Run the script in a new run directory named case, as the batch shell of a job."""
        run = self.tmp / case
        run.mkdir()
        work = run / "work"
        work.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -I {shlex.quote(str(self.work))} "$@"\n',
            encoding="utf-8",
        )
        work.chmod(0o755)
        env = {"PATH": os.environ["PATH"], "SHK_NAME": NAME, "STUB_LOG": str(run / "calls")}
        if restarts is not None:
            env["SLURM_RESTART_COUNT"] = str(restarts)
        proc = subprocess.Popen(
            [self.bash, str(self.script)],
            cwd=run,
            env={**env, **knobs},
            start_new_session=True,
            stdin=subprocess.DEVNULL,
        )
        self.addCleanup(self.kill, proc)
        return proc, run

    def kill(self, proc: subprocess.Popen):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()

    def wait_for(self, marker: Path, proc: subprocess.Popen):
        """Poll every 10 ms until marker exists or the script has ended."""
        end = time.monotonic() + DEADLINE
        while not marker.exists() and proc.poll() is None:
            self.assertLess(time.monotonic(), end, f"no {marker.name}")
            time.sleep(0.01)

    def usr1_once_started(self, proc: subprocess.Popen, run: Path) -> int:
        """Send USR1 once the workload runs (after the script's trap is set); the exit status."""
        self.wait_for(run / "started", proc)
        self.assertTrue((run / "started").exists(), "the script ended before the workload")
        os.kill(proc.pid, signal.SIGUSR1)
        self.wait_for(run / "term", proc)
        (run / "release").write_text("", encoding="utf-8")
        return proc.wait(timeout=DEADLINE)

    def calls(self, run: Path) -> list[tuple[bool, list[str]]]:
        log = run / "calls"
        if not log.exists():
            return []
        lines = log.read_text(encoding="utf-8").splitlines()
        return [(line[0] == "1", line.split("\x1f")[1:]) for line in lines]

    def test_usr1_checkpoints_then_requeues_itself(self):
        for stub_exit in (0, 3):
            with self.subTest(stub_exit=stub_exit):
                proc, run = self.start(f"usr1-{stub_exit}", STUB_EXIT=str(stub_exit))
                code = self.usr1_once_started(proc, run)
                self.assertEqual(code, stub_exit, "the script exits with shk's status")
                [(checkpointed, argv)] = self.calls(run)
                self.assertTrue(checkpointed, "the checkpoint precedes the request")
                self.assertEqual(argv, REQUEUE)
                args = cli.build_parser().parse_args(argv)
                self.assertEqual(
                    (args.site, args.executor, args.command, args.self, args.expect_name),
                    ("marlowe", "local", "requeue", True, NAME),
                )
                self.assertIsNone(args.expect_token)

    def test_last_allowed_attempt_checkpoints_and_stops(self):
        proc, run = self.start("at-bound", restarts=self.max_restarts)
        self.assertEqual(self.usr1_once_started(proc, run), 1)
        self.assertEqual(self.calls(run), [])
        self.assertTrue((run / "ckpt").exists())

    def test_attempt_past_the_bound_stops_before_the_workload(self):
        proc, run = self.start("past-bound", restarts=self.max_restarts + 1)
        self.assertEqual(proc.wait(timeout=DEADLINE), 1)
        self.assertFalse((run / "started").exists())
        self.assertEqual(self.calls(run), [])

    def test_workload_that_ends_by_itself_sets_the_exit_status(self):
        for status in (0, 7):
            with self.subTest(status=status):
                proc, run = self.start(f"self-exit-{status}", WORK_EXIT=str(status))
                self.assertEqual(proc.wait(timeout=DEADLINE), status)
                self.assertTrue((run / "started").exists())
                self.assertEqual(self.calls(run), [])
                self.assertFalse((run / "ckpt").exists())


if __name__ == "__main__":
    unittest.main()
