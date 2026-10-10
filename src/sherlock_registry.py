"""Slurm-backed attempt registry: resolution core, registry files and the remote program.

The workstation CLI imports this module for the pure resolution logic and ships
its exact source to the login node on every call as a self-decoding
``python3 -c`` program, so runner, reader and event writer resolve attempts with
the same code against a query taken on the login node under the registry lock.
The module imports no sibling module, uses only the standard library and never
touches ``__file__`` at module level or inside ``main``: when exec'd through the
stub there is none. Slurm accounting and the create-once files under
``registry_root`` are the only durable attempt state; nothing is ever
read-modified-written.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
import zlib

SCHEMA_VERSION = 1
SUBMIT_TIME_TOLERANCE_SECONDS = 300
BASE_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL", "DEADLINE"})
PREEMPTION_STATES = frozenset({"PREEMPTED", "REQUEUED"})
# A row in one of these states has left its allocation; with an End time its accounting is final for that restart.
FINISHED_STATES = BASE_TERMINAL | PREEMPTION_STATES
PENDING_STATES = frozenset({"PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "RESV_DEL_HOLD"})
FIELDS = ("JobID", "JobIDRaw", "User", "JobName", "State", "ElapsedRaw", "AllocCPUS", "AllocTRES", "Submit", "Start", "End", "Restarts", "ExitCode", "Cluster", "DBIndex")
WIDE_FIELDS = frozenset({"JobName", "User", "Cluster"})
ABANDON_SECONDS = 900
MAX_OPEN = 500
MAX_ROWS = 50_000
LOCK_TIMEOUT_SECONDS = 20
LOCK_POLL_SECONDS = 0.05
QUERY_TIMEOUT_SECONDS = 15
PROGRAM_LIMIT = 100_000
OUTPUT_LIMIT = 4096
EVENT_LIMIT = 64 * 1024
RECORD_LIMIT = 256 * 1024
MARKER_LIMIT = 1024
MANIFEST_LIMIT = 4 * 1024 * 1024
HEX32 = re.compile(r"[0-9a-f]{32}")
DIGEST = re.compile(r"[0-9a-f]{64}")
IDENT = re.compile(r"[A-Za-z0-9_.-]{1,128}")
NUMBER = re.compile(r"[1-9][0-9]*")
JOB_ID = re.compile(r"^(?P<job>[1-9][0-9]*)(?:_(?:(?P<task>[0-9]+)|\[(?P<spec>[0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*)(?:%[0-9]+)?\]))?$")
SBATCH_REPLY = re.compile(r"^([1-9][0-9]*)(?:;([A-Za-z0-9_.-]+))?\s*$")
SBATCH_DIRECTIVE = re.compile(rb"^\s*#SBATCH", re.M)
ACK_FILE = re.compile(r"ack-[0-9]+\.json")
SINGLE_EVENTS = ("submitted", "not_sent", "abandoned", "resolved")
CLOSING_FILES = frozenset({"not_sent.json", "abandoned.json", "resolved.json"})
STAMPED_KEYS = ("created", "created_on", "principal_uid", "program_sha256")
PROFILE_FLAGS = ("preemptible", "requeue", "borrowed", "gpus_allowed")
STUB_TEMPLATE = "import base64,sys,zlib;exec(compile(zlib.decompress(base64.b64decode('{}')),'sherlock_registry','exec'))"
REASONS = {"not_sent": "sbatch never executed for this attempt", "abandoned": "abandoned by the operator after the accounting window stayed empty",
           "inconclusive": "absence/lag/retention never proves non-submission", "abandonable": "no accounting rows after 900 s and no known job id",
           "unexpected_preemption": "restart or preemption on a non-preemptible partition; investigate, then acknowledge"}


class RegistryError(ValueError):
    """A registry or accounting contract cannot be established; the message is a reason token."""


# ---------------------------------------------------------------- shared constants and helpers

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def terminal_states(resources, profile):
    """States that release an attempt: PREEMPTED only ends a preemptible job that is not requeued."""
    if profile["preemptible"] and not resources.get("requeue", False):
        return BASE_TERMINAL | {"PREEMPTED"}
    return BASE_TERMINAL


def task_key(spec):
    """The logical task of a spec: one marker file per key under tasks/."""
    return digest([spec["project"], spec["campaign"], spec["task"]])


def epoch(text):
    if not text or text in {"Unknown", "None", "N/A"}:
        return None
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


# ---------------------------------------------------------------- sacct rows

def sacct_argv(principal, created, selector):
    """Bounded accounting query: this principal, from the admission window, one --name selector."""
    lower = datetime.fromtimestamp(created - SUBMIT_TIME_TOLERANCE_SECONDS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return ["env", "TZ=UTC", "LC_ALL=C", "SLURM_TIME_FORMAT=standard", "sacct", "-n", "-P", "--local", "--allocations",
            "--user=" + principal, "--duplicates", "--starttime=" + lower, "--endtime=now",
            "--format=" + ",".join(field + "%128" if field in WIDE_FIELDS else field for field in FIELDS), selector]


def parse_rows(stdout):
    """Raw bounded sacct rows, one dict per allocation line; the shape is checked, identity is not."""
    rows = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        columns = line.split("|")
        if len(columns) == len(FIELDS) + 1 and not columns[-1]:
            columns.pop()
        if len(columns) != len(FIELDS):
            raise RegistryError("malformed bounded accounting response")
        rows.append(dict(zip(FIELDS, columns)))
    return rows


def parse_tres(text):
    tres = {}
    for part in text.split(",") if text else ():
        if "=" not in part:
            raise RegistryError("malformed_accounting")
        key, value = part.split("=", 1)
        if key in tres:
            raise RegistryError("malformed_accounting")
        tres[key] = value
    return tres


def gpu_allocation(tres):
    """Allocated GPUs from generic and typed TRES, or None when neither is reported; a disagreement is refused."""
    try:
        typed = [int(value) for key, value in tres.items() if key.startswith("gres/gpu:")]
        generic = int(tres["gres/gpu"]) if "gres/gpu" in tres else None
    except ValueError:
        raise RegistryError("malformed_accounting") from None
    if generic is not None and typed and generic != sum(typed):
        raise RegistryError("gpu_accounting_conflict")
    if generic is None and not typed:
        return None
    return generic if generic is not None else sum(typed)


def expand_indices(spec):
    """Sorted distinct task indices of an aggregate specification such as ``0-3,7``."""
    indices = set()
    for part in spec.split(","):
        lower, _, upper = part.partition("-")
        if not lower.isdigit() or (upper and not upper.isdigit()):
            raise RegistryError("invalid_job_id")
        first, last = int(lower), int(upper or lower)
        if last < first:
            raise RegistryError("invalid_job_id")
        indices.update(range(first, last + 1))
    return sorted(indices)


def parse_job_id(text):
    """(job, task, indices): plain job, ``N_k`` task k, or ``N_[spec%t]`` expanded aggregate."""
    match = JOB_ID.match(text)
    if match is None:
        raise RegistryError("invalid_job_id")
    if match.group("task") is not None:
        return match.group("job"), int(match.group("task")), None
    if match.group("spec") is not None:
        return match.group("job"), None, expand_indices(match.group("spec"))
    return match.group("job"), None, None


def _task_count(record):
    array = record["spec"]["resources"].get("array")
    return array["count"] if array else 1


def _row_times(item, submit):
    try:
        start, end = epoch(item["Start"]), epoch(item["End"])
    except ValueError:
        raise RegistryError("malformed_accounting") from None
    if start is not None and start < submit - 5:
        raise RegistryError("start_predates_submit")
    if end is not None and start is not None and end < start:
        raise RegistryError("time_conflict")
    return start, end


def _row_shape(item, record, count):
    job, task, indices = parse_job_id(item["JobID"])
    if not NUMBER.fullmatch(item["JobIDRaw"]):
        raise RegistryError("invalid_allocation_identity")
    if not NUMBER.fullmatch(item["DBIndex"]):
        raise RegistryError("db_index_missing")
    array = record["spec"]["resources"].get("array")
    if array is None:
        if task is not None or indices is not None:
            raise RegistryError("shape_conflict")
        return job, 0, None
    if task is None and indices is None:
        raise RegistryError("shape_conflict")
    if any(index >= count for index in ([task] if task is not None else indices)):
        raise RegistryError("array_index_outside_frozen_count")
    return job, task, indices


def _normalised_row(record, item, count):
    spec = record["spec"]
    resources = spec["resources"]
    job, task, indices = _row_shape(item, record, count)
    if item["User"] != spec["principal"] or item["Cluster"] != spec["cluster"]:
        raise RegistryError("ownership_conflict")
    try:
        submit = epoch(item["Submit"])
    except ValueError:
        raise RegistryError("malformed_accounting") from None
    if submit is None or submit < record["created"] - SUBMIT_TIME_TOLERANCE_SECONDS:
        raise RegistryError("stale_submit")
    words = item["State"].split()
    if not words:
        raise RegistryError("malformed_accounting")
    state = words[0].rstrip("+")
    try:
        elapsed, cpus, restart = (int(item[key]) for key in ("ElapsedRaw", "AllocCPUS", "Restarts"))
    except ValueError:
        raise RegistryError("malformed_accounting") from None
    gpus = gpu_allocation(parse_tres(item["AllocTRES"]))
    if min(elapsed, cpus, restart, 0 if gpus is None else gpus) < 0:
        raise RegistryError("negative_accounting")
    start, end = _row_times(item, submit)
    if start is not None and cpus != resources["cpus"] * resources["tasks"]:
        raise RegistryError("allocated_cpus_differ")
    complete = state in FINISHED_STATES and end is not None and (start is not None or (elapsed == 0 and cpus == 0))
    if resources["gpus"] > 0 and gpus is None and start is not None:
        complete = False
    cpu_seconds, gpu_seconds = elapsed * cpus, elapsed * (gpus or 0)
    if indices is not None:
        # Pending aggregates never ran; a cancelled aggregate is terminal tasks that cost nothing.
        complete, cpu_seconds, gpu_seconds = state in BASE_TERMINAL, 0, 0
    return {"job": job, "task": task, "indices": indices, "raw": item["JobIDRaw"], "db_index": item["DBIndex"], "state": state,
            "restart": restart, "elapsed": elapsed, "cpus": cpus, "gpus": gpus, "submit": submit, "start": start, "end": end,
            "complete": complete, "cpu_seconds": cpu_seconds, "gpu_seconds": gpu_seconds, "exit_code": item["ExitCode"]}


def normalise_rows(record, rows):
    """This attempt's allocation rows checked against the frozen spec; steps and other names are skipped."""
    name = "shk-" + record["attempt"]
    count = _task_count(record)
    by_index = {}
    for item in rows:
        if "." in item["JobID"] or "." in item["JobIDRaw"] or item["JobName"] != name:
            continue
        row = _normalised_row(record, item, count)
        previous = by_index.get(row["db_index"])
        if previous is None:
            by_index[row["db_index"]] = row
        elif previous != row:
            raise RegistryError("duplicate_accounting_conflicts")
    return list(by_index.values())


