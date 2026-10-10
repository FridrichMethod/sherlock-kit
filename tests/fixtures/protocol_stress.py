"""Bounded offline protocol soak: real processes run the registry program against fake Slurm tools, plus real rsync.

Every runner, reader and event writer under test is the shipped ``python3 -c``
stub (``sherlock_registry.program_argv``) executed as its own process against a
private temporary ``registry_root``. ``sbatch``, ``sacct`` and ``squeue`` are
fixture scripts on a PATH that contains nothing else, so the program under test
can never reach the site's Slurm; their replies are synthetic and labelled so in
the report. This program never invokes SSH. Its success is engineering evidence,
not proof of a live Slurm or scientific result.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import pwd
import random
import resource
import shutil
import signal
import stat
import subprocess
import sys
import time
import traceback
from types import SimpleNamespace

from sherlock_artifacts import attempt_record, build_manifest, fetch_bundle, read_attempt_sidecar, rsync_transfer, verify_bundle
from sherlock_commands import QUERY_SENTINEL, cached_query, forget_queries
from sherlock_kit import policy_identity
from sherlock_orchestration import AttemptSpec, SafetyError, canonical, digest, frozen_record, sbatch_options
from sherlock_registry import (FIELDS, FINISHED_STATES, parse_event_reply, parse_read, parse_rows, parse_submit_reply, program_argv, program_sha256,
                               released, resolve, task_key)

FAULTS = ("intent_committed", "transferred", "verified", "promoted", "receipt_committed")
FAMILIES = ("submit_race", "runner_death", "query_cache", "resolution", "fetch_death", "fetch_race", "fetch_reject")
THREAD_VARS = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
EVENT_FILES = ("submitted.json", "not_sent.json", "abandoned.json", "resolved.json")
FINALS = ("COMPLETED", "FAILED", "RUNNING", "PENDING")
CRASH = 73
KILLED = -signal.SIGKILL
MAX_CASES = 40000
MAX_LOG_BYTES = 64 * 1024 * 1024
PROGRAM_TIMEOUT = 60
CLUSTER = "fixture-cluster"
PRINCIPAL = pwd.getpwuid(os.getuid()).pw_name
SCRIPT = b"#!/bin/bash -l\necho fixture task \"$SLURM_ARRAY_TASK_ID\"\n"
CONTEXT = mp.get_context("fork")  # Sherlock/Linux; at most three workers per case.
FAKE_TOOL = '''#!@PYTHON@
import json, os, signal, sys, time
tool = os.path.basename(sys.argv[0])
prefix = "SHK_FAKE_" + tool.upper() + "_"
data = sys.stdin.buffer.read() if tool == "sbatch" else b""
record = os.environ.get("SHK_FAKE_RECORD")
if record:
    entry = {"tool": tool, "argv": sys.argv[1:], "stdin": data.decode("latin-1"),
             "sbatch_env": sorted(k for k in os.environ if k.startswith("SBATCH_"))}
    with open(record, "a") as stream:
        stream.write(json.dumps(entry) + "\\n")
if os.environ.get(prefix + "SLEEP"):
    time.sleep(float(os.environ[prefix + "SLEEP"]))
if os.environ.get(prefix + "KILL") == "before_reply":
    os.kill(os.getppid(), signal.SIGKILL)
sys.stdout.write(os.environ.get(prefix + "STDOUT", ""))
sys.stderr.write(os.environ.get(prefix + "STDERR", ""))
sys.stdout.flush()
sys.stderr.flush()
if os.environ.get(prefix + "KILL") == "after_accept":
    os.kill(os.getppid(), signal.SIGKILL)
sys.exit(int(os.environ.get(prefix + "RC", "0")))
'''


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def read(path):
    return json.loads(Path(path).read_text())


def stamp(seconds):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))


def emit(path, value):
    # Each worker owns one file. Atomicity of a shared log is not assumed.
    with open(path, "x", encoding="utf-8") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


# ---------------------------------------------------------------- registry program as a real process

def prepare(root):
    """Fake Slurm tools, a workload and an empty registry location for one case."""
    fake_bin = root / "fake-bin"
    fake_bin.mkdir(mode=0o700)
    for tool in ("sbatch", "sacct", "squeue"):
        path = fake_bin / tool
        path.write_text(FAKE_TOOL.replace("@PYTHON@", sys.executable))
        path.chmod(0o700)
    # The program runs its queries through `env`; linking the real one keeps PATH free of any real Slurm tool.
    (fake_bin / "env").symlink_to(shutil.which("env"))
    run_dir = root / "run"
    run_dir.mkdir(mode=0o700)
    script = root / "job.sh"
    script.write_bytes(SCRIPT)
    return SimpleNamespace(root=root, bin=fake_bin, calls=root / "tool-calls.jsonl", registry=root / "registry", run_dir=run_dir, script=script)


def registry_env(case, **fake):
    """PATH holds only the fake tools; SBATCH_* must be stripped by the runner before it executes sbatch."""
    env = {"PATH": str(case.bin), "SHK_FAKE_RECORD": str(case.calls), "SBATCH_ACCOUNT": "leak", "SBATCH_PARTITION": "leak"}
    env.update({name: "1" for name in THREAD_VARS})
    env.update({"SHK_FAKE_" + key.upper(): str(value) for key, value in fake.items()})
    return env


def start_program(case, subcommand, *args, **fake):
    """The shipped stub as its own process; the caller collects stdout/stderr."""
    argv = program_argv(subcommand, *args)
    return subprocess.Popen([sys.executable, *argv[1:]], env=registry_env(case, **fake), stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def program(case, subcommand, *args, **fake):
    process = start_program(case, subcommand, *args, **fake)
    stdout, stderr = process.communicate(timeout=PROGRAM_TIMEOUT)
    return SimpleNamespace(returncode=process.returncode, stdout=stdout, stderr=stderr)


def tool_calls(case, tool):
    lines = case.calls.read_text().splitlines() if case.calls.exists() else []
    return [entry for entry in map(json.loads, lines) if entry["tool"] == tool]


def attempt_id(config, n):
    return digest([config["seed"], "attempt", n])[:32]


def spec(case, config, task="task", partition=None, count=1, parent=None):
    d = digest([config["seed"], config["index"], task])
    resources = {"partition": partition or config["partition"], "cpus": config["cpus"], "gpus": 0, "tasks": 1,
                 "memory_mb": config["memory_mb"], "walltime_seconds": config["walltime"]}
    if count > 1:
        resources["array"] = {"count": count}
    return AttemptSpec(project="stress-fixture", campaign="offline", task=f"case-{config['index']}-{config['seed']:x}-{task}",
        cluster=CLUSTER, principal=PRINCIPAL, resource_scope="fixture-scope", code_digest=d, input_digest=digest([d, "input"]),
        runtime_digest=digest([d, "runtime"]), policy_digest=digest([d, "policy"]), remote_script=str(case.script),
        script_digest=hashlib.sha256(SCRIPT).hexdigest(), remote_run_directory=str(case.run_dir), parent_attempt=parent, resources=resources)


def record_text(case, config, attempt, **spec_overrides):
    """The record the workstation ships: canonical JSON, unstamped; the runner adds created/created_on/principal_uid/program_sha256."""
    frozen = frozen_record(spec(case, config, **spec_overrides))
    return canonical({"schema_version": 1, "kind": "record", "attempt": attempt, "key": task_key(frozen), "job_name": "shk-" + attempt,
                      "spec": frozen, "sbatch_options": sbatch_options(attempt, frozen)})


def submit(case, config, attempt, job=None, **kwargs):
    """One real runner process; returns (outcome, value) from the single SHK_ line."""
    fake = kwargs.pop("fake", {})
    fake.setdefault("sbatch_stdout", f"{config['job'] if job is None else job}\n")
    result = program(case, "submit", str(case.registry), record_text(case, config, attempt, **kwargs), program_sha256(), **fake)
    require(result.returncode == 0, f"runner exited {result.returncode}: {result.stderr[-500:]}")
    return parse_submit_reply(attempt, result.stdout)


def read_attempt(case, attempt, sacct_stdout=""):
    """The reader process plus the library's resolve(); the shipped program and the import must agree."""
    result = program(case, "read", str(case.registry), "--attempt", attempt, sacct_stdout=sacct_stdout)
    require(result.returncode == 0, f"reader exited {result.returncode}: {result.stderr[-500:]}")
    document = parse_read(result.stdout)
    entry, = document["attempts"]
    require(entry["exists"] and entry["record"] is not None and not entry["errors"], f"reader entry unusable: {entry['errors']}")
    sacct = document["sacct"]
    require(sacct is not None and sacct["status"] == "complete" and sacct["argv"][-1] == "--name=shk-" + attempt, "reader selected by something other than the job name")
    resolution = resolve(entry["record"], entry["events"], sacct["rows"], document["now"])
    local = resolve(entry["record"], entry["events"], parse_rows(sacct_stdout), document["now"])
    require(resolution == local, "shipped reader and imported library disagree")
    return entry, resolution


