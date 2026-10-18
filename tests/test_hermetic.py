"""The suite stays offline inside a Sherlock job: importing fakes (both entry points do) scrubs
the inherited scheduler environment, puts blocking Slurm clients on PATH behind the per-test
fakes, and refuses a TMPDIR that cannot execute the fakes written there."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fakes import SRC, TRIPWIRE
from fakes.fakeslurm import BIN, FakeSlurm

TESTS = Path(__file__).resolve().parent
# A Sherlock array element's environment (synthetic values), plus what users export.
JOB_ENV = {
    "SLURM_JOB_ID": "900002",
    "SLURM_JOBID": "900002",
    "SLURM_ARRAY_JOB_ID": "900001",
    "SLURM_ARRAY_TASK_ID": "0",
    "SLURM_CLUSTER_NAME": "sherlock",
    "SLURM_CONF": "/synthetic/slurm.conf",
    "SLURM_JOB_NAME": "shk.demo.probe.0123456789",
    "SBATCH_ACCOUNT": "leaky",
    "SQUEUE_FORMAT": "%.18i %.9P",
    "SACCT_FORMAT": "JobID,State",
    "SCANCEL_INTERACTIVE": "1",
    "SHK_EXECUTOR": "local",
    "SHK_NAME": "shk.demo.probe.0123456789",
    "SHERLOCK_KIT_STATE_ROOT": "/synthetic/state",
}
# The probe fails as execve does on a noexec mount (EACCES).
NOEXEC = (
    "import subprocess\n"
    "def denied(argv, *args, **kwargs):\n"
    "    raise PermissionError(13, 'Permission denied', argv[0])\n"
    "subprocess.run = denied\n"
    "import fakes\n"
)


def child(code, **env):
    """Run code in a new interpreter that imports fakes from tests/, as the suite does."""
    path = os.pathsep.join([str(TESTS), str(SRC)])
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        env={**os.environ, "PYTHONPATH": path, **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def noexec_directory():
    """A writable directory on a noexec mount, if this machine has one (never mounts one)."""
    for name in ("/dev/shm", "/run/lock", os.environ.get("XDG_RUNTIME_DIR")):
        writable = name and os.path.isdir(name) and os.access(name, os.W_OK | os.X_OK)
        if writable and os.statvfs(name).f_flag & os.ST_NOEXEC:
            return name
    return None


class HermeticSuite(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def real_slurm(self, *tools):
        """System-looking clients that record each call; only the tripwire may answer instead."""
        real = self.tmp / "real-slurm"
        real.mkdir()
        for tool in tools:
            (real / tool).write_text(f'#!/bin/sh\necho "{tool} $*" >>"{real}/calls"\n')
            (real / tool).chmod(0o755)
        return real

    def test_inherited_scheduler_kit_and_function_environment_is_scrubbed(self):
        injected = {**JOB_ENV, "BASH_FUNC_squeue%%": "() { :; }"}
        result = child(
            "import json, os, fakes; print(json.dumps(sorted(os.environ)))",
            **injected,
            SHK_REQUIRE_ALL="1",
            SHK_BUILD_PYTHON="/synthetic/python",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        names = set(json.loads(result.stdout))
        self.assertEqual(names & set(injected), set())
        self.assertLessEqual({"SHK_REQUIRE_ALL", "SHK_BUILD_PYTHON"}, names)

    def test_real_clients_are_blocked_through_exported_functions_and_path_fallthrough(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("needs bash")
        shim = self.tmp / "bash-as-sh"  # Sherlock's sh is bash, which imports functions
        shim.mkdir()
        (shim / "sh").symlink_to(bash)
        real = self.real_slurm("squeue", "scancel", "scontrol")
        code = (
            "import json, fakes\n"
            "from sherlock_kit import executor\n"
            "local = executor.LocalExecutor()\n"
            "cancel = executor.run_program(local, 'cancel', ['shk.demo.t.0123456789'])\n"
            "job = executor.run_program(local, 'allocation', ['job', '44'])\n"
            "print(json.dumps([cancel.stdout, job.stdout, job.stderr, fakes.tripwire_hits()]))\n"
        )
        result = child(
            code,
            **JOB_ENV,
            PATH=f"{shim}{os.pathsep}{os.environ['PATH']}{os.pathsep}{real}",
            **{"BASH_FUNC_squeue%%": f'() {{ "{real}/squeue" "$@"; }}'},
        )
        calls = real / "calls"
        self.assertEqual(calls.read_text() if calls.exists() else "", "")
        self.assertEqual(result.returncode, 0, result.stderr)
        cancel, job, error, hits = json.loads(result.stdout)
        self.assertEqual(cancel, "SHK_REFUSED squeue\n")
        self.assertIn("SHK_RC 97\n", job)
        self.assertIn("blocked the real Slurm client scontrol", error)
        self.assertEqual(
            [hit.split()[:2] for hit in hits], [["squeue", "-h"], ["scontrol", "show"]]
        )

    def test_per_test_fakes_stay_ahead_of_the_tripwire(self):
        path = FakeSlurm(self.tmp).env()["PATH"]
        self.assertEqual(shutil.which("squeue", path=path), str(BIN / "squeue"))
        self.assertEqual(shutil.which("scontrol", path=path), str(TRIPWIRE / "scontrol"))

    def test_a_tmpdir_that_cannot_execute_is_refused(self):
        cases = [("simulated", NOEXEC, {})]
        mount = noexec_directory()
        if mount:
            cases.append((mount, "import fakes", {"TMPDIR": mount}))
        for label, code, env in cases:
            with self.subTest(label):
                result = child(code, **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("cannot execute files", result.stderr)


if __name__ == "__main__":
    unittest.main()