# ---------------------------------------------------------------- resolution

def _checked_events(events):
    """({kind: body} for single-instance kinds, [ack bodies]); duplicates and malformed bodies are contract errors."""
    single, acks = {}, []
    for item in events:
        kind, body = item.get("kind"), item.get("body")
        if not isinstance(body, dict):
            raise RegistryError("malformed_event:" + str(kind))
        if kind == "ack":
            waived = body.get("waived")
            if not isinstance(waived, dict) or any(not isinstance(entry, dict) or type(entry.get("restart")) is not int for entry in waived.values()):
                raise RegistryError("malformed_event:ack")
            acks.append(body)
            continue
        if kind not in SINGLE_EVENTS:
            raise RegistryError("unknown_event_kind")
        if kind in single:
            raise RegistryError("duplicate_event")
        single[kind] = body
    submitted = single.get("submitted")
    if submitted is not None:
        job_id = submitted.get("job_id")
        if job_id is not None and not (isinstance(job_id, str) and NUMBER.fullmatch(job_id)):
            raise RegistryError("malformed_event:submitted")
    return single, acks


def _empty_tasks(count):
    return {"count": count, "terminal": 0, "running": 0, "pending": 0, "missing": count, "by_state": {}, "incomplete_history": []}


def _result(attempt, resolution, job_id, tasks, cost, anomalies, reason):
    return {"attempt": attempt, "resolution": resolution, "job_id": job_id, "tasks": tasks, "cost": cost,
            "anomalies": sorted(anomalies), "reason": reason}