def open_attempts(case):
    result = program(case, "read", str(case.registry), "--open")
    require(result.returncode == 0, "open listing failed")
    return [entry["attempt"] for entry in parse_read(result.stdout)["attempts"]]


def event(case, kind, attempt, sacct_stdout="", note=""):
    result = program(case, "event", str(case.registry), kind, canonical({"note": note}), attempt, sacct_stdout=sacct_stdout)
    require(result.returncode == 0, f"event writer exited {result.returncode}: {result.stderr[-500:]}")
    (replied, outcome, value), = parse_event_reply(result.stdout)
    require(replied == attempt, "event reply names another attempt")
    return outcome, value


def files(case, attempt):
    directory = case.registry / "attempts" / attempt
    return sorted(path.name for path in directory.iterdir()) if directory.exists() else None


def marker(case, key):
    path = case.registry / "tasks" / key
    return path.read_text().strip() if path.exists() else None


def key_of(case, config, **spec_overrides):
    return task_key(frozen_record(spec(case, config, **spec_overrides)))


def sacct_row(attempt, created, job, *, state, cpus, elapsed=0, restart=0, task=None, indices=None, db_index):
    """One bounded sacct allocation line for this attempt's job, task ``N_k`` or pending aggregate ``N_[spec]``."""
    if task is not None:
        job_id, raw = f"{job}_{task}", str(job + task)
    elif indices is not None:
        job_id, raw = f"{job}_[{indices}]", str(job)
    else:
        job_id, raw = str(job), str(job)
    started = state not in {"PENDING"}
    start = created + 1 + restart * 1000
    values = {"JobID": job_id, "JobIDRaw": raw, "User": PRINCIPAL, "JobName": "shk-" + attempt, "State": state,
              "ElapsedRaw": str(elapsed if started else 0), "AllocCPUS": str(cpus if started else 0),
              "AllocTRES": f"cpu={cpus},mem=1024M,node=1" if started else "", "Submit": stamp(created),
              "Start": stamp(start) if started else "Unknown", "End": stamp(start + elapsed) if started and state in FINISHED_STATES else "Unknown",
              "Restarts": str(restart), "ExitCode": "0:0" if state != "FAILED" else "1:0", "Cluster": CLUSTER, "DBIndex": str(db_index)}
    return "|".join(values[field] for field in FIELDS)


