"""Fake Slurm accounting for usage.sh: sacctmgr, sreport, sacct and id with strict argv checks.

``UsageSlurm(directory, user=..., assoc=..., sreport=..., sacct=..., rc=..., now_end=...)``
writes wrappers that run this file over a JSON state; ``.env()`` puts them first on PATH and
``.calls(tool)`` returns each call of a tool with its argv and the TZ, LC_ALL,
SLURM_TIME_FORMAT, SACCT_FORMAT and SLURM_CONF it saw. Modelled Slurm 25.05.2 behavior
observed on Marlowe (``docs/validation/observed-marlowe.md``, "Usage accounting"):

- ``id -un`` answers the ``user`` knob, so replies name ``USER`` and not the tester's login;
- ``sacctmgr -nP show assoc where user=U account=A format=account`` prints A once when
  ``assoc`` holds, else nothing (an unknown account is not an error);
- ``sreport -P -t seconds -T cpu,gres/gpu cluster AccountUtilizationByUser Accounts=A
  Start=D End=D|now`` prints the five header lines (a banner whose window ends one second
  before End, or at ``now_end``, the end of the current hour, for End=now; its seconds count
  site-local time across a daylight saving change), then the rows given for (Start, End) and
  none for any other window, exactly as for an unknown account or a window without usage;
- ``sacct -X -D -n -P -u U -A A -S D -E D|now --format=JobIDRaw,State,AllocTRES,ElapsedRaw,
  Start,End`` prints the ``sacct`` rows, whole runs that overlap the window.

Dates and times are site-local, as Slurm reads and prints them under the
TZ=America/Los_Angeles that usage.sh exports; the fake records the TZ each call saw (tests
assert it) but does not read it. Any other argv exits 2 with a message on stderr, so a changed
remote program cannot pass by accident. ``rc`` maps a tool to an exit status with which every
call of it fails like an unreachable database.
"""

import json
import os
import re
import shlex
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TOOLS = ("id", "sacctmgr", "sreport", "sacct")
RECORDED_ENV = ("TZ", "LC_ALL", "SLURM_TIME_FORMAT", "SACCT_FORMAT", "SLURM_CONF")
STATE = "FAKE_USAGE_SLURM"
SITE_ZONE = ZoneInfo("America/Los_Angeles")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
RULE = "-" * 80
HEADER = "Cluster|Account|Login|Proper Name|TRES Name|Used"
FORMAT = "--format=JobIDRaw,State,AllocTRES,ElapsedRaw,Start,End"
UNREACHABLE = "error: Problem talking to the database: Connection refused"


class UsageSlurm:
    """Test-side API: fixed accounting text plus the recorded calls."""

    def __init__(
        self,
        directory,
        *,
        user="USER",
        assoc=True,
        sreport=None,
        sacct=(),
        rc=None,
        now_end="2026-11-23T19:59:59",
    ):
        self.path = Path(directory) / "usage-slurm.json"
        self.bin = Path(directory) / "usage-bin"
        self.bin.mkdir(exist_ok=True)
        for tool in TOOLS:
            command = shlex.join([sys.executable, "-B", str(Path(__file__).resolve()), tool])
            wrapper = self.bin / tool
            wrapper.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
        windows = [[start, end, list(rows)] for (start, end), rows in (sreport or {}).items()]
        state = {
            "calls": [],
            "user": user,
            "assoc": assoc,
            "sreport": windows,
            "sacct": list(sacct),
            "rc": dict(rc or {}),
            "now_end": now_end,
        }
        self.path.write_text(json.dumps(state), encoding="utf-8")

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def env(self):
        return {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", STATE: str(self.path)}

    def calls(self, tool):
        return [call for call in self.state()["calls"] if call["tool"] == tool]


def _field(arg, key, pattern=r".+"):
    """The value of KEY=VALUE in arg when it matches pattern, else None."""
    found = re.fullmatch(rf"{re.escape(key)}=({pattern})", arg)
    return found[1] if found else None


def _failed(state, tool):
    if state["rc"].get(tool):
        print(f"{tool}: {UNREACHABLE}", file=sys.stderr)
        return state["rc"][tool]
    return 0


def ident(state, args):
    if args != ["-un"]:
        return None
    print(state["user"])
    return 0


def sacctmgr(state, args):
    head = ["-nP", "show", "assoc", "where", f"user={state['user']}"]
    account = _field(args[5], "account") if len(args) == 7 else None
    if args[:5] != head or account is None or args[6] != "format=account":
        return None
    code = _failed(state, "sacctmgr")
    if code == 0 and state["assoc"]:
        print(account)
    return code


def banner(start, end, now_end):
    """The sreport window line: [Start 00:00:00, end of the window], seconds in site time."""
    first = datetime.combine(date.fromisoformat(start), datetime.min.time(), SITE_ZONE)
    if end == "now":
        last = datetime.fromisoformat(now_end).replace(tzinfo=SITE_ZONE)
    else:
        last = datetime.combine(date.fromisoformat(end), datetime.min.time(), SITE_ZONE)
        last -= timedelta(seconds=1)
    seconds = int(last.timestamp() - first.timestamp()) + 1
    return (
        f"Cluster/Account/User Utilization {first:%Y-%m-%dT%H:%M:%S} - "
        f"{last:%Y-%m-%dT%H:%M:%S} ({seconds} secs)"
    )


def sreport(state, args):
    head = ["-P", "-t", "seconds", "-T", "cpu,gres/gpu", "cluster", "AccountUtilizationByUser"]
    if len(args) != 10 or args[:7] != head or _field(args[7], "Accounts") is None:
        return None
    start = _field(args[8], "Start", DAY.pattern)
    end = _field(args[9], "End", f"{DAY.pattern}|now")
    if start is None or end is None:
        return None
    code = _failed(state, "sreport")
    if code:
        return code
    print(RULE, banner(start, end, state["now_end"]), "Usage reported in TRES Seconds", sep="\n")
    print(RULE, HEADER, sep="\n")
    for window_start, window_end, rows in state["sreport"]:
        if (window_start, window_end) == (start, end):
            print("\n".join(rows))
    return 0


def sacct(state, args):
    head = ["-X", "-D", "-n", "-P", "-u", state["user"], "-A"]
    if (
        len(args) != 13
        or args[:7] != head
        or not args[7]
        or args[8] != "-S"
        or not DAY.fullmatch(args[9])
        or args[10] != "-E"
        or not (args[11] == "now" or DAY.fullmatch(args[11]))
        or args[12] != FORMAT
    ):
        return None
    code = _failed(state, "sacct")
    if code == 0 and state["sacct"]:
        print("\n".join(state["sacct"]))
    return code


def main(tool, args):
    path = Path(os.environ[STATE])
    state = json.loads(path.read_text(encoding="utf-8"))
    seen = {key: os.environ[key] for key in RECORDED_ENV if key in os.environ}
    state["calls"].append({"tool": tool, "argv": args, "env": seen})
    path.write_text(json.dumps(state), encoding="utf-8")
    handlers = {"id": ident, "sacctmgr": sacctmgr, "sreport": sreport, "sacct": sacct}
    code = handlers[tool](state, args)
    if code is None:
        print(f"fake {tool}: unsupported argv {args}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
