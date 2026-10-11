#!/usr/bin/env python3
"""Offline Slurm stand-in: sbatch, squeue, sacct and scancel over one JSON state file.

tests/fakes/bin/<tool> runs ``$FAKE_SLURM_PYTHON -B fakeslurm.py <tool> ARGS``. The state
at $FAKE_SLURM_STATE is {"next", "jobs", "calls"}. A job is {"id", "name", "user",
"partition", "array": null | {"count", "throttle"}, "tasks": [[restart, ...] per index],
"submit", "last", "script"}; a restart is {"state", "elapsed", "cpus", "tres", "exit"}.

Live Sherlock facts modelled: array tasks that never started (throttle-held) have no sacct
row and show in squeue as one N_[a-b%t] PENDING aggregate (reason JobArrayTaskLimit, which
shk does not read); started tasks are N_k rows with distinct JobIDRaw, except the last task
to leave the array, which keeps N (query.sh does not ask for JobIDRaw, so the quirk cannot
confuse the parser); --duplicates prints one row per restart (Restarts 0, 1, ...); squeue
-t all forgets a finished job after MinJobAge; sbatch --parsable prints N or N;cluster.

Knobs: FAKE_SBATCH_RC, FAKE_SBATCH_UNPARSED, FAKE_SBATCH_SUBMIT_THEN_FAIL (queued, exit
1), FAKE_SBATCH_HANG (queued, then sleep that many seconds), FAKE_SBATCH_CLUSTER,
FAKE_SCANCEL_RC. Every call is recorded with the SBATCH_/SLURM_/SQUEUE_/SACCT_/SCANCEL_
variables it saw. FakeSlurm is the test-side API.
"""
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import sys
import time

BIN = Path(__file__).resolve().with_name("bin")
LIVE = {"PENDING", "RUNNING", "SUSPENDED", "COMPLETING", "CONFIGURING", "REQUEUED"}
VALUE_FLAGS = ("-u", "-t", "-p", "-o", "-O", "-S", "-E")
MIN_JOB_AGE = 300  # seconds slurmctld keeps a finished job (counted from submission here)
RECORDED_ENV = ("SBATCH_", "SLURM_", "SQUEUE_", "SACCT_", "SCANCEL_")


def _user():
    return pwd.getpwuid(os.getuid()).pw_name


def _restart(state, cpus=1, tres="", elapsed=0):
    return {"state": state, "elapsed": elapsed, "cpus": cpus,
            "tres": tres or f"cpu={cpus},mem=1G,node=1", "exit": "0:0"}


def _locked(path, change):
    """Apply change(state) under an exclusive lock and write the state back."""
    with open(path, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = json.load(stream)
        result = change(state)
        stream.seek(0)
        stream.truncate()
        json.dump(state, stream)
    return result


def _append_job(state, *, name, user, partition, tasks, array=None, submit=None, script=""):
    job = {"id": state["next"], "name": name, "user": user, "partition": partition,
           "array": array, "tasks": tasks, "submit": time.time() if submit is None else submit,
           "last": None, "script": script}
    state["next"] += 1
    state["jobs"].append(job)
    return job


def _job(state, job_id):
    return next(job for job in state["jobs"] if job["id"] == job_id)


class FakeSlurm:
    def __init__(self, directory):
        self.path = Path(directory) / "slurm-state.json"
        self.path.write_text(json.dumps({"next": 1001, "jobs": [], "calls": []}), encoding="utf-8")

    def env(self, **knobs):
        return {"PATH": f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}",
                "FAKE_SLURM_STATE": str(self.path), "FAKE_SLURM_PYTHON": sys.executable, **knobs}

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def calls(self, tool=None):
        return [call for call in self.state()["calls"] if tool in (None, call["tool"])]

    def add(self, name, *states, user=None, partition="normal", tres="", age_days=0):
        """A job with one task per state (an array when there are several); returns its id."""
        tasks = [[_restart(s, tres=tres, elapsed=0 if s == "PENDING" else 60)] for s in states]
        array = {"count": len(states), "throttle": None} if len(states) > 1 else None

        def change(state):
            job = _append_job(state, name=name, user=user or _user(), partition=partition,
                              tasks=tasks, array=array, submit=time.time() - age_days * 86400)
            return job["id"]
        return _locked(self.path, change)

    def set(self, job_id, *states, **fields):
        """Set the newest restart of task i to states[i] (None keeps it)."""
        def change(state):
            job = _job(state, job_id)
            for index, value in enumerate(states):
                if value is None:
                    continue
                job["tasks"][index][-1].update(state=value, elapsed=60, **fields)
                if value not in LIVE:
                    job["last"] = index  # the last task to leave the array keeps the array id
        _locked(self.path, change)

    def requeue(self, job_id, index=0):
        """Preempt and requeue task index: one more restart row, pending again."""
        def change(state):
            task = _job(state, job_id)["tasks"][index]
            task[-1].update(state="PREEMPTED", elapsed=30)
            task.append(_restart("PENDING", task[-1]["cpus"]))
        _locked(self.path, change)