def _task_groups(normalised, count, anomalies):
    """{task: {restart: [rows]}}; an explicit N_k row beats an aggregate covering the same index."""
    explicit, aggregate = {}, {}
    for row in normalised:
        if row["indices"] is None:
            explicit.setdefault(row["task"], {}).setdefault(row["restart"], []).append(row)
            continue
        for index in row["indices"]:
            aggregate.setdefault(index, {}).setdefault(row["restart"], []).append({**row, "task": index})
    if set(explicit) & set(aggregate):
        anomalies.add("aggregate_overlap")
    return {task: explicit[task] if task in explicit else aggregate[task] for task in range(count) if task in explicit or task in aggregate}


def _group_state(rows):
    states = {row["state"] for row in rows}
    if len(states) > 1 and states & (BASE_TERMINAL | {"PREEMPTED"}):
        raise RegistryError("terminal_state_conflict")
    return max(rows, key=lambda row: int(row["db_index"]))["state"]


def _group_cost(rows):
    """Agreed (cpu, gpu) of one restart's complete rows, or None while that restart is still accounted."""
    costs = {(row["cpu_seconds"], row["gpu_seconds"]) for row in rows if row["complete"]}
    if len(costs) > 1:
        raise RegistryError("accounting_conflicts")
    return costs.pop() if costs else None


def _analyse_task(task, groups, resources, profile, acks):
    top = max(groups)
    state = _group_state(groups[top])
    history_complete = set(groups) == set(range(top + 1))
    waived = any(ack["waived"].get(str(task), {}).get("restart", -1) >= top for ack in acks)
    terminal = terminal_states(resources, profile) | ({"PREEMPTED"} if waived else frozenset())
    if state in terminal and history_complete:
        status = "terminal"
    elif state in PENDING_STATES or (state == "PREEMPTED" and not waived):
        status = "pending"
    else:
        status = "running"
    costs = [_group_cost(rows) for rows in groups.values()]
    states = {row["state"] for rows in groups.values() for row in rows}
    return {"task": task, "top": top, "state": state, "status": status, "waived": waived, "history_complete": history_complete,
            "complete": all(cost is not None for cost in costs), "cpu_seconds": sum(cost[0] for cost in costs if cost),
            "gpu_seconds": sum(cost[1] for cost in costs if cost), "preempted": bool(states & PREEMPTION_STATES), "requeued": "REQUEUED" in states}


def _task_summary(details, count, anomalies):
    by_state, incomplete = {}, []
    for detail in details:
        by_state[detail["state"]] = by_state.get(detail["state"], 0) + 1
        if not detail["history_complete"] and detail["status"] != "terminal":
            incomplete.append(detail["task"])
    if incomplete:
        anomalies.add("restart_gap")
    return {"count": count, "terminal": sum(d["status"] == "terminal" for d in details), "running": sum(d["status"] == "running" for d in details),
            "pending": sum(d["status"] == "pending" for d in details), "missing": count - len(details),
            "by_state": dict(sorted(by_state.items())), "incomplete_history": incomplete[:20]}


def _anomalous(detail):
    return not detail["waived"] and (detail["top"] > 0 or detail["preempted"])


def _attempt_resolution(resources, profile, details, tasks, anomalies):
    if not profile["preemptible"]:
        for detail in filter(_anomalous, details):
            if detail["top"] > 0:
                anomalies.add("restart_on_non_preemptible")
            if detail["preempted"]:
                anomalies.add("preempted_on_non_preemptible")
        if anomalies & {"restart_on_non_preemptible", "preempted_on_non_preemptible"}:
            return "unexpected_preemption"
    elif not resources.get("requeue", False) and any(detail["requeued"] for detail in details):
        anomalies.add("requeued_without_requeue")
    return "terminal" if tasks["terminal"] == tasks["count"] else "identified"


def _analyse(record, events, rows, now):
    """(result, per-task details) for a record whose rows and events are trusted enough to parse."""
    attempt, spec = record["attempt"], record["spec"]
    resources, profile = spec["resources"], spec["partition_profile"]
    count = _task_count(record)
    single, acks = _checked_events(events)
    normalised = normalise_rows(record, rows)
    zero = {"cpu_seconds": 0, "gpu_seconds": 0, "known": False}
    for kind in ("not_sent", "abandoned"):
        if kind in single:
            if normalised:
                raise RegistryError("rows_for_" + kind + "_attempt")
            return _result(attempt, kind, None, _empty_tasks(count), zero, [], REASONS[kind]), []
    submitted = single.get("submitted")
    submitted_job = submitted.get("job_id") if submitted else None
    anomalies = set()
    if submitted and submitted.get("cluster") not in (None, spec["cluster"]):
        anomalies.add("cluster_suffix_mismatch")
    jobs = {row["job"] for row in normalised}
    if len(jobs) > 1:
        raise RegistryError("multiple_scheduler_identities")
    if jobs and submitted_job and submitted_job not in jobs:
        raise RegistryError("job_id_conflict")
    job_id = next(iter(jobs)) if jobs else submitted_job
    if not normalised:
        resolution = "inconclusive" if submitted_job or now - record["created"] < ABANDON_SECONDS else "abandonable"
        if "resolved" in single:
            anomalies.add("reopened_after_resolved")
        return _result(attempt, resolution, job_id, _empty_tasks(count), zero, anomalies, REASONS[resolution]), []
    if submitted is None:
        anomalies.add("submitted_unrecorded")
    if acks:
        anomalies.add("acknowledged_preemption")
    groups = _task_groups(normalised, count, anomalies)
    details = [_analyse_task(task, groups[task], resources, profile, acks) for task in sorted(groups)]
    tasks = _task_summary(details, count, anomalies)
    resolution = _attempt_resolution(resources, profile, details, tasks, anomalies)
    if "resolved" in single and resolution != "terminal":
        anomalies.add("reopened_after_resolved")
    cost = {"cpu_seconds": sum(d["cpu_seconds"] for d in details), "gpu_seconds": sum(d["gpu_seconds"] for d in details),
            "known": resolution == "terminal" and all(d["complete"] for d in details) and tasks["missing"] == 0}
    return _result(attempt, resolution, job_id, tasks, cost, anomalies, REASONS.get(resolution)), details


