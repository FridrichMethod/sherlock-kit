#!/usr/bin/env python3
"""Offline synthetic OpenSSH substitute; never contacts a network endpoint."""
import json
import os
from pathlib import Path
import shlex
import sys
import time

record = os.environ.get("FAKE_SSH_RECORD")
if record:
    with Path(record).open("a") as stream:
        stream.write(json.dumps(sys.argv[1:]) + "\n")
mode = os.environ.get("FAKE_SSH_MODE", "echo")
if mode == "auth":
    print("Permission denied (keyboard-interactive).", file=sys.stderr)
    raise SystemExit(255)
if mode == "disconnect":
    print("Connection to host closed", file=sys.stderr)
    raise SystemExit(255)
if mode == "timeout":
    time.sleep(5)
if "-O" in sys.argv:
    raise SystemExit(0 if mode != "no-master" else 255)
argv = shlex.split(sys.argv[-1])
if argv[:2] == ["sh", "-c"]:
    print("synthetic-login\nSLURM_JOB_ID=")
elif argv[0] == "cat":
    print("Synthetic installed site instructions")
elif argv == ["scontrol", "--version"]:
    print("broken output" if mode == "malformed" else "slurm 25.11.8")
else:
    print(json.dumps(argv))