def _options(args):
    """{"u": ..., "name": ..., ...} from the -X VALUE and --long[=VALUE] options the scripts use."""
    options, index = {}, 0
    while index < len(args):
        arg = args[index]
        if arg in VALUE_FLAGS and index + 1 < len(args):
            options[arg[1]] = args[index + 1]
            index += 2
            continue
        key, _, value = arg.lstrip("-").partition("=")
        options[key] = value
        index += 1
    return options


def _selected(state, options):
    names = set(options["name"].split(",")) if options.get("name") else None
    return [job for job in state["jobs"]
            if options.get("u") in (None, job["user"])
            and options.get("p") in (None, job["partition"])
            and (names is None or job["name"] in names)]


def _ranges(indices):
    """[2, 3, 5] -> "2-3,5"."""
    spans, start = [], indices[0]
    for previous, current in zip(indices, [*indices[1:], None], strict=True):
        if current is None or current != previous + 1:
            spans.append(str(start) if start == previous else f"{start}-{previous}")
            start = current
    return ",".join(spans)


def _never_started(task):
    return len(task) == 1 and task[0]["state"] == "PENDING"


def _queue_entries(job):
    """(squeue %i, newest restart) per line: started tasks one by one, the rest as N_[...]."""
    newest = [task[-1] for task in job["tasks"]]
    if not job["array"]:
        return [(str(job["id"]), newest[0])]
    waiting = [i for i, task in enumerate(job["tasks"]) if _never_started(task)]
    entries = [(f"{job['id']}_{i}", top) for i, top in enumerate(newest) if i not in waiting]
    if waiting:
        throttle = job["array"]["throttle"]
        span = _ranges(waiting) + (f"%{throttle}" if throttle else "")
        entries.append((f"{job['id']}_[{span}]", newest[waiting[0]]))
    return entries


def _format_columns(job, top, layout):
    """One fixed-width squeue -O line for a layout such as "UserName:64,State:24"."""
    running = top["state"] == "RUNNING"
    values = {"username": job["user"], "state": top["state"],
              "tres-alloc": top["tres"] if running else ""}
    line = ""
    for column in layout.split(","):
        field, _, width = column.partition(":")
        line += values[field.lower()].ljust(int(width))[:int(width)]
    return line


def _in_controller(job):
    finished = all(task[-1]["state"] not in LIVE for task in job["tasks"])
    return not finished or time.time() - job["submit"] <= MIN_JOB_AGE


def _format_fields(layout, values):
    """squeue -o "%i|%T|..." with the given field values (widths are ignored)."""
    return re.sub(r"%\.?[0-9]*([A-Za-z])", lambda match: values.get(match[1], ""), layout)


def squeue(state, options):
    lines = []
    for job in filter(_in_controller, _selected(state, options)):
        for job_id, top in _queue_entries(job):
            if options.get("t") != "all" and top["state"] not in LIVE:
                continue
            if "O" in options:
                lines.append(_format_columns(job, top, options["O"]))
                continue
            values = {"i": job_id, "T": top["state"], "P": job["partition"], "j": job["name"]}
            lines.append(_format_fields(options["o"], values))
    return "".join(line + "\n" for line in lines), "", 0


