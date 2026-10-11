#!/usr/bin/env python3
"""Offline Slurm stand-in: sbatch, squeue, sacct and scancel over one JSON state file.

tests/fakes/bin/<tool> runs ``$FAKE_SLURM_PYTHON -B fakeslurm.py <tool> ARGS``. The state at
$FAKE_SLURM_STATE is {"next", "jobs", "calls"}; a job is {"id", "name", "user", "partition",
"array": null | {"count", "throttle"}, "tasks": [[restart, ...] per index], "submit", "purged",
"last", "script"} and a restart is {"state", "elapsed", "cpus", "tres", "exit"}. Live Sherlock
facts modelled: array tasks that never started (throttle-held) have no sacct row and show in
squeue as N_[a-b%t] PENDING JobArrayTaskLimit; started tasks are N_k rows with distinct
JobIDRaw, except the last task to leave the array, which keeps N; --duplicates prints one row
per restart (Restarts 0, 1, ...); sbatch --parsable prints N or N;cluster. Knobs:
FAKE_SBATCH_RC, FAKE_SBATCH_UNPARSED, FAKE_SBATCH_SUBMIT_THEN_FAIL (queued, exit 1),
FAKE_SBATCH_HANG (queued, then sleep seconds), FAKE_SBATCH_CLUSTER, FAKE_SCANCEL_RC. Every
call is recorded with the SBATCH_/SLURM_/SQUEUE_/SACCT_ variables it saw. FakeSlurm is the
test-side API.
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


def _user():
    return pwd.getpwuid(os.getuid()).pw_name


def _restart(state, cpus=1, tres="", elapsed=0):
    return {"state": state, "elapsed": elapsed, "cpus": cpus, "tres": tres or f"cpu={cpus},mem=1G,node=1", "exit": "0:0"}


def _locked(path, change):
    with open(path, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = json.load(stream)
        result = change(state)
        stream.seek(0)
        stream.truncate()
        json.dump(state, stream)
    return result


class FakeSlurm:
    def __init__(self, directory):
        self.path = Path(directory) / "slurm-state.json"
        self.path.write_text(json.dumps({"next": 1001, "jobs": [], "calls": []}), encoding="utf-8")

    def env(self, **knobs):
        return {"PATH": f"{BIN}{os.pathsep}{os.environ.get('PATH', '')}", "FAKE_SLURM_STATE": str(self.path),
                "FAKE_SLURM_PYTHON": sys.executable, **knobs}

    def state(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def calls(self, tool=None):
        return [call for call in self.state()["calls"] if tool in (None, call["tool"])]

    def add(self, name, *states, user=None, partition="normal", tres="", age_days=0):
        def change(state):
            job = {"id": state["next"], "name": name, "user": user or _user(), "partition": partition, "array": None,
                   "tasks": [[_restart(s, tres=tres, elapsed=60 * (s != "PENDING"))] for s in states],
                   "submit": time.time() - age_days * 86400, "purged": False, "last": None, "script": ""}
            job["array"] = {"count": len(states), "throttle": None} if len(states) > 1 else None
            state["next"] += 1
            state["jobs"].append(job)
            return job["id"]
        return _locked(self.path, change)

    def set(self, job_id, *states, **fields):
        """Set the newest restart of task i to states[i] (None keeps it); the last task set terminal keeps N."""
        def change(state):
            job = next(j for j in state["jobs"] if j["id"] == job_id)
            for index, value in enumerate(states):
                if value is not None:
                    job["tasks"][index][-1].update(state=value, elapsed=60, **fields)
                    job["last"] = index if value not in LIVE else job["last"]
        _locked(self.path, change)

    def requeue(self, job_id, index=0):
        def change(state):
            task = next(j for j in state["jobs"] if j["id"] == job_id)["tasks"][index]
            task[-1].update(state="PREEMPTED", elapsed=30)
            task.append(_restart("PENDING", task[-1]["cpus"]))
        _locked(self.path, change)


def _options(args):
    options, index = {}, 0
    while index < len(args):
        arg = args[index]
        if arg in VALUE_FLAGS and index + 1 < len(args):
            options[arg[1]], index = args[index + 1], index + 1
        else:
            key, _, value = arg.lstrip("-").partition("=")
            options[key] = value
        index += 1
    return options


def _mine(state, options):
    names = set(options["name"].split(",")) if options.get("name") else None
    return [job for job in state["jobs"] if options.get("u") in (None, job["user"])
            and options.get("p") in (None, job["partition"]) and (names is None or job["name"] in names)]


def _ranges(indices):
    spans, start = [], indices[0]
    for previous, current in zip(indices, indices[1:] + [None]):
        if current is None or current != previous + 1:
            spans.append(str(start) if start == previous else f"{start}-{previous}")
            start = current
    return ",".join(spans)


def _queue_entries(job):
    tops = [task[-1] for task in job["tasks"]]
    if not job["array"]:
        return [(str(job["id"]), tops[0])]
    waiting = [i for i, task in enumerate(job["tasks"]) if len(task) == 1 and task[0]["state"] == "PENDING"]
    entries = [(f"{job['id']}_{i}", top) for i, top in enumerate(tops) if i not in waiting]
    if waiting:
        throttle = job["array"]["throttle"]
        held = throttle and sum(top["state"] in ("RUNNING", "COMPLETING") for top in tops) >= throttle
        span = _ranges(waiting) + (f"%{throttle}" if throttle else "")
        entries.append((f"{job['id']}_[{span}]", {**tops[waiting[0]], "reason": "JobArrayTaskLimit" if held else "Priority"}))
    return entries


def squeue(state, options):
    lines = []
    for job in (j for j in _mine(state, options) if not j["purged"]):
        for text, top in _queue_entries(job):
            if options.get("t") != "all" and top["state"] not in LIVE:
                continue
            if "O" in options:
                values = {"username": job["user"], "state": top["state"], "timeused": "1:00", "timelimit": "2:00:00",
                          "tres-alloc": top["tres"] if top["state"] == "RUNNING" else ""}
                columns = (column.partition(":") for column in options["O"].split(","))
                lines.append("".join(values[field.lower()].ljust(int(width))[:int(width)] for field, _, width in columns))
            else:
                values = {"i": text, "T": top["state"], "r": top.get("reason", "None"), "P": job["partition"], "j": job["name"]}
                lines.append(re.sub(r"%\.?[0-9]*([A-Za-z])", lambda m: values.get(m[1], ""), options["o"]))
    return "".join(line + "\n" for line in lines), "", 0


def sacct(state, options):
    fields, days = [f.partition("%")[0] for f in options["format"].split(",")], int(options["S"][4:-4])
    lines = []
    for job in (j for j in _mine(state, options) if time.time() - j["submit"] <= days * 86400):
        for index, task in enumerate(job["tasks"]):
            if job["array"] and len(task) == 1 and task[0]["state"] == "PENDING":
                continue  # never started (throttle-held): no accounting row at all
            for restart, entry in enumerate(task) if "duplicates" in options else [(len(task) - 1, task[-1])]:
                raw = job["id"] if not job["array"] or job["last"] == index else job["id"] + 1 + index
                values = {"JobID": f"{job['id']}_{index}" if job["array"] else str(job["id"]), "JobIDRaw": str(raw),
                          "JobName": job["name"], "State": entry["state"], "Partition": job["partition"],
                          "ElapsedRaw": str(entry["elapsed"]), "AllocCPUS": str(entry["cpus"]), "AllocTRES": entry["tres"],
                          "Submit": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(job["submit"])),
                          "Restarts": str(restart), "ExitCode": entry["exit"]}
                lines.append("|".join(values[field] for field in fields))
    return "".join(line + "\n" for line in lines), "", 0


def sbatch(state, options, script):
    if os.environ.get("FAKE_SBATCH_RC"):
        return "", "sbatch: error: Batch job submission failed: Invalid partition name specified\n", int(os.environ["FAKE_SBATCH_RC"])
    count, _, throttle = (options["array"].partition("-")[2].partition("%") if "array" in options else ("", "", ""))
    cpus = int(options.get("cpus-per-task", "1")) * int(options.get("ntasks", "1"))
    job = {"id": state["next"], "name": options.get("job-name"), "user": _user(), "partition": options.get("partition"),
           "array": {"count": int(count) + 1, "throttle": int(throttle) if throttle else None} if count else None,
           "tasks": [[_restart("PENDING", cpus)] for _ in range(int(count) + 1 if count else 1)],
           "submit": time.time(), "purged": False, "last": None, "script": Path(script).read_text(encoding="utf-8")}
    state["next"] += 1
    state["jobs"].append(job)
    if os.environ.get("FAKE_SBATCH_UNPARSED"):
        return f"Submitted batch job {job['id']}\n", "", 0
    if os.environ.get("FAKE_SBATCH_SUBMIT_THEN_FAIL"):
        return "", "sbatch: error: Socket timed out on send/recv operation\n", 1
    cluster = os.environ.get("FAKE_SBATCH_CLUSTER")
    return ("" if os.environ.get("FAKE_SBATCH_HANG") else f"{job['id']}{';' + cluster if cluster else ''}\n"), "", 0


def scancel(state, options):
    if os.environ.get("FAKE_SCANCEL_RC"):
        return "", "scancel: error: Kill job error on job id\n", int(os.environ["FAKE_SCANCEL_RC"])
    for job in _mine(state, options):
        for task in job["tasks"]:
            if task[-1]["state"] in LIVE:
                task[-1]["state"] = "CANCELLED"
    return "", "", 0


def main(tool, args):
    def run(state):
        state["calls"].append({"tool": tool, "argv": args, "env": {k: v for k, v in os.environ.items()
                                                                     if k.startswith(("SBATCH_", "SLURM_", "SQUEUE_", "SACCT_"))}})
        options = _options(args[:-1] if tool == "sbatch" else args)
        return sbatch(state, options, args[-1]) if tool == "sbatch" else globals()[tool](state, options)
    out, err, code = _locked(os.environ["FAKE_SLURM_STATE"], run)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.stdout.flush()
    if tool == "sbatch" and os.environ.get("FAKE_SBATCH_HANG"):
        time.sleep(float(os.environ["FAKE_SBATCH_HANG"]))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