def check_registry(case):
    """Structural invariants of the registry tree after a case; returns one summary row per attempt."""
    root = case.registry
    require(root.is_dir() and not root.is_symlink(), "registry missing")
    for directory in (root, root / "attempts", root / "tasks"):
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077, f"registry directory not private: {directory}")
    for path in root.rglob("*"):
        info = path.lstat()
        require(not stat.S_ISLNK(info.st_mode), f"symlink inside the registry: {path}")
        require(not path.name.endswith(".tmp"), f"temporary file left behind: {path}")
        if stat.S_ISREG(info.st_mode):
            require(stat.S_IMODE(info.st_mode) == 0o600, f"registry file not 0600: {path}")
    require(not (root / ".lock").exists() or stat.S_ISREG((root / ".lock").lstat().st_mode), "registry lock is not a regular file")
    summary, keys, parents = [], {}, {}
    for directory in sorted((root / "attempts").iterdir()):
        names = sorted(path.name for path in directory.iterdir())
        require("record.json" in names, f"attempt without record.json: {directory.name}")
        record = read(directory / "record.json")
        require(record["attempt"] == directory.name and record["key"] == task_key(record["spec"]), "record identity disagrees with its directory")
        require(all(key in record for key in ("created", "created_on", "principal_uid", "program_sha256")), "record not stamped by the runner")
        require(record["program_sha256"] == program_sha256(), "record stamped by another program revision")
        require(len({"submitted.json", "not_sent.json"} & set(names)) <= 1, "both submitted.json and not_sent.json present")
        for name in names:
            require(name == "record.json" or name in EVENT_FILES or (name.startswith("ack-") and name.endswith(".json")), f"unexpected registry file: {name}")
        keys.setdefault(record["key"], []).append(directory.name)
        if record["spec"].get("parent_attempt") is not None:
            parents.setdefault(record["key"], set()).add(record["spec"]["parent_attempt"])
        summary.append({"attempt": directory.name, "key": record["key"], "files": names, "parent": record["spec"].get("parent_attempt")})
    for path in (root / "tasks").iterdir():
        target = path.read_text().strip()
        require(target in keys.get(path.name, ()), f"task marker {path.name} names a missing or foreign attempt {target}")
        # D2: the marker names the latest attempt, the one no sibling record of the key calls its parent.
        childmost = [attempt for attempt in keys[path.name] if attempt not in parents.get(path.name, ())]
        require(childmost == [target], f"task marker {path.name} names {target}, not the childmost attempt {childmost}")
    return summary


# ---------------------------------------------------------------- registry families

def race_case(case, config):
    """Three runners race one or three logical tasks; the lock and the marker admit exactly one attempt per task."""
    ids = [attempt_id(config, i) for i in range(3)]
    tasks = ["same"] * 3 if config["same_task"] else [f"task-{i}" for i in range(3)]
    texts = [record_text(case, config, ids[i], task=tasks[i]) for i in range(3)]
    processes = [(ids[i], start_program(case, "submit", str(case.registry), texts[i], program_sha256(), sbatch_stdout=f"{config['job'] + i}\n",
                                        sbatch_sleep=config["sbatch_sleep"])) for i in config["worker_order"]]
    replies = {}
    for attempt, process in processes:
        stdout, stderr = process.communicate(timeout=PROGRAM_TIMEOUT)
        require(process.returncode == 0, f"runner exited {process.returncode}: {stderr[-500:]}")
        replies[attempt] = parse_submit_reply(attempt, stdout)
    submitted = sorted(attempt for attempt, (outcome, _) in replies.items() if outcome == "submitted")
    if config["same_task"]:
        require(len(submitted) == 1, "race admitted more or less than one attempt of one logical task")
        winner = submitted[0]
        losers = sorted(value for attempt, (outcome, value) in replies.items() if attempt != winner)
        require(all(outcome == "refused" for attempt, (outcome, _) in replies.items() if attempt != winner), "loser was not refused")
        require(losers == ["duplicate_logical_task:" + winner] * 2, f"losers did not name the winner: {losers}")
        require(sorted(path.name for path in (case.registry / "attempts").iterdir()) == [winner], "refused attempt left a directory")
        require(marker(case, key_of(case, config, task="same")) == winner, "marker does not name the winner")
    else:
        require(len(submitted) == 3, "independent logical tasks were not all admitted")
        require(all(marker(case, key_of(case, config, task=tasks[i])) == ids[i] for i in range(3)), "markers do not name their attempts")
    sbatch = tool_calls(case, "sbatch")
    require(len(sbatch) == len(submitted), "sbatch ran for a refused attempt")
    # Each admitted attempt runs sbatch exactly once with its frozen option list (minus the program name) and the script bytes.
    frozen_argv = {ids[i]: json.loads(texts[i])["sbatch_options"][1:] for i in range(3)}
    owners = []
    for call in sbatch:
        owner = [attempt for attempt in submitted if "--job-name=shk-" + attempt in call["argv"]]
        require(len(owner) == 1 and call["argv"] == frozen_argv[owner[0]], f"sbatch argv is not the frozen option list: {call['argv']}")
        require(call["stdin"].encode("latin-1") == SCRIPT and call["sbatch_env"] == [], "sbatch received other bytes or inherited SBATCH_*")
        owners.append(owner[0])
    require(sorted(owners) == submitted, "sbatch calls do not match the admitted attempts one to one")
    for attempt in submitted:
        require(files(case, attempt) == ["record.json", "submitted.json"], "submitted attempt lacks its two create-once files")
        require(read(case.registry / "attempts" / attempt / "submitted.json")["job_id"] == str(config["job"] + ids.index(attempt)), "job id not recorded")
    return [replies[attempt][0] for attempt in ids]


