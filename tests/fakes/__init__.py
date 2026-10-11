"""Shared offline test fakes.

``install_fake_ssh(directory)`` writes ``directory/fake-bin/ssh``, a wrapper that
runs ``fake_ssh.py`` with the test interpreter, and returns a ``FakeSsh``:
``.path`` (the wrapper), ``.bin`` (prepend to PATH), ``.env(**knobs)`` (PATH plus
FAKE_SSH_RECORD plus knobs such as FAKE_SSH_MODE / FAKE_SSH_MASTER /
FAKE_SSH_HANG, for ``unittest.mock.patch.dict(os.environ, ...)``) and
``.calls()`` (every recorded argv[1:], in order). ``SRC`` is the package source
root for tests that import ``sherlock_kit`` without installing it.
"""

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
FAKE_SSH = Path(__file__).with_name("fake_ssh.py")


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
