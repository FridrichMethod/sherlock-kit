"""shk submit / status / cancel / occupancy with Slurm as the only record (frozen 0.4.0).

One read (data/remote/query.sh) per command; submit adds one mutation (submit.sh, script on
stdin) and cancel is one mutation (cancel.sh: scancel -u USER --name=NAME). resolve_name()
is pure: one name's squeue + sacct rows grouped by base job id -> new | live | done |
failed | stale. live refuses even with --resubmit; done/failed need --resubmit REASON;
stale (spec files, no rows) submits like new. Reads print next_check_after (now + 60 s); a
lost mutation reply prints action "unknown" (exit 5); exit 6 when a newest job has anomalies.
"""
from __future__ import annotations

from collections import Counter
import json
import re

from sherlock_kit import partitions, spec
from sherlock_kit.errors import EXIT_LOCAL, ShkError

JOB_ID = re.compile(r"([1-9][0-9]*)(?:_(?:([0-9]+)|\[([0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*)(?:%[0-9]+)?\]))?")
QUEUE = ("JobID", "State", "Reason", "Partition", "JobName")  # query.sh: squeue -o "%i|%T|%r|%P|%j"
ACCT = ("JobID", "State", "Partition", "ElapsedRaw", "AllocCPUS", "AllocTRES", "Submit", "Restarts", "ExitCode", "JobName")
OCCUPANCY = (("UserName", 64), ("State", 24), ("tres-alloc", 128))  # squeue -O prefix, layout confirmed live
TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL",
                      "DEADLINE", "PREEMPTED", "REVOKED"})
RUNNING = frozenset({"RUNNING", "COMPLETING", "SUSPENDED", "CONFIGURING", "STAGE_OUT", "SIGNALING", "RESIZING", "STOPPED"})
MAX_ROWS = 50_000


def job_id(text: str) -> tuple:
    """(base, task, indices) of N, N_k or an aggregate N_[a-b,c%t]; a malformed id raises ValueError."""
    match, indices = JOB_ID.fullmatch(text), set()
    for part in (match[3] or "").split(",") if match and match[3] else ():
        first, _, last = part.partition("-")
        if not int(first) <= int(last or first) < spec.ARRAY_MAX_TASKS:
            match = None
            break
        indices.update(range(int(first), int(last or first) + 1))
    if match is None:
        raise ValueError(f"job id {text!r}")
    return int(match[1]), None if match[2] is None else int(match[2]), sorted(indices) if match[3] else None


def gpus(tres: str) -> int:
    """GPUs in a TRES string; Slurm prints gres/gpu=N beside gres/gpu:TYPE=N, so the two are never added."""
    generic, typed = None, 0
    for key, sep, value in (part.partition("=") for part in tres.split(",")):
        if key == "gres/gpu" or key.startswith("gres/gpu:"):
            if not sep or not value.isdecimal():
                raise ValueError(f"TRES {tres!r}")
            generic, typed = (int(value), typed) if key == "gres/gpu" else (generic, typed + int(value))
    if generic is not None and typed and generic != typed:
        raise ValueError(f"TRES {tres!r}: gres/gpu disagrees with the typed counts")
    return typed if generic is None else generic


def _spec_count(text: str):
    try:
        found = json.loads(text)["resources"]["array"]
        return 1 if found is None else int(found["count"])
    except (ValueError, KeyError, TypeError):
        return None


def parse_read(stdout: str, tools) -> dict:
    """query.sh output; complete only with SHK_END and rc 0 from every tool (else exit 1)."""
    lines, facts = {"SHK_Q": [], "SHK_A": [], "SHK_O": [], "SHK_S": [], "SHK_SPEC": []}, {}
    for line in stdout.splitlines():
        head, sep, rest = line.partition("|")
        if sep and head in lines:
            lines[head].append(rest)
        elif line.startswith("SHK_"):
            key, _, value = line.rpartition(" ")
            facts[key or value] = value
    rcs = {tool: facts.get(f"SHK_RC {tool}") for tool in tools}
    if "SHK_END" not in facts or any(rc != "0" for rc in rcs.values()):
        raise ShkError(EXIT_LOCAL, "malformed_read", f"query incomplete: end={'SHK_END' in facts}, rc={rcs}",
                       hint="retry once after 60 s; shk doctor --remote checks the remote tools")
    occupancy, specs = {}, {}
    for partition, _, row in (row.partition("|") for row in lines["SHK_O"]):
        occupancy.setdefault(partition, []).append(row)
    for name, _, rest in (row.partition("|") for row in lines["SHK_SPEC"]):
        job, _, text = rest.partition("|")
        specs[name] = (int(job) if job.isdecimal() else 0, _spec_count(text))
    files = {name: int(n) for name, _, n in (row.partition("|") for row in lines["SHK_S"]) if n.isdecimal()}
    return {"user": facts.get("SHK_USER"), "host": facts.get("SHK_HOST"), "q": lines["SHK_Q"], "a": lines["SHK_A"],
            "o": occupancy, "spec": specs, "spec_files": files}


def _row(item: dict) -> dict:
    base, task, indices = job_id(item["JobID"])
    state = (item["State"].split() or [""])[0].rstrip("+")
    elapsed, cpus, restart = (int(item.get(key, "0")) for key in ("ElapsedRaw", "AllocCPUS", "Restarts"))
    if not state or min(elapsed, cpus, restart) < 0:
        raise ValueError(f"row {item}")
    ran, acct = indices is None, "Restarts" in item  # an aggregate never ran
    return {"key": (acct, item["JobID"], restart), "base": base, "tasks": indices or [task or 0], "acct": acct,
            "explicit": ran, "array": "_" in item["JobID"], "state": state, "partition": item["Partition"],
            "restart": restart, "cpu": elapsed * cpus * ran, "gpu": elapsed * gpus(item.get("AllocTRES", "")) * ran,
            "exit": item.get("ExitCode"), "submit": item.get("Submit")}