def death_case(case, config):
    """sbatch kills the runner after the record and the claim are durable; nothing is released until accounting decides."""
    attempt = attempt_id(config, 0)
    text = record_text(case, config, attempt)
    process = start_program(case, "submit", str(case.registry), text, program_sha256(), sbatch_stdout=f"{config['job']}\n", sbatch_kill=config["death_mode"])
    stdout, _ = process.communicate(timeout=PROGRAM_TIMEOUT)
    require(process.returncode == KILLED and stdout == "", "runner survived its sbatch or printed a reply")
    key = key_of(case, config)
    require(files(case, attempt) == ["record.json"] and marker(case, key) == attempt, "death left the registry without record+marker or with a reply file")
    sbatch, = tool_calls(case, "sbatch")
    require(sbatch["sbatch_env"] == [], "SBATCH_* leaked to sbatch")
    # The dead runner's lock does not survive it: the reader, the event writer and other runners proceed.
    entry, result = read_attempt(case, attempt)
    require(result["resolution"] == "inconclusive" and result["job_id"] is None and entry["events"] == [], "claimed attempt without accounting was not inconclusive")
    duplicate, value = submit(case, config, attempt_id(config, 1))
    require((duplicate, value) == ("refused", "duplicate_logical_task:" + attempt), "duplicate not refused after death")
    retry_id = attempt_id(config, 2)
    require(submit(case, config, retry_id, parent=attempt) == ("refused", "parent_not_released:inconclusive"), "inconclusive parent released a retry")
    require(event(case, "abandon", attempt) == ("refused", "not_applicable:inconclusive"), "young attempt was abandonable")
    require(sorted(path.name for path in (case.registry / "attempts").iterdir()) == [attempt], "refused retries left directories")
    if config["death_state"] is None:
        require(open_attempts(case) == [attempt], "open listing lost the claimed attempt")
        return ["inconclusive", "unreleased"]
    created = read(case.registry / "attempts" / attempt / "record.json")["created"]
    rows = sacct_row(attempt, created, config["job"], state=config["death_state"], cpus=config["cpus"], elapsed=config["cost"], db_index=1)
    _, result = read_attempt(case, attempt, rows)
    require(result["resolution"] == "terminal" and result["job_id"] == str(config["job"]), "named accounting did not identify the unrecorded submission")
    require(result["anomalies"] == ["submitted_unrecorded"] and result["cost"] == {"cpu_seconds": config["cost"] * config["cpus"], "gpu_seconds": 0, "known": True}, "cost or flags wrong")
    require(event(case, "resolved", attempt, rows) == ("written", "resolved.json"), "terminal attempt not closed")
    require(event(case, "resolved", attempt, rows) == ("refused", "already_present"), "closure written twice")
    require(open_attempts(case) == [], "closed attempt still open")
    outcome = submit(case, config, retry_id, parent=attempt, fake={"sacct_stdout": rows})
    if config["death_state"] == "FAILED":
        require(outcome == ("submitted", str(config["job"])) and marker(case, key) == retry_id, "FAILED parent did not release the retry")
        require(read(case.registry / "attempts" / retry_id / "record.json")["spec"]["parent_attempt"] == attempt, "retry lost its parent")
    else:
        require(outcome == ("refused", "parent_not_released:terminal") and marker(case, key) == attempt, "COMPLETED parent released a retry")
    return ["terminal", "released" if config["death_state"] == "FAILED" else "unreleased"]


def query_worker(path, key, start, output, invocation, config, now):
    require(start.wait(8), "query barrier timed out")
    def fake_query():
        emit(invocation, {"synthetic_query": config["seed"], "now": now})
        if config["query_failure"]:
            raise TimeoutError("synthetic transport failure")
        return {"seed": config["seed"], "jobs": [config["job"]]}
    try:
        emit(output, {"result": cached_query(path, key, fake_query, now=now)})
    except TimeoutError:
        emit(output, {"expected_failure": True})


def query_case(case, config):
    """Three processes ask one bounded scope at the same logical instant; one query, the others take the sentinel or the body."""
    state = case.root / "state"
    state.mkdir(mode=0o700)
    path = state / "query-cache.json"
    key, start = canonical([CLUSTER, PRINCIPAL, "sherlock-plain", "read", attempt_id(config, 0)]), CONTEXT.Event()
    outputs, invocations = ([case.root / f"query-{kind}{i}.json" for i in range(3)] for kind in ("", "invocation-"))
    now = 1000 + config["index"] * 100
    workers = [launch(query_worker, (path, key, start, str(outputs[i]), str(invocations[i]), config, now)) for i in config["worker_order"]]
    start.set()
    join(workers)
    require(sum(p.exists() for p in invocations) == 1, "equivalent query duplicated")
    answers = [read(p) for p in outputs]
    body = {"seed": config["seed"], "jobs": [config["job"]]}
    if config["query_failure"]:
        require(sorted(canonical(a) for a in answers) == sorted([canonical({"expected_failure": True})] + [canonical({"result": QUERY_SENTINEL})] * 2), "failed query did not leave the sentinel for the others")
    else:
        require(all(a.get("result") in (body, QUERY_SENTINEL) for a in answers) and sum(a.get("result") == body for a in answers) >= 1, "query result or sentinel missing")
    calls = []
    cached = cached_query(path, key, lambda: calls.append(1), now=now + config["cache_delta"])
    require(not calls and cached in (body, QUERY_SENTINEL), "query cadence bypassed")
    require(cached_query(path, key, lambda: calls.append(1) or body, now=now + 60) == body and calls == [1], "expired query cache did not refresh")
    require(forget_queries(path, [key]) == () and cached_query(path, key, lambda: calls.append(1) or body, now=now + 61) == body and calls == [1, 1], "forgotten scope was still served")
    require(stat.S_IMODE(path.stat().st_mode) == 0o600 and (state / "query-cache.json.lock").is_file(), "query cache not private or unlocked")
    require(not (state / "auth-backoff.json.lock").exists() and not (state / "auth-backoff.json").exists(), "query cache touched the transport cooldown state")
    return answers


