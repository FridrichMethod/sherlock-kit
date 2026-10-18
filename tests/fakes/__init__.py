"""Shared offline test fakes, and the hermetic suite setup that importing them performs.

Every test module imports this package, so both entry points (``tests/run_suite.py`` and
``python3 -m unittest discover -s tests``) run the setup once, before any test:

- inherited scheduler and kit state is removed from ``os.environ``: ``SLURM_*``, ``SBATCH_*``,
  ``SQUEUE_*``, ``SACCT_*``, ``SCANCEL_*``, ``SHK_*`` (except the suite knobs
  ``SHK_BUILD_PYTHON`` and ``SHK_REQUIRE_ALL``), ``SHERLOCK_KIT_*`` and exported shell functions
  (``BASH_FUNC_*``, which bash, Sherlock's ``sh``, runs instead of a command on PATH);
- a tripwire of blocking stubs for every Slurm client is put first on PATH, so per-test fakes
  (prepended later) shadow it and it shadows the system clients. A stub records its call
  (``tripwire_hits()``) and exits ``TRIPWIRE_RC``; ``run_suite.py`` fails a run with hits;
- the tripwire is written under TMPDIR, where the tests write their fakes too, and must run
  there: a fake that cannot execute (a noexec mount) falls through to the next PATH entry, so
  a TMPDIR that cannot execute files is refused with RuntimeError.

``install_fake_ssh(directory)`` writes ``directory/fake-bin/ssh``, a wrapper that
runs ``fake_ssh.py`` with the test interpreter, and returns a ``FakeSsh``:
``.path`` (the wrapper), ``.bin`` (prepend to PATH), ``.env(**knobs)`` (PATH plus
FAKE_SSH_RECORD plus knobs such as FAKE_SSH_MODE / FAKE_SSH_MASTER /
FAKE_SSH_HANG, for ``unittest.mock.patch.dict(os.environ, ...)``) and
``.calls()`` (every recorded argv[1:], in order). ``SRC`` is the package source
root for tests that import ``sherlock_kit`` without installing it.
"""

import atexit
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FAKE_SSH = Path(__file__).with_name("fake_ssh.py")
SCRUBBED = (
    "SLURM_",
    "SBATCH_",
    "SQUEUE_",
    "SACCT_",
    "SCANCEL_",
    "SHK_",
    "SHERLOCK_KIT_",
    "BASH_FUNC_",
)
SUITE_KNOBS = ("SHK_BUILD_PYTHON", "SHK_REQUIRE_ALL")
SLURM_CLIENTS = (
    "sacct",
    "sacctmgr",
    "salloc",
    "sattach",
    "sbatch",
    "sbcast",
    "scancel",
    "scontrol",
    "scrontab",
    "sdiag",
    "sinfo",
    "sprio",
    "squeue",
    "sreport",
    "srun",
    "sshare",
    "sstat",
    "strigger",
)
TRIPWIRE_RC = 97


def _scrub():
    for name in list(os.environ):
        if name.startswith(SCRUBBED) and name not in SUITE_KNOBS:
            del os.environ[name]


def _tripwire():
    """Write the blocking clients under TMPDIR, check that they run, put them first on PATH."""
    tripwire = Path(tempfile.mkdtemp(prefix="shk-tests-")) / "tripwire"
    atexit.register(shutil.rmtree, tripwire.parent, True)
    log = tripwire.with_name("tripwire.log")
    tripwire.mkdir()
    for tool in SLURM_CLIENTS:
        (tripwire / tool).write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "{tool} $*" >>{shlex.quote(str(log))}\n'
            f'echo "sherlock-kit tests: blocked the real Slurm client {tool}; fake it" >&2\n'
            f"exit {TRIPWIRE_RC}\n",
            encoding="utf-8",
        )
        (tripwire / tool).chmod(0o755)
    try:
        code = subprocess.run([tripwire / "sbatch"], capture_output=True, check=False).returncode
    except OSError:
        code = None
    log.write_text("", encoding="utf-8")
    if code != TRIPWIRE_RC:
        raise RuntimeError(
            f"TMPDIR {tripwire.parent.parent} cannot execute files (a noexec mount?): the "
            "tests write fake ssh and Slurm tools there, and a fake that cannot run falls "
            "through to the real client on PATH; set TMPDIR to a directory that allows execution"
        )
    os.environ["PATH"] = f"{tripwire}{os.pathsep}{os.environ.get('PATH', '')}"
    return tripwire


def tripwire_hits() -> list:
    """The Slurm client calls the tripwire blocked so far, as "tool args" lines."""
    return TRIPWIRE.with_name("tripwire.log").read_text(encoding="utf-8").splitlines()


_scrub()
TRIPWIRE = _tripwire()


@dataclass(frozen=True)
class FakeSsh:
    bin: Path
    record: Path

    @property
    def path(self) -> Path:
        return self.bin / "ssh"

    def env(self, **knobs) -> dict:
        path = f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"
        return {"PATH": path, "FAKE_SSH_RECORD": str(self.record), **knobs}

    def calls(self) -> list:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]


def install_fake_ssh(directory) -> FakeSsh:
    bin_dir = Path(directory) / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "ssh"
    command = shlex.join([sys.executable, "-B", str(FAKE_SSH)])
    wrapper.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o755)
    return FakeSsh(bin_dir, Path(directory) / "ssh-calls.jsonl")