def resolve(record, events, rows, now):
    """Pure resolution of one attempt from its record, registry events and raw sacct rows.

    Contract violations become ``resolution "error"`` with a reason token so a batch
    can isolate them; nothing here reads the filesystem or the clock.
    """
    attempt = record.get("attempt") if isinstance(record, dict) else None
    try:
        return _analyse(record, events, rows, now)[0]
    except RegistryError as exc:
        reason = str(exc)
    except (KeyError, TypeError, AttributeError, ValueError):
        reason = "malformed_record"
    try:
        count = _task_count(record)
    except (KeyError, TypeError, AttributeError):
        count = 1
    return _result(attempt, "error", None, _empty_tasks(count), {"cpu_seconds": 0, "gpu_seconds": 0, "known": False}, [], reason)


def released(resolution):
    """A logical task may be retried only after nothing of it can still complete; absence is never release."""
    kind = resolution["resolution"]
    return kind in {"not_sent", "abandoned"} or (kind == "terminal" and resolution["tasks"]["by_state"].get("COMPLETED", 0) == 0)


# ---------------------------------------------------------------- registry files

def attempt_directory(root, attempt):
    return Path(root) / "attempts" / attempt


def marker_path(root, key):
    return Path(root) / "tasks" / key


def lock_path(root):
    return Path(root) / ".lock"


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private_tmp(path, data):
    """A complete, fsynced private temporary file next to ``path``; the caller links or replaces it."""
    tmp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _unlink_quietly(tmp)
        raise
    return tmp


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def write_once(path, data):
    """Create ``path`` exactly once with complete content; FileExistsError is the create-once guard."""
    path = Path(path)
    if path.is_symlink():
        raise OSError("refusing to write through a symlink: " + str(path))
    tmp = _write_private_tmp(path, data)
    try:
        os.link(tmp, path)
    finally:
        _unlink_quietly(tmp)
    _fsync_directory(path.parent)


def replace_marker(path, data):
    """Atomically replace a task marker with complete content (the only file ever rewritten)."""
    path = Path(path)
    if path.is_symlink():
        raise OSError("refusing to replace a symlink: " + str(path))
    tmp = _write_private_tmp(path, data)
    try:
        os.replace(tmp, path)
    except BaseException:
        _unlink_quietly(tmp)
        raise
    _fsync_directory(path.parent)


def read_bounded(path, limit):
    """Bytes of a regular, non-symlink file no larger than ``limit``."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise RegistryError("not_regular")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise RegistryError("oversized")
    return data


def load_json(path, limit):
    data = read_bounded(path, limit)
    try:
        value = json.loads(data)
    except ValueError:
        raise RegistryError("malformed") from None
    if not isinstance(value, dict):
        raise RegistryError("malformed")
    return value


def _checked_private_directory(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RegistryError("registry_unsafe")


def checked_root(root_text):
    """An absolute, owned 0700 registry root without symlinks; attempts/ and tasks/ are created if absent."""
    root = Path(root_text)
    if not root.is_absolute() or any(ancestor.is_symlink() for ancestor in root.parents):
        raise RegistryError("registry_unsafe")
    for directory in (root, root / "attempts", root / "tasks"):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        except OSError:
            raise RegistryError("registry_unsafe") from None
        _checked_private_directory(directory)
    return root


def existing_root(root_text):
    """checked_root's checks without creating anything: an event writer never materialises a registry."""
    root = Path(root_text)
    if not root.is_absolute() or any(ancestor.is_symlink() for ancestor in root.parents):
        raise RegistryError("registry_unsafe")
    for directory in (root, root / "attempts", root / "tasks"):
        try:
            _checked_private_directory(directory)
        except FileNotFoundError:
            raise RegistryError("unknown_attempt") from None
        except OSError:
            raise RegistryError("registry_unsafe") from None
    return root