def expected_resolution(config, plan, present, partition):
    """What resolve() must say about the generated rows, computed from the plan alone."""
    finals = [plan[i]["final"] for i in present]
    terminal = sum(final in {"COMPLETED", "FAILED"} for final in finals)
    tasks = {"count": len(plan), "terminal": terminal, "running": finals.count("RUNNING"), "pending": finals.count("PENDING"),
             "missing": len(plan) - len(present), "by_state": dict(sorted(Counter(finals).items())), "incomplete_history": []}
    if partition == "normal" and config["anomaly"]:
        resolution = "unexpected_preemption"
    else:
        resolution = "terminal" if terminal == len(plan) else "identified"
    return resolution, tasks


def generated_rows(config, attempt, created, plan, present):
    """sacct lines for the plan: restarts below the top are PREEMPTED, pending array tasks share one aggregate."""
    lines, cost, db_index = [], 0, 100
    pending = [i for i in present if plan[i]["final"] == "PENDING"]
    for i in present:
        item = plan[i]
        if item["final"] == "PENDING":
            if len(plan) == 1:
                lines.append(sacct_row(attempt, created, config["job"], state="PENDING", cpus=config["cpus"], db_index=db_index))
                db_index += 1
            continue
        task = i if len(plan) > 1 else None
        for restart in range(item["top"] + 1):
            state = item["final"] if restart == item["top"] else "PREEMPTED"
            elapsed = config["cost"] + restart + i
            lines.append(sacct_row(attempt, created, config["job"], state=state, cpus=config["cpus"], elapsed=elapsed, restart=restart, task=task, db_index=db_index))
            db_index += 1
            if state in FINISHED_STATES:
                cost += elapsed * config["cpus"]
    if pending and len(plan) > 1:
        lines.append(sacct_row(attempt, created, config["job"], state="PENDING", cpus=config["cpus"], indices=",".join(str(i) for i in pending), db_index=db_index))
    return lines, cost


def resolution_case(case, config):
    """A real submission, then randomized accounting through the shipped reader: counts, cost, idempotence, closures and waivers."""
    attempt = attempt_id(config, 0)
    plan, count = config["task_plan"], len(config["task_plan"])
    require(submit(case, config, attempt, count=count) == ("submitted", str(config["job"])), "fixture submission failed")
    created = read(case.registry / "attempts" / attempt / "record.json")["created"]
    present = list(range(count - 1 if config["missing_task"] and count > 1 else count))
    lines, expected_cost = generated_rows(config, attempt, created, plan, present)
    expected, tasks = expected_resolution(config, plan, present, config["partition"])
    stdout = "\n".join(lines) + "\n"
    _, result = read_attempt(case, attempt, stdout)
    require(result["resolution"] == expected, f"resolution {result['resolution']} != {expected}")
    require(result["tasks"] == tasks, f"task summary {result['tasks']} != {tasks}")
    require(result["cost"] == {"cpu_seconds": expected_cost, "gpu_seconds": 0, "known": expected == "terminal"}, f"cost {result['cost']} != {expected_cost}")
    require(result["job_id"] == str(config["job"]), "job identity lost")
    require(released(result) == (expected == "terminal" and tasks["by_state"].get("COMPLETED", 0) == 0), "released predicate disagrees with D5")
    # Identical duplicate rows, step rows and another attempt's rows change nothing.
    noise = [line.replace("|" + "shk-" + attempt + "|", "|shk-" + attempt_id(config, 9) + "|", 1) for line in lines[:1]]
    steps = [line.split("|")[0] + ".batch|" + "|".join(line.split("|")[1:]) for line in lines if "_[" not in line]
    _, again = read_attempt(case, attempt, "\n".join(lines + lines + steps + noise) + "\n")
    require(again == result, "duplicate accounting, steps or foreign rows changed the resolution")
    observed = [expected, expected_cost]
    if expected == "unexpected_preemption":
        require(event(case, "resolved", attempt, stdout) == ("refused", "not_applicable:unexpected_preemption"), "anomalous attempt closed")
        outcome, name = event(case, "ack", attempt, stdout, note="fixture waiver")
        require(outcome == "written" and name.startswith("ack-"), "waiver not written")
        waiver = read(case.registry / "attempts" / attempt / name)
        require(waiver["waived"] == {"0": {"restart": 1, "state": plan[0]["final"]}} and waiver["note"] == "fixture waiver", f"waiver names other tasks: {waiver['waived']}")
        expected, _ = expected_resolution({**config, "anomaly": False}, plan, present, config["partition"])
        _, result = read_attempt(case, attempt, stdout)
        require(result["resolution"] == expected and result["anomalies"] == ["acknowledged_preemption"], f"waiver did not resolve the anomaly: {result['anomalies']}")
        require(event(case, "ack", attempt, stdout) == ("refused", "not_applicable:" + expected), "second waiver accepted")
        observed.append("waived")
    else:
        require(result["anomalies"] == [], f"spurious anomaly flags on a regular plan: {result['anomalies']}")
        refusal = "preemptible_profile" if config["partition"] == "owners" else "not_applicable:" + expected
        require(event(case, "ack", attempt, stdout) == ("refused", refusal), "waiver accepted without an anomaly")
    if expected == "terminal":
        require(event(case, "resolved", attempt, stdout) == ("written", "resolved.json"), "terminal attempt not closed")
        require(event(case, "resolved", attempt, stdout) == ("refused", "already_present"), "closure written twice")
        require(open_attempts(case) == [], "closed attempt still open")
        require(read(case.registry / "attempts" / attempt / "resolved.json")["cost"]["cpu_seconds"] == expected_cost, "closure cost differs")
    else:
        require(event(case, "resolved", attempt, stdout) == ("refused", "not_applicable:" + expected), "open attempt closed")
        require(open_attempts(case) == [attempt], "open attempt missing from the listing")
    return observed