def parse_rows(queue, acct, keep) -> tuple:
    """({name: rows}, Counter of malformed rows by name (None: unattributable), truncated) for keep(name)."""
    rows, malformed = {}, Counter()
    for fields, lines in ((QUEUE, queue), (ACCT, acct[:MAX_ROWS])):
        for line in lines:
            values = line.split("|", len(fields) - 1)  # JobName is last, so it may contain "|"
            name = values[-1] if len(values) == len(fields) else None
            if name is None:
                malformed[None] += 1
            elif keep(name):
                try:
                    rows.setdefault(name, []).append(_row(dict(zip(fields, values))))
                except ValueError:
                    malformed[name] += 1
    return rows, malformed, len(acct) > MAX_ROWS


def _kind(state: str) -> str:
    return ("completed" if state == "COMPLETED" else "failed" if state in TERMINAL else
            "running" if state in RUNNING else "pending")


def _job(rows, known) -> dict:
    """One base job: squeue beats sacct per task, then the highest restart, then an explicit row."""
    top, acct_top, seen = {}, {}, {}
    for row in sorted(rows, key=lambda r: (not r["acct"], r["restart"], r["explicit"])):
        for index in row["tasks"]:
            top[index] = row
            if row["acct"]:
                acct_top[index] = row
                seen.setdefault(index, set()).add(row["restart"])
    kinds, single, width = Counter(_kind(r["state"]) for r in top.values()), not any(r["array"] for r in rows), max(top) + 1
    count = 1 if single else max(known or 0, width)
    tasks = {"count": count, **{kind: kinds[kind] for kind in ("completed", "failed", "running", "pending")},
             "missing": count - len(top), "by_state": dict(sorted(Counter(r["state"] for r in top.values()).items())),
             "incomplete_history": [i for i, s in sorted(seen.items()) if s != set(range(max(s) + 1))][:20]}
    state = "live" if kinds["running"] or kinds["pending"] else "done" if kinds["completed"] == count else "failed"
    submitted = min((r["submit"] for r in rows if r["acct"] and "T" in r["submit"]), default=None)
    return {"state": state, "submitted": submitted and f"{submitted}Z", "job_id": str(rows[0]["base"]),
            "partition": rows[0]["partition"], "array": None if single else {"count": count, "count_known": (known or 0) >= width},
            "tasks": tasks, "exit_codes": dict(sorted(Counter(r["exit"] for r in acct_top.values() if r["state"] in TERMINAL).items())),
            "max_restart": max(r["restart"] for r in rows), "preempted": any(r["state"] in ("PREEMPTED", "REQUEUED") for r in rows),
            "cost": {"cpu_seconds": sum(r["cpu"] for r in rows), "gpu_seconds": sum(r["gpu"] for r in rows),
                     "final": state != "live" and tasks["missing"] == 0}}


def resolve_name(rows, spec_files: int = 0, spec_job=None, malformed: int = 0) -> dict:
    """Pure: one name's rows -> {state, current, history, spec_files}; the largest base id is current."""
    jobs = {}
    for row in {r["key"]: r for r in rows}.values():
        jobs.setdefault(row["base"], []).append(row)
    if not jobs:
        return {"state": "stale" if spec_files else "new", "current": None, "history": [], "spec_files": spec_files}
    summary = {base: _job(found, spec_job[1] if spec_job and spec_job[0] == base else None)
               for base, found in sorted(jobs.items())}
    newest, live = max(summary), [base for base, job in summary.items() if job["state"] == "live"]
    job, profile = summary[newest], partitions.profiles().get(summary[newest]["partition"])
    flags = {"duplicate_live": len(live) > 1, "malformed_rows": malformed > 0, "unknown_partition": profile is None,
             "unexpected_preemption": bool(profile and not profile.preemptible and (job["preempted"] or job["max_restart"]))}
    current = {key: value for key, value in job.items() if key not in ("state", "submitted", "preempted")}
    return {"state": "live" if live else job["state"],
            "current": {**current, "anomalies": sorted(flag for flag, on in flags.items() if on)},
            "history": [{"job_id": str(b), "state": j["state"], "submitted": j["submitted"]} for b, j in summary.items() if b != newest],
            "spec_files": spec_files}


def occupancy_summary(rows) -> tuple:
    """({user: counts}, totals) of running (with GPUs) and pending jobs from fixed-width squeue -O rows."""
    users, totals = {}, {"running_jobs": 0, "running_gpus": 0, "pending_jobs": 0}
    for line in rows:
        offsets = [sum(width for _, width in OCCUPANCY[:i]) for i in range(len(OCCUPANCY) + 1)]
        user, state, tres = (line[offsets[i]:offsets[i + 1]].strip() for i in range(len(OCCUPANCY)))
        if state not in ("RUNNING", "COMPLETING", "PENDING"):
            continue
        try:
            delta = {"running_jobs": int(state != "PENDING"), "running_gpus": gpus(tres) if state != "PENDING" else 0,
                     "pending_jobs": int(state == "PENDING")}
        except ValueError as exc:
            raise ShkError(EXIT_LOCAL, "malformed_read", f"occupancy row: {exc}") from None
        for target in (users.setdefault(user, dict.fromkeys(totals, 0)), totals):
            for key, value in delta.items():
                target[key] += value
    return users, totals