def acquire_lock(root):
    """flock on .lock, polled up to LOCK_TIMEOUT_SECONDS; the descriptor is returned to release_lock."""
    try:
        fd = os.open(lock_path(root), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        # ELOOP (a symlinked .lock), EISDIR, EACCES: the lock file fails the regular/owned check.
        raise RegistryError("registry_unsafe") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise RegistryError("registry_unsafe")
        deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RegistryError("registry_busy") from None
                time.sleep(LOCK_POLL_SECONDS)
    except BaseException:
        os.close(fd)
        raise


def release_lock(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def event_kind(name):
    if name in {kind + ".json" for kind in SINGLE_EVENTS}:
        return name[:-5]
    return "ack" if ACK_FILE.fullmatch(name) else None


def _read_marker(root, key):
    try:
        return read_bounded(marker_path(root, key), MARKER_LIMIT).decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return None


def load_attempt(root, attempt):
    """{attempt, exists, record|null, events[{name, kind, body}], marker, errors[]} without raising."""
    entry = {"attempt": attempt, "exists": False, "record": None, "events": [], "marker": None, "errors": []}
    if not HEX32.fullmatch(attempt):
        entry["errors"].append("invalid_attempt_id")
        return entry
    directory = attempt_directory(root, attempt)
    try:
        if not stat.S_ISDIR(os.lstat(directory).st_mode):
            raise RegistryError("attempt_not_directory")
    except FileNotFoundError:
        entry["errors"].append("attempt_missing")
        return entry
    except (OSError, RegistryError) as exc:
        entry["errors"].append(str(exc))
        return entry
    entry["exists"] = True
    try:
        record = load_json(directory / "record.json", RECORD_LIMIT)
        if record.get("attempt") != attempt:
            raise RegistryError("attempt_mismatch")
        entry["record"] = record
    except (OSError, RegistryError) as exc:
        entry["errors"].append("record.json:" + ("missing" if isinstance(exc, FileNotFoundError) else str(exc)))
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        entry["errors"].append("listing:" + str(exc))
        names = []
    for name in names:
        if name == "record.json" or (name.startswith(".") and name.endswith(".tmp")):
            continue
        kind = event_kind(name)
        if kind is None:
            entry["errors"].append("unexpected_file:" + name)
            continue
        try:
            entry["events"].append({"name": name, "kind": kind, "body": load_json(directory / name, EVENT_LIMIT)})
        except (OSError, RegistryError) as exc:
            entry["errors"].append(name + ":" + str(exc))
    key = (entry["record"] or {}).get("key")
    if isinstance(key, str) and DIGEST.fullmatch(key):
        try:
            entry["marker"] = _read_marker(root, key)
        except (OSError, RegistryError) as exc:
            entry["errors"].append("marker:" + str(exc))
    return entry


# ---------------------------------------------------------------- bounded Slurm queries

def run_bounded(argv):
    """A query with QUERY_TIMEOUT_SECONDS; {status, returncode, stdout, stderr} with status complete|failed|timeout|unavailable."""
    try:
        process = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=QUERY_TIMEOUT_SECONDS)
    except OSError as exc:
        return {"status": "unavailable", "returncode": None, "stdout": b"", "stderr": str(exc).encode()}
    except subprocess.TimeoutExpired as exc:
        return {"status": "timeout", "returncode": None, "stdout": exc.stdout or b"", "stderr": exc.stderr or b""}
    return {"status": "complete" if process.returncode == 0 else "failed", "returncode": process.returncode, "stdout": process.stdout, "stderr": process.stderr}


def query_rows(principal, created, attempts):
    """One sacct call by job name for the given attempts; rows are parsed, never interpreted."""
    argv = sacct_argv(principal, created, "--name=" + ",".join("shk-" + attempt for attempt in attempts))
    result = run_bounded(argv)
    report = {"status": result["status"], "returncode": result["returncode"], "argv": argv, "rows": [], "row_count": 0, "truncated": False,
              "stderr_tail": result["stderr"].decode("utf-8", "replace")[-OUTPUT_LIMIT:]}
    if result["status"] != "complete":
        return report
    try:
        rows = parse_rows(result["stdout"].decode("utf-8", "replace"))
    except RegistryError:
        report["status"] = "malformed"
        return report
    report.update(rows=rows[:MAX_ROWS], row_count=min(len(rows), MAX_ROWS), truncated=len(rows) > MAX_ROWS)
    return report


def _created_of(record):
    created = record.get("created") if record else None
    return created if type(created) in (int, float) else None


# ---------------------------------------------------------------- submit runner

def _checked_resources(resources):
    if not isinstance(resources, dict):
        raise RegistryError("record_invalid")
    for name, lower in (("cpus", 1), ("tasks", 1), ("gpus", 0)):
        if type(resources.get(name)) is not int or resources[name] < lower:
            raise RegistryError("record_invalid")
    if type(resources.get("requeue")) is not bool:
        raise RegistryError("record_invalid")
    array = resources.get("array")
    if array is None:
        return
    count = array.get("count") if isinstance(array, dict) else None
    if type(count) is not int or not 2 <= count <= 1000 or set(array) - {"count", "throttle"}:
        raise RegistryError("record_invalid")
    if "throttle" in array and (type(array["throttle"]) is not int or not 1 <= array["throttle"] <= count):
        raise RegistryError("record_invalid")


def _checked_spec(spec):
    if not isinstance(spec, dict):
        raise RegistryError("record_invalid")
    for name in ("project", "campaign", "task", "cluster", "principal"):
        if not isinstance(spec.get(name), str) or not IDENT.fullmatch(spec[name]):
            raise RegistryError("record_invalid")
    _checked_resources(spec.get("resources"))
    profile = spec.get("partition_profile")
    if not isinstance(profile, dict) or any(type(profile.get(flag)) is not bool for flag in PROFILE_FLAGS):
        raise RegistryError("record_invalid")
    if not isinstance(spec.get("remote_script"), str) or not spec["remote_script"].startswith("/"):
        raise RegistryError("record_invalid")
    if not isinstance(spec.get("script_digest"), str) or not DIGEST.fullmatch(spec["script_digest"]):
        raise RegistryError("record_invalid")
    parent = spec.get("parent_attempt")
    if parent is not None and not (isinstance(parent, str) and HEX32.fullmatch(parent)):
        raise RegistryError("record_invalid")


def checked_record(text):
    """The CLI-built record: shape, hex32 id, recomputed key, job name and --array iff array."""
    try:
        record = json.loads(text)
    except ValueError:
        raise RegistryError("record_invalid") from None
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION or record.get("kind") != "record":
        raise RegistryError("record_invalid")
    attempt = record.get("attempt")
    if not isinstance(attempt, str) or not HEX32.fullmatch(attempt) or any(key in record for key in STAMPED_KEYS):
        raise RegistryError("record_invalid")
    _checked_spec(record.get("spec"))
    options = record.get("sbatch_options")
    if not isinstance(options, list) or options[:2] != ["sbatch", "--parsable"] or not all(isinstance(option, str) for option in options):
        raise RegistryError("record_invalid")
    if record.get("key") != task_key(record["spec"]) or record.get("job_name") != "shk-" + attempt or "--job-name=shk-" + attempt not in options:
        raise RegistryError("record_invalid")
    if any(option.startswith("--array") for option in options) != (record["spec"]["resources"].get("array") is not None):
        raise RegistryError("record_invalid")
    return record


def _attempt_hint(text):
    try:
        attempt = json.loads(text).get("attempt")
    except (ValueError, AttributeError):
        return "-"
    return attempt if isinstance(attempt, str) and HEX32.fullmatch(attempt) else "-"


def checked_workload(spec):
    """The script bytes that will be piped to sbatch, after the run directory and digest checks."""
    run_directory = spec.get("remote_run_directory")
    try:
        if not isinstance(run_directory, str) or not run_directory.startswith("/"):
            raise RegistryError("run_directory_invalid")
        path = Path(run_directory)
        if path.resolve(strict=True) != path or not path.is_dir():
            raise RegistryError("run_directory_invalid")
    except OSError:
        raise RegistryError("run_directory_invalid") from None
    try:
        payload = Path(spec["remote_script"]).read_bytes()
    except OSError:
        raise RegistryError("script_unreadable") from None
    if hashlib.sha256(payload).hexdigest() != spec["script_digest"] or SBATCH_DIRECTIVE.search(payload):
        raise RegistryError("digest_mismatch")
    return payload


def _parent_released(root, record, parent):
    entry = load_attempt(root, parent)
    created = _created_of(entry["record"])
    if entry["record"] is None or created is None:
        raise RegistryError("parent_conflict:missing_record")
    report = query_rows(record["spec"]["principal"], created, [parent])
    if report["status"] != "complete":
        raise RegistryError("parent_unverifiable")
    resolution = resolve(entry["record"], entry["events"], report["rows"], time.time())
    if resolution["resolution"] == "error":
        raise RegistryError("parent_conflict:" + resolution["reason"])
    if not released(resolution):
        raise RegistryError("parent_not_released:" + resolution["resolution"])


def dedup(root, record):
    """True when this is the first attempt of its logical task, False for an admitted retry; refusals raise."""
    parent = record["spec"].get("parent_attempt")
    try:
        marker = _read_marker(root, record["key"])
    except (OSError, RegistryError):
        raise RegistryError("registry_unsafe") from None
    if marker is None:
        if parent is None:
            return True
        raise RegistryError("parent_unknown")
    if parent is None:
        raise RegistryError("duplicate_logical_task:" + marker)
    if parent != marker:
        raise RegistryError("parent_mismatch:" + marker)
    _parent_released(root, record, parent)
    return False


def _not_sent(directory, attempt, reason, detail, notes):
    body = {"at": time.time(), "reason": reason, "detail": detail[:OUTPUT_LIMIT]}
    try:
        write_once(directory / "not_sent.json", encoded(body))
    except OSError as exc:
        notes.append("not_sent.json unrecorded: " + str(exc))
    return "SHK_NOT_SENT:" + attempt + ":" + reason


def _launch(directory, record, payload, notes):
    """Run sbatch exactly once; whatever happens afterwards is recorded, never turned into a refusal."""
    attempt, spec = record["attempt"], record["spec"]
    env = {key: value for key, value in os.environ.items() if not key.startswith("SBATCH_")}
    try:
        result = subprocess.run(record["sbatch_options"], input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    except OSError as exc:
        return _not_sent(directory, attempt, "sbatch_unavailable", str(exc), notes)
    stdout, stderr = (stream.decode("utf-8", "replace") for stream in (result.stdout, result.stderr))
    if stderr:
        notes.append(stderr[:OUTPUT_LIMIT])
    match = SBATCH_REPLY.match(stdout) if result.returncode == 0 else None
    cluster = match.group(2) if match else None
    job_id = match.group(1) if match and cluster in (None, spec["cluster"]) else None
    body = {"job_id": job_id, "cluster": cluster, "submitted_at": time.time(),
            "sbatch": {"returncode": result.returncode, "stdout": stdout[:OUTPUT_LIMIT], "stderr": stderr[:OUTPUT_LIMIT]}}
    try:
        write_once(directory / "submitted.json", encoded(body))
    except OSError as exc:
        notes.append("submitted.json unrecorded: " + str(exc))
    if job_id is None:
        return "SHK_UNKNOWN:" + attempt + ":" + str(result.returncode)
    return "SHK_SUBMITTED:" + attempt + ":" + job_id


def _claim(root, directory, record, first, notes):
    marker = marker_path(root, record["key"])
    data = (record["attempt"] + "\n").encode()
    try:
        if first:
            write_once(marker, data)
        else:
            replace_marker(marker, data)
    except FileExistsError:
        return _not_sent(directory, record["attempt"], "claim_failed", "task marker appeared after the dedup check", notes)
    except OSError as exc:
        return _not_sent(directory, record["attempt"], "claim_failed", str(exc), notes)
    return None


def _submit_locked(root, record, program_sha256, payload, notes):
    attempt = record["attempt"]
    first = dedup(root, record)
    directory = attempt_directory(root, attempt)
    try:
        os.mkdir(directory, 0o700)
    except FileExistsError:
        raise RegistryError("attempt_exists") from None
    except OSError:
        raise RegistryError("record_write_failed") from None
    stamped = {**record, "created": time.time(), "created_on": socket.gethostname(), "principal_uid": os.getuid(), "program_sha256": program_sha256}
    try:
        _fsync_directory(root / "attempts")
        write_once(directory / "record.json", encoded(stamped))
    except OSError:
        raise RegistryError("record_write_failed") from None
    line = _claim(root, directory, record, first, notes)
    return line if line else _launch(directory, record, payload, notes)


def _emit(line, notes):
    text = "".join(note if note.endswith("\n") else note + "\n" for note in notes)
    if text:
        sys.stderr.write(text[:OUTPUT_LIMIT])
        sys.stderr.flush()
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def run_submit(args):
    """submit <registry_root> <record_json> <program_sha256>: one SHK_ line on stdout, exit 0."""
    if len(args) != 3:
        raise RegistryError("usage: submit <registry_root> <record_json> <program_sha256>")
    root_text, record_text, program_sha256 = args
    attempt, notes = _attempt_hint(record_text), []
    try:
        record = checked_record(record_text)
        if pwd.getpwuid(os.getuid()).pw_name != record["spec"]["principal"]:
            raise RegistryError("principal_mismatch")
        root = checked_root(root_text)
        payload = checked_workload(record["spec"])
        lock = acquire_lock(root)
        try:
            line = _submit_locked(root, record, program_sha256, payload, notes)
        finally:
            release_lock(lock)
    except RegistryError as exc:
        line = "SHK_REFUSED:" + attempt + ":" + str(exc)
    _emit(line, notes)
    return 0


# ---------------------------------------------------------------- reader

def _registry_exists(root):
    try:
        return stat.S_ISDIR(os.lstat(root).st_mode)
    except OSError:
        return False


def open_attempts(root):
    """Attempt directories with record.json and no closing file, sorted by created; acks do not close."""
    entries = []
    for item in os.scandir(root / "attempts"):
        if not item.is_dir(follow_symlinks=False) or not HEX32.fullmatch(item.name):
            continue
        try:
            names = set(os.listdir(item.path))
        except OSError:
            # Unlistable (or vanished since scandir): load_attempt records the listing error in `errors`.
            entries.append(load_attempt(root, item.name))
            continue
        if "record.json" in names and not names & CLOSING_FILES:
            entries.append(load_attempt(root, item.name))
    entries.sort(key=lambda entry: (_created_of(entry["record"]) is None, _created_of(entry["record"]) or 0, entry["attempt"]))
    return entries


def _selected_records(entries):
    """Records sharing the first record's principal/cluster; a disagreeing record gets an errors entry."""
    records = [entry for entry in entries if entry["record"] is not None]
    for entry in records:
        if _created_of(entry["record"]) is None or not isinstance(entry["record"].get("spec"), dict):
            entry["errors"].append("record.json:malformed_identity")
    records = [entry for entry in records if "record.json:malformed_identity" not in entry["errors"]]
    if not records:
        return []
    reference = tuple(records[0]["record"]["spec"].get(key) for key in ("principal", "cluster"))
    if not isinstance(reference[0], str) or not IDENT.fullmatch(reference[0]):
        records[0]["errors"].append("record.json:malformed_identity")
        return []
    selected = []
    for entry in records:
        if tuple(entry["record"]["spec"].get(key) for key in ("principal", "cluster")) != reference:
            entry["errors"].append("principal_cluster_disagrees")
        else:
            selected.append(entry["record"])
    return selected


def run_read(args):
    """read <registry_root> (--attempt <id>... | --open): one JSON document, read-only, no lock."""
    if len(args) < 2 or args[1] not in {"--attempt", "--open"} or (args[1] == "--attempt") != (len(args) > 2):
        raise RegistryError("usage: read <registry_root> (--attempt <id>... | --open)")
    root = Path(args[0])
    registry = {"exists": _registry_exists(root), "root": str(root), "open_count": 0, "truncated": False}
    document = {"schema_version": SCHEMA_VERSION, "now": time.time(), "host": socket.gethostname(), "registry": registry, "attempts": [], "sacct": None}
    if registry["exists"] and args[1] == "--open":
        entries = open_attempts(root) if _registry_exists(root / "attempts") else []
        registry.update(open_count=len(entries), truncated=len(entries) > MAX_OPEN)
        entries = entries[:MAX_OPEN]
    elif args[1] == "--attempt":
        entries = [load_attempt(root, attempt) for attempt in args[2:]]
    else:
        entries = []
    document["attempts"] = entries
    records = _selected_records(entries)
    if records:
        document["sacct"] = query_rows(records[0]["spec"]["principal"], min(_created_of(record) for record in records), [record["attempt"] for record in records])
    sys.stdout.write(json.dumps(document, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- event writer

def _anomalous_tasks(details):
    return {str(detail["task"]): {"restart": detail["top"], "state": detail["state"]} for detail in details if _anomalous(detail)}


def _rows_digest(attempt, rows):
    return digest([row for row in rows if row.get("JobName") == "shk-" + attempt])


def _ack_event(entry, result, details, rows, note, now):
    if entry["record"]["spec"]["partition_profile"]["preemptible"]:
        raise RegistryError("preemptible_profile")
    if result["resolution"] != "unexpected_preemption":
        raise RegistryError("not_applicable:" + result["resolution"])
    body = {"at": now, "job_id": result["job_id"], "waived": _anomalous_tasks(details), "note": note,
            "observation": {"rows_sha256": _rows_digest(entry["attempt"], rows), "tasks": result["tasks"], "cost": result["cost"]}}
    return "ack-" + str(int(now)) + ".json", body


def _abandon_event(entry, directory, result, report, note, now):
    attempt = entry["attempt"]
    if os.path.lexists(directory / "abandoned.json"):
        raise RegistryError("already_present")
    if result["resolution"] != "abandonable":
        raise RegistryError("not_applicable:" + result["resolution"])
    squeue_argv = ["env", "LC_ALL=C", "squeue", "-h", "--name=shk-" + attempt, "-o", "%i"]
    squeue = run_bounded(squeue_argv)
    if squeue["status"] != "complete":
        raise RegistryError("unverifiable")
    if squeue["stdout"].strip():
        raise RegistryError("squeue_shows_job")
    body = {"at": now, "age_seconds": now - entry["record"]["created"], "note": note,
            "checks": {"sacct_rows": 0, "squeue_rows": 0, "sacct_argv": report["argv"], "squeue_argv": squeue_argv}}
    return "abandoned.json", body


def _resolved_event(entry, directory, result, rows, now):
    if os.path.lexists(directory / "resolved.json"):
        raise RegistryError("already_present")
    if result["resolution"] != "terminal":
        raise RegistryError("not_applicable:" + result["resolution"])
    body = {"at": now, "resolution": "terminal", "job_id": result["job_id"], "tasks": result["tasks"], "cost": result["cost"],
            "anomalies": result["anomalies"], "rows_sha256": _rows_digest(entry["attempt"], rows)}
    return "resolved.json", body


def _write_event(root, kind, entry, report, note):
    """One SHK_EVENT_ line for one attempt; preconditions are evaluated on a fresh resolution."""
    attempt = entry["attempt"]
    directory = attempt_directory(root, attempt)
    try:
        if entry["record"] is None:
            raise RegistryError("unknown_attempt")
        if report is None or report["status"] != "complete":
            raise RegistryError("unverifiable")
        now = time.time()
        result, details = _analyse_or_error(entry["record"], entry["events"], report["rows"], now)
        if kind == "ack":
            name, body = _ack_event(entry, result, details, report["rows"], note, now)
        elif kind == "abandon":
            name, body = _abandon_event(entry, directory, result, report, note, now)
        else:
            name, body = _resolved_event(entry, directory, result, report["rows"], now)
    except RegistryError as exc:
        return "SHK_EVENT_REFUSED:" + attempt + ":" + str(exc)
    try:
        write_once(directory / name, encoded(body))
    except FileExistsError:
        # Single-instance events already exist; for `ack-<epoch>.json` this also covers a second ack
        # within the same second, which the fresh resolution above makes practically unreachable.
        return "SHK_EVENT_REFUSED:" + attempt + ":already_present"
    except OSError as exc:
        return "SHK_EVENT_UNKNOWN:" + attempt + ":" + str(exc).replace("\n", " ")
    return "SHK_EVENT_WRITTEN:" + attempt + ":" + name


def _analyse_or_error(record, events, rows, now):
    try:
        return _analyse(record, events, rows, now)
    except RegistryError as exc:
        raise RegistryError("not_applicable:error:" + str(exc)) from None
    except (KeyError, TypeError, AttributeError, ValueError):
        raise RegistryError("not_applicable:error:malformed_record") from None


def _event_payload(text):
    try:
        payload = json.loads(text)
    except ValueError:
        raise RegistryError("event payload must be a JSON object") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("note", ""), str):
        raise RegistryError("event payload must be a JSON object")
    return payload.get("note", "")


def _emit_lines(lines):
    sys.stdout.write("".join(line + "\n" for line in lines))
    sys.stdout.flush()
    return 0


def run_event(args):
    """event <registry_root> <kind> <payload_json> <attempt>...: one SHK_EVENT_ line per attempt, exit 0."""
    if len(args) < 4:
        raise RegistryError("usage: event <registry_root> ack|abandon|resolved <payload_json> <attempt>...")
    root_text, kind, payload_text, *attempts = args
    if kind not in {"ack", "abandon", "resolved"}:
        raise RegistryError("unknown event kind")
    note = _event_payload(payload_text)
    if kind != "resolved" and len(attempts) != 1:
        return _emit_lines(["SHK_EVENT_REFUSED:" + attempt + ":invalid_request" for attempt in attempts])
    try:
        root = existing_root(root_text)
        lock = acquire_lock(root)
    except RegistryError as exc:
        return _emit_lines(["SHK_EVENT_REFUSED:" + attempt + ":" + str(exc) for attempt in attempts])
    try:
        entries = [load_attempt(root, attempt) for attempt in attempts]
        records = _selected_records(entries)
        report = None
        if records:
            report = query_rows(records[0]["spec"]["principal"], min(_created_of(record) for record in records), [record["attempt"] for record in records])
        queried = {record["attempt"] for record in records}
        return _emit_lines([_write_event(root, kind, entry, report if entry["attempt"] in queried else None, note) for entry in entries])
    finally:
        release_lock(lock)


# ---------------------------------------------------------------- fetch-manifest

def _canonical_under(path, root):
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise RegistryError("symlink/noncanonical remote artifact path")
    path.relative_to(root)


def run_fetch_manifest(args):
    """fetch-manifest <registry_root> <attempt> <root> <source> <manifest_path>: {record, manifest} or a non-zero exit."""
    if len(args) != 5:
        raise RegistryError("usage: fetch-manifest <registry_root> <attempt> <root> <source> <manifest_path>")
    root_text, attempt, scope, source, manifest = args
    if not HEX32.fullmatch(attempt):
        raise RegistryError("invalid attempt id")
    record = load_json(attempt_directory(root_text, attempt) / "record.json", RECORD_LIMIT)
    if record.get("attempt") != attempt:
        raise RegistryError("attempt record disagrees with its directory")
    scope, source, manifest = Path(scope), Path(source), Path(manifest)
    for path in (scope, source, manifest):
        _canonical_under(path, scope)
    if not scope.is_dir() or not source.is_dir():
        raise RegistryError("remote artifact roots must be directories")
    value = json.loads(read_bounded(manifest, MANIFEST_LIMIT))
    if not isinstance(value, dict):
        raise RegistryError("manifest must be a JSON object")
    for item in value.get("files", []):
        name = item.get("path", "") if isinstance(item, dict) else ""
        relative = PurePosixPath(name)
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise RegistryError("manifest path escape")
        path = source / name
        if path.resolve(strict=True) != path or not path.is_file():
            raise RegistryError("symlink/noncanonical artifact file")
        path.relative_to(scope)
    sys.stdout.write(json.dumps({"record": record, "manifest": value}, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0


# ---------------------------------------------------------------- workstation helpers

def program_source():
    """This module's exact source bytes; only the workstation ever calls this."""
    path = globals().get("__file__")
    if not path:
        raise RegistryError("program source unavailable outside an installed module")
    source = Path(path)
    if source.suffix != ".py":
        source = source.with_suffix(".py")
    return source.read_bytes()


def program_sha256():
    return hashlib.sha256(program_source()).hexdigest()


def program_argv(subcommand, *args):
    """['python3', '-c', STUB, subcommand, *args] where STUB inflates and executes this module."""
    encoded_source = base64.b64encode(zlib.compress(program_source(), 9)).decode("ascii")
    argv = ["python3", "-c", STUB_TEMPLATE.format(encoded_source), subcommand, *args]
    if sum(len(part.encode()) for part in argv) > PROGRAM_LIMIT:
        raise RegistryError("remote program argv exceeds " + str(PROGRAM_LIMIT) + " bytes")
    return argv


def _single_line(stdout):
    lines = [line for line in stdout.splitlines() if line.strip()]
    return lines[0].strip() if len(lines) == 1 else None


def parse_submit_reply(attempt, stdout):
    """(outcome, value) with outcome in submitted|not_sent|refused|unknown; anything irregular is unknown."""
    line = _single_line(stdout)
    parts = line.split(":", 2) if line else []
    outcomes = {"SHK_SUBMITTED": "submitted", "SHK_NOT_SENT": "not_sent", "SHK_REFUSED": "refused", "SHK_UNKNOWN": "unknown"}
    if len(parts) != 3 or parts[1] != attempt or parts[0] not in outcomes or not parts[2]:
        return "unknown", "malformed_reply"
    if parts[0] == "SHK_SUBMITTED" and not NUMBER.fullmatch(parts[2]):
        return "unknown", "malformed_reply"
    return outcomes[parts[0]], parts[2]


def parse_read(stdout):
    try:
        document = json.loads(stdout)
    except ValueError:
        raise RegistryError("malformed reader output") from None
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION or not isinstance(document.get("attempts"), list):
        raise RegistryError("malformed reader output")
    return document


def parse_event_reply(stdout):
    """[(attempt, outcome, value)] with outcome in written|refused|unknown."""
    outcomes = {"SHK_EVENT_WRITTEN": "written", "SHK_EVENT_REFUSED": "refused", "SHK_EVENT_UNKNOWN": "unknown"}
    replies = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split(":", 2)
        if len(parts) != 3 or parts[0] not in outcomes or not HEX32.fullmatch(parts[1]):
            raise RegistryError("malformed event reply")
        replies.append((parts[1], outcomes[parts[0]], parts[2]))
    return replies


def parse_fetch_reply(stdout):
    try:
        document = json.loads(stdout)
    except ValueError:
        raise RegistryError("malformed fetch-manifest output") from None
    if not isinstance(document, dict) or not isinstance(document.get("record"), dict) or not isinstance(document.get("manifest"), dict):
        raise RegistryError("malformed fetch-manifest output")
    return document["record"], document["manifest"]


# ---------------------------------------------------------------- entry point

def main(argv):
    handlers = {"submit": run_submit, "read": run_read, "event": run_event, "fetch-manifest": run_fetch_manifest}
    try:
        if not argv or argv[0] not in handlers:
            raise RegistryError("unknown subcommand; use submit|read|event|fetch-manifest")
        return handlers[argv[0]](argv[1:])
    except (RegistryError, OSError, ValueError, KeyError, TypeError) as exc:
        sys.stderr.write("sherlock_registry: " + str(exc) + "\n")
        sys.stderr.flush()
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