def protocol_case(root, config):
    case = prepare(root)
    family = config["family"]
    if family == "submit_race":
        observed, crashes = race_case(case, config), 0
    elif family == "runner_death":
        observed, crashes = death_case(case, config), 1
    elif family == "query_cache":
        return {"observed": query_case(case, config), "attempts": [], "crashes": 0, "invariants": ["single_query", "cadence", "private_state", family]}
    else:
        observed, crashes = resolution_case(case, config), 0
    attempts = check_registry(case)
    return {"observed": observed, "attempts": attempts, "crashes": crashes,
            "invariants": ["registry_private", "create_once", "marker_integrity", "no_sbatch_env", family]}


# ---------------------------------------------------------------- artifact families

def fetch_worker(source, destination, manifest, sidecar, output, config, start=None):
    if start is not None:
        require(start.wait(8), "fetch barrier timed out")

    def fault(point):
        if point == config.get("fetch_fault"):
            os._exit(CRASH)
    def transfer(stage, m):
        rsync_transfer(source, stage, m, timeout=8)
        if config.get("fetch_fault") == "partial_transfer":
            target = stage / m["files"][0]["path"]
            target.write_bytes(target.read_bytes()[: config["truncate"]])
            os._exit(CRASH)
    def validate(root):
        if config.get("reject") == "validator_false":
            return False
        if config.get("reject") == "validator_mutation":
            (root / "result.json").write_bytes(b"mutated")
        return (root / "result.json").read_text() == canonical({"seed": config["seed"], "job": config["job"]}) + "\n" or config.get("reject") == "validator_mutation"
    def source_manifest():
        if config.get("reject") == "changed_manifest":
            changed = json.loads(canonical(manifest))
            changed["producer"]["input_digest"] = "f" * 64
            return changed
        return manifest
    try:
        result = fetch_bundle(manifest, Path(destination), transfer, source_manifest=source_manifest, validator=validate,
                              validator_digest=sidecar["validator_digest"], expected_identity=manifest["producer"], fault=fault, attempt_record=sidecar)
        emit(output, {"ok": True, "recovered": result["recovered"], "receipt": result["receipt"]})
    except SafetyError as exc:
        if not config.get("reject"):
            raise
        emit(output, {"blocked": str(exc)})


def launch(target, args):
    process = CONTEXT.Process(target=target, args=args)
    process.start()
    return process


def join(processes, expected=None):
    # A hung protocol is a counterexample, never a reason to consume the budget.
    expected = expected or [0] * len(processes)
    try:
        for process, code in zip(processes, expected):
            process.join(10)
            require(not process.is_alive(), "worker deadline exceeded")
            require(process.exitcode == code, f"worker exited {process.exitcode}, expected {code}")
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(2)
                if process.is_alive():
                    process.kill()
                    process.join(2)
            process.close()


