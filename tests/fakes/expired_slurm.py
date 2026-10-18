"""Canned Slurm text for detailed status: squeue, sacct and scontrol with strict argv checks.

``CannedSlurm(directory, queue=..., accounting=..., jobs=...)`` writes wrappers that run this
file over a JSON state; ``.env()`` puts them first on PATH. Queue and accounting rows are lists
of field values, or strings split at "|", in details.sh column order; jobs are scontrol
--oneliner records. Modelled Slurm 25.11 behavior observed on Sherlock:

- squeue -o and sacct --delimiter print the separator the caller asks for, and --name selects
  exact job names; rows are never filtered by user or job id, so callers must scope them;
- squeue without --array folds the pending tasks of one array record into N_[a-b];
- scontrol show job N prints record N and every element whose ArrayJobId is N;
- sacct refuses windows wider than 31 days ("Too wide of a date range in query").

Any other argv exits 2, so a changed remote program cannot pass by accident. The state knobs
"queue_rc" and "sacct_rc" fail a tool like an unreachable controller or database. Every call
is recorded with the TZ, LC_ALL and SLURM_TIME_FORMAT it saw.
"""

import json
import os
import pwd
import re
import shlex
import sys
from pathlib import Path

QUEUE = ("%i", "%A", "%T", "%P", "%M", "%C", "%V", "%S", "%e", "%N", "%U", "%k", "%j")
ACCOUNTING = (
    "JobID",
    "JobIDRaw",
    "Cluster",
    "DBIndex",
    "State",
    "Partition",
    "ElapsedRaw",
    "AllocCPUS",
    "AllocTRES",
    "Submit",
    "Start",
    "End",
    "Restarts",
    "ExitCode",
    "NodeList",
    "UID",
    "Comment",
    "JobName",
)
WINDOW = re.compile(r"now-([0-9]+)days")
MAX_WINDOW_DAYS = 31
RECORDED_ENV = ("TZ", "LC_ALL", "SLURM_TIME_FORMAT")
STATE = "FAKE_CANNED_SLURM"
USER = pwd.getpwuid(os.getuid()).pw_name


class CannedSlurm:
    """Test-side API: fixed Slurm text plus the recorded calls."""

    def __init__(self, directory, *, queue=(), accounting=(), jobs=()):
        self.path = Path(directory) / "canned-slurm.json"
        self.bin = Path(directory) / "canned-bin"
        self.bin.mkdir(exist_ok=True)
        for tool in ("squeue", "sacct", "scontrol"):
            command = shlex.join([sys.executable, "-B", str(Path(__file__).resolve()), tool])
            wrapper = self.bin / tool
            wrapper.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
            wrapper.chmod(0o755)
        self._write({"queue": list(queue), "accounting": list(accounting), "jobs": list(jobs)})

    def _write(self, state):
        self.path.write_text(json.dumps({"calls": [], **state}), encoding="utf-8")

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def update(self, **changes):
        self._write({**self.state(), **changes})

    def env(self):
        return {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", STATE: str(self.path)}

    def calls(self, tool):
        return [call for call in self.state()["calls"] if call["tool"] == tool]


def _fields(row):
    return row if isinstance(row, list) else row.split("|")


def _value(record, key):
    found = re.search(rf"(?:^| ){re.escape(key)}=(\S*)", record)
    return found[1] if found else None


def _named(rows, selector, column):
    names = [arg.removeprefix("--name=") for arg in selector if arg.startswith("--name=")]
    return rows if not names else [row for row in rows if row[column] in names[0].split(",")]


def _ranges(indices):
    """[2, 3, 5] -> "2-3,5"."""
    spans, start = [], indices[0]
    for previous, current in zip(indices, [*indices[1:], None], strict=True):
        if current is None or current != previous + 1:
            spans.append(str(start) if start == previous else f"{start}-{previous}")
            start = current
    return ",".join(spans)


def _folded(rows):
    """squeue without --array: the pending tasks of one array record print as N_[a-b]."""
    kept, waiting = [], {}
    for row in rows:
        base, _, task = row[0].partition("_")
        if row[2] == "PENDING" and task.isdecimal() and row[1] == base:
            waiting.setdefault(base, []).append((int(task), row))
        else:
            kept.append(row)
    for base, tasks in waiting.items():
        indices = sorted(task for task, _ in tasks)
        kept.append([f"{base}_[{_ranges(indices)}]", *tasks[0][1][1:]])
    return kept


def _selector(selector, prefixes):
    return not selector or (len(selector) == 1 and selector[0].startswith(prefixes))


def squeue(state, args):
    head = ["-h", "--local", "-t", "all", "-u", USER]
    selector, layout = args[6:-2], args[-1:]
    separator = layout[0][2:3] if layout else "|"
    if (
        args[:6] != head
        or args[-2:-1] != ["-o"]
        or layout[0].split(separator) != list(QUEUE)
        or not (selector == ["--array"] or _selector(selector, ("--name=",)))
    ):
        return None
    if state.get("queue_rc"):
        print("slurm_load_jobs error: Unable to contact slurm controller", file=sys.stderr)
        return state["queue_rc"]
    rows = _named([_fields(row) for row in state["queue"]], selector, -1)
    for row in rows if selector == ["--array"] else _folded(rows):
        print(separator.join(row))
    return 0


def sacct(state, args):
    separator = "|"
    if args[2:3] and args[2].startswith("--delimiter="):
        separator = args[2].removeprefix("--delimiter=")
        args = args[:2] + args[3:]
    head = ["-n", "-P", "--local", "--allocations", "--duplicates", "-u", USER]
    selector, tail = args[7:-5], args[-5:]
    window = WINDOW.fullmatch(tail[1]) if len(args) >= 12 else None
    layout = tail[-1].removeprefix("--format=") if tail else ""
    if (
        args[:7] != head
        or not _selector(selector, ("--jobs=", "--name="))
        or window is None
        or tail[0] != "-S"
        or tail[2:4] != ["-E", "now"]
        or [column.partition("%")[0] for column in layout.split(",")] != list(ACCOUNTING)
    ):
        return None
    if int(window[1]) > MAX_WINDOW_DAYS:
        print("sacct: error: Too wide of a date range in query", file=sys.stderr)
        return 1
    if state.get("sacct_rc"):
        print("sacct: error: Problem talking to the database: Connection refused", file=sys.stderr)
        return state["sacct_rc"]
    for row in _named([_fields(row) for row in state["accounting"]], selector, -1):
        print(separator.join(row))
    return 0


def scontrol(state, args):
    if len(args) != 4 or args[:2] != ["show", "job"] or args[3] != "--oneliner":
        return None
    if not args[2].isdecimal():
        return None
    found = [
        job for job in state["jobs"] if args[2] in (_value(job, "JobId"), _value(job, "ArrayJobId"))
    ]
    if not found:
        print("slurm_load_jobs error: Invalid job id specified", file=sys.stderr)
        return 1
    print("\n".join(found))
    return 0


def main(tool, args):
    path = Path(os.environ[STATE])
    state = json.loads(path.read_text(encoding="utf-8"))
    seen = {key: os.environ[key] for key in RECORDED_ENV if key in os.environ}
    state["calls"].append({"tool": tool, "argv": args, "env": seen})
    path.write_text(json.dumps(state), encoding="utf-8")
    code = {"squeue": squeue, "sacct": sacct, "scontrol": scontrol}[tool](state, args)
    if code is None:
        print(f"fake {tool}: unsupported argv {args}", file=sys.stderr)
        return 2
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2:]))