def _acct_rows(job, duplicates):
    """sacct rows of one job: never-started array tasks have none; one row per restart."""
    for index, task in enumerate(job["tasks"]):
        if job["array"] and _never_started(task):
            continue
        restarts = list(enumerate(task)) if duplicates else [(len(task) - 1, task[-1])]
        for restart, entry in restarts:
            keeps_id = not job["array"] or job["last"] == index
            yield {"JobID": f"{job['id']}_{index}" if job["array"] else str(job["id"]),
                   "JobIDRaw": str(job["id"] if keeps_id else job["id"] + 1 + index),
                   "JobName": job["name"], "State": entry["state"], "Partition": job["partition"],
                   "ElapsedRaw": str(entry["elapsed"]), "AllocCPUS": str(entry["cpus"]),
                   "AllocTRES": entry["tres"],
                   "Submit": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(job["submit"])),
                   "Restarts": str(restart), "ExitCode": entry["exit"]}


def sacct(state, options):
    fields = [field.partition("%")[0] for field in options["format"].split(",")]
    days = int(options["S"].removeprefix("now-").removesuffix("days"))
    lines = []
    for job in _selected(state, options):
        if time.time() - job["submit"] <= days * 86400:
            rows = _acct_rows(job, "duplicates" in options)
            lines.extend("|".join(row[field] for field in fields) for row in rows)
    return "".join(line + "\n" for line in lines), "", 0


def sbatch(state, options, script):
    if os.environ.get("FAKE_SBATCH_RC"):
        error = "sbatch: error: Batch job submission failed: Invalid partition name specified\n"
        return "", error, int(os.environ["FAKE_SBATCH_RC"])
    last, _, throttle = options.get("array", "").partition("-")[2].partition("%")
    count = int(last) + 1 if last else 1
    cpus = int(options.get("cpus-per-task", "1")) * int(options.get("ntasks", "1"))
    array = {"count": count, "throttle": int(throttle) if throttle else None} if last else None
    job = _append_job(state, name=options.get("job-name"), user=_user(),
                      partition=options.get("partition"), array=array,
                      tasks=[[_restart("PENDING", cpus)] for _ in range(count)],
                      script=Path(script).read_text(encoding="utf-8"))
    if os.environ.get("FAKE_SBATCH_UNPARSED"):
        return f"Submitted batch job {job['id']}\n", "", 0
    if os.environ.get("FAKE_SBATCH_SUBMIT_THEN_FAIL"):
        return "", "sbatch: error: Socket timed out on send/recv operation\n", 1
    if os.environ.get("FAKE_SBATCH_HANG"):
        return "", "", 0
    cluster = os.environ.get("FAKE_SBATCH_CLUSTER")
    return f"{job['id']}{';' + cluster if cluster else ''}\n", "", 0


def scancel(state, options):
    if os.environ.get("FAKE_SCANCEL_RC"):
        return "", "scancel: error: Kill job error on job id\n", int(os.environ["FAKE_SCANCEL_RC"])
    for job in _selected(state, options):
        for task in job["tasks"]:
            if task[-1]["state"] in LIVE:
                task[-1]["state"] = "CANCELLED"
    return "", "", 0


def main(tool, args):
    def run(state):
        seen = {key: value for key, value in os.environ.items() if key.startswith(RECORDED_ENV)}
        state["calls"].append({"tool": tool, "argv": args, "env": seen})
        if tool == "sbatch":  # the staged script path comes last
            return sbatch(state, _options(args[:-1]), args[-1])
        return {"squeue": squeue, "sacct": sacct, "scancel": scancel}[tool](state, _options(args))
    out, err, code = _locked(os.environ["FAKE_SLURM_STATE"], run)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.stdout.flush()
    if tool == "sbatch" and os.environ.get("FAKE_SBATCH_HANG"):
        time.sleep(float(os.environ["FAKE_SBATCH_HANG"]))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