def artifact_case(root, config):
    source, destination = root / "source", root / "destination"
    source.mkdir()
    (source / "result.json").write_text(canonical({"seed": config["seed"], "job": config["job"]}) + "\n")
    rng = random.Random(config["seed"])
    for i in range(config["files"]):
        path = source / ("nested " + str(i % config["depth"])) / ("payload-'" + str(i) + "-λ.bin")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(rng.randbytes(config["bytes"] + i))
    producer = {key: getattr(spec(SimpleNamespace(script="/synthetic/job.sh", run_dir="/synthetic/run"), config), key)
                for key in ("cluster", "principal", "code_digest", "input_digest", "runtime_digest", "policy_digest")}
    producer["attempt"] = attempt_id(config, 0)
    manifest = build_manifest(source, producer)
    validator = {"validator_path": "/synthetic/immutable/validate.py", "validator_digest": digest([config["seed"], "validator"]), "validator_function": "validate"}
    sidecar = attempt_record(producer["attempt"], producer, validator, manifest)
    sidecar_path = root / ".destination.shk-attempt.json"
    output = root / "fetch.json"
    family = config["family"]
    crashes = 0
    if family == "fetch_death":
        join([launch(fetch_worker, (str(source), str(destination), manifest, sidecar, str(output), config))], [CRASH])
        crashes = 1
        require(destination.exists() == (config["fetch_fault"] in {"promoted", "receipt_committed"}), "partial final exposed")
        require(read(sidecar_path) == sidecar, "sidecar not durable before the transaction")
        if config["fetch_fault"] in {"transferred", "partial_transfer"}:
            stage = root / (".destination.shk-stage-" + digest(manifest))
            target = stage / manifest["files"][0]["path"]
            original = source / manifest["files"][0]["path"]
            data = original.read_bytes()
            target.write_bytes(bytes(b ^ 0x55 for b in data))
            os.utime(target, ns=(original.stat().st_atime_ns, original.stat().st_mtime_ns))
        clean = {**config, "fetch_fault": None}
        join([launch(fetch_worker, (str(source), str(destination), manifest, sidecar, str(output), clean))])
    elif family == "fetch_race":
        start = CONTEXT.Event()
        outputs = [root / f"fetch-{i}.json" for i in range(2)]
        workers = [launch(fetch_worker, (str(source), str(destination), manifest, sidecar, str(outputs[i]), config, start)) for i in config["worker_order"] if i < 2]
        start.set()
        join(workers)
        answers = [read(p) for p in outputs]
        require(sorted(a["recovered"] for a in answers) == [False, True], "promotion not serialized")
        output = outputs[0]
    else:
        if config["reject"] == "extra_stage":
            stage = root / (".destination.shk-stage-" + digest(manifest))
            stage.mkdir(mode=0o700)
            (stage / "unrelated").write_bytes(b"preserve")
        elif config["reject"] == "conflicting_final":
            destination.mkdir(mode=0o700)
            (destination / "unrelated").write_bytes(b"preserve")
        elif config["reject"] == "symlink_stage":
            stage = root / (".destination.shk-stage-" + digest(manifest))
            stage.mkdir(mode=0o700)
            (stage / "escape").symlink_to(source / "result.json")
        elif config["reject"] == "sidecar_conflict":
            foreign = attempt_record(attempt_id(config, 1), {**producer, "attempt": attempt_id(config, 1)}, validator, build_manifest(source, {**producer, "attempt": attempt_id(config, 1)}))
            sidecar_path.write_text(canonical(foreign) + "\n")
            sidecar_path.chmod(0o600)
        join([launch(fetch_worker, (str(source), str(destination), manifest, sidecar, str(output), config))])
        require("blocked" in read(output), "invalid bundle accepted")
        if config["reject"] == "conflicting_final":
            require((destination / "unrelated").read_bytes() == b"preserve", "conflicting final overwritten")
        else:
            require(not destination.exists(), "rejected bundle promoted")
        if config["reject"] == "sidecar_conflict":
            require(read(sidecar_path)["attempt"] == attempt_id(config, 1) and not (root / ".destination.shk-transaction.json").exists(), "foreign sidecar replaced or transaction opened")
        require(digest(build_manifest(source, producer)) == digest(manifest), "source changed")
        return {"observed": [config["reject"], "blocked"], "crashes": 0,
                "invariants": ["no_invalid_promotion", "source_immutable", "preserve_unrelated", "sidecar_pinned"]}
    verify_bundle(destination, manifest)
    require(digest(build_manifest(source, producer)) == digest(manifest), "source changed")
    receipt = read(root / ".destination.shk-receipt.json")
    require(receipt == read(root / ".destination.shk-transaction.json"), "receipt/intent disagree")
    require(receipt["manifest_sha256"] == digest(manifest) and receipt["producer"] == producer, "receipt identity mismatch")
    require(read(output)["receipt"] == receipt, "worker receipt mismatch")
    require([p.name for p in root.iterdir() if "attempt" in p.name] == [sidecar_path.name], "more or fewer than one attempt sidecar")
    require(read_attempt_sidecar(destination) == sidecar, "sidecar does not describe the promoted bundle")
    return {"observed": [read(output)["recovered"], digest(manifest)], "producer": producer,
            "validator_sha256": receipt["validator_sha256"], "crashes": crashes,
            "invariants": ["exact_inventory_hashes", "source_immutable", "receipt_identity", "atomic_promotion", "sidecar_pinned", family]}


# ---------------------------------------------------------------- case generation and the soak loop

def task_plan(rng, count, partition, anomaly):
    plan = []
    for _ in range(count):
        final = rng.choice(FINALS)
        top = rng.randint(0, 2) if partition == "owners" and final != "PENDING" else 0
        plan.append({"top": top, "final": final})
    if partition == "normal" and anomaly:
        plan[0] = {"top": 1, "final": rng.choice(FINALS[:3])}
    return plan


def configuration(seed, index):
    # Reproducible case-level generation; a failing index does not require replaying
    # thousands of preceding cases or preserving their private registries.
    case_seed = int(hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()[:16], 16)
    rng = random.Random(case_seed)
    family = FAMILIES[index % len(FAMILIES)]
    config = {"seed": case_seed, "index": index, "family": family, "cpus": rng.randint(1, 3),
        "memory_mb": rng.randint(128, 2048), "walltime": rng.randint(61, 900),
        "job": rng.randint(10000, 99999999), "cost": rng.randint(1, 200), "same_task": bool(rng.getrandbits(1)),
        "sbatch_sleep": round(rng.random() * 0.15, 3), "death_mode": rng.choice(("after_accept", "before_reply")),
        "death_state": rng.choice(("COMPLETED", "FAILED", None)), "query_failure": bool(rng.getrandbits(1)), "cache_delta": rng.randrange(60),
        "partition": rng.choice(("normal", "owners")), "count": rng.choice((1, 2, 3, 4, 6)), "anomaly": bool(rng.getrandbits(1)),
        "missing_task": bool(rng.getrandbits(1)), "fetch_fault": rng.choice((*FAULTS, "partial_transfer")), "truncate": rng.randrange(16),
        "files": rng.randint(1, 6), "bytes": rng.randint(32, 32768), "depth": rng.randint(1, 3),
        "reject": rng.choice(("validator_false", "validator_mutation", "changed_manifest", "extra_stage", "conflicting_final", "symlink_stage", "sidecar_conflict")),
        "worker_order": rng.sample(range(3), 3)}
    config["task_plan"] = task_plan(rng, config["count"], config["partition"], config["anomaly"]) if family == "resolution" else []
    if family != "resolution":
        config["anomaly"] = False
        config["missing_task"] = False
    if family != "fetch_death":
        config["fetch_fault"] = None
    if family != "fetch_reject":
        config["reject"] = None
    return config


def semantic_bucket(config):
    family = config["family"]
    detail = {"submit_race": config["same_task"], "runner_death": (config["death_mode"], config["death_state"]), "query_cache": config["query_failure"],
              "resolution": (config["partition"], config["anomaly"], config["missing_task"], len(config["task_plan"]) > 1),
              "fetch_death": config["fetch_fault"], "fetch_reject": config["reject"]}.get(family, "default")
    return (family, detail)


def atomic_report(path, report):
    temporary = path.with_suffix(".pending")
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def module_hashes():
    paths = [Path(sys.modules[name].__file__) for name in ("sherlock_registry", "sherlock_orchestration", "sherlock_artifacts", "sherlock_commands")]
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def run(output: Path, *, duration_seconds=600, seed=20261007, max_cases=MAX_CASES, replay_index=None):
    require(0 < duration_seconds <= 600, "duration must be in (0, 600]")
    require(1 <= max_cases <= MAX_CASES, "case count outside bounded limit")
    require(replay_index is None or 0 <= replay_index < MAX_CASES, "replay index outside bounded limit")
    for name in THREAD_VARS:
        os.environ[name] = "1"
    # The caller supplies node-local storage inside an allocation. No default home,
    # shared registry or production local state root is used.
    output = output.absolute()
    require(not output.exists() and not output.is_symlink(), "output must be fresh")
    require(output.parent.is_dir(), "output parent must exist")
    require(all(not p.is_symlink() for p in (output.parent, *output.parent.parents)), "output ancestor is a symlink")
    output.mkdir(mode=0o700)
    started = time.monotonic()
    cpu_start = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    rsync = subprocess.run(["rsync", "--version"], capture_output=True, text=True, check=True, timeout=5).stdout.splitlines()[0]
    report = {"schema_version": 2, "status": "running", "seed": seed, "duration_budget_seconds": duration_seconds,
        "scheduler_evidence": "synthetic; fake sbatch/sacct/squeue on a private PATH, no child Slurm jobs, no SSH", "live_integration": False,
        "scientific_acceptance": False, "max_workers": 3, "max_live_payload_bytes": 6 * 32773, "max_case_log_bytes": MAX_LOG_BYTES,
        "thread_limits": {name: os.environ[name] for name in THREAD_VARS}, "policy_identity": policy_identity(),
        "python": sys.version, "rsync": rsync, "fixture_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "module_sha256": module_hashes(),
        "registry_program_sha256": program_sha256(), "registry_program_bytes": sum(len(part.encode()) for part in program_argv("read")),
        "case_count": 0, "crash_count": 0, "coverage": {}, "invariant_counts": {}, "case_chain_sha256": "0" * 64,
        "novel_semantic_buckets": 0, "revisited_semantic_buckets": 0}
    report_path = output / "report.json"
    atomic_report(report_path, report)
    coverage, invariants, semantic = Counter(), Counter(), set()
    log = output / "cases.jsonl"
    case_root = None
    try:
        with open(log, "x", encoding="utf-8") as stream:
            for offset in range(max_cases):
                # Cases finish before another starts; the last bounded case can
                # overrun the generation budget by its worker/rsync deadline.
                if offset and time.monotonic() - started >= duration_seconds:
                    report["stop_reason"] = "generation_deadline"
                    break
                index = replay_index if replay_index is not None else offset
                config = configuration(seed, index)
                case_root = output / "active-case"
                case_root.mkdir(mode=0o700)
                case_start = time.monotonic()
                outcome = artifact_case(case_root, config) if config["family"].startswith("fetch_") else protocol_case(case_root, config)
                key = canonical(semantic_bucket(config))
                semantic.add(key)
                coverage[key] += 1
                invariants.update(outcome["invariants"])
                record = {"config": config, "outcome": outcome, "elapsed_seconds": time.monotonic() - case_start}
                body = canonical(record)
                require(stream.tell() + len(body.encode()) + 1 <= MAX_LOG_BYTES, "case log limit reached")
                stream.write(body + "\n")
                stream.flush()
                os.fsync(stream.fileno())
                report["case_chain_sha256"] = hashlib.sha256((report["case_chain_sha256"] + body).encode()).hexdigest()
                report["case_count"] += 1
                report["crash_count"] += outcome["crashes"]
                report["coverage"] = dict(coverage)
                report["invariant_counts"] = dict(invariants)
                report["novel_semantic_buckets"] = len(semantic)
                report["revisited_semantic_buckets"] = report["case_count"] - len(semantic)
                report["elapsed_seconds"] = time.monotonic() - started
                atomic_report(report_path, report)
                shutil.rmtree(case_root)  # Exact, fixture-owned bounded outputs only.
                case_root = None
                if replay_index is not None:
                    report["stop_reason"] = "counterexample_replay"
                    break
            else:
                report["stop_reason"] = "case_limit"
        report["status"] = "passed"
        report["coverage_limit"] = "Fixed semantic buckets saturate; subsequent unique parameter draws measure race/failure robustness, not new semantic coverage."
    except BaseException as exc:
        report["status"] = "failed"
        report["counterexample"] = {"config": config if "config" in locals() else None,
            "error": repr(exc), "traceback": traceback.format_exc(), "retained_fixture": str(case_root) if case_root else None}
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        for label, who, baseline in zip(("parent", "children"), (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN), cpu_start):
            usage = resource.getrusage(who)
            report[label + "_cpu_seconds"] = usage.ru_utime + usage.ru_stime - baseline.ru_utime - baseline.ru_stime
            report[label + "_maxrss_kib"] = usage.ru_maxrss  # Linux units; children include the stub processes and rsync.
        atomic_report(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--duration-seconds", type=float, default=600)
    parser.add_argument("--seed", type=int, default=20261007)
    parser.add_argument("--max-cases", type=int, default=MAX_CASES)
    parser.add_argument("--replay-index", type=int)
    args = parser.parse_args()
    report = run(args.output, duration_seconds=args.duration_seconds, seed=args.seed,
                 max_cases=args.max_cases, replay_index=args.replay_index)
    print(canonical({k: report[k] for k in ("status", "case_count", "crash_count", "elapsed_seconds", "novel_semantic_buckets", "revisited_semantic_buckets", "stop_reason")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
