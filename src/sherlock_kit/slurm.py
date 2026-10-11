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
from dataclasses import asdict
from datetime import timedelta
import json
import re
import secrets
import shlex

from sherlock_kit import executor as executors, partitions, spec
from sherlock_kit.errors import EXIT_ANOMALY, EXIT_LOCAL, EXIT_OK, EXIT_REFUSED, EXIT_UNKNOWN, Outcome, ShkError

JOB_ID = re.compile(r"([1-9][0-9]*)(?:_(?:([0-9]+)|\[([0-9]+(?:-[0-9]+)?(?:,[0-9]+(?:-[0-9]+)?)*)(?:%[0-9]+)?\]))?")
QUEUE = ("JobID", "State", "Reason", "Partition", "JobName")  # query.sh: squeue -o "%i|%T|%r|%P|%j"
ACCT = ("JobID", "State", "Partition", "ElapsedRaw", "AllocCPUS", "AllocTRES", "Submit", "Restarts", "ExitCode", "JobName")
OCCUPANCY = (("UserName", 64), ("State", 24), ("tres-alloc", 128))  # squeue -O prefix, layout confirmed live
TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "BOOT_FAIL",
                      "DEADLINE", "PREEMPTED", "REVOKED"})
RUNNING = frozenset({"RUNNING", "COMPLETING", "SUSPENDED", "CONFIGURING", "STAGE_OUT", "SIGNALING", "RESIZING", "STOPPED"})
MAX_ROWS, CADENCE = 50_000, timedelta(seconds=60)
REFUSALS = {"SHK_LIVE": ("live", "wait; check shk status at most once per 60 s"),
            "SHK_REJECTED": ("slurm_rejected", "read the message; shk status shows whether anything was queued"),
            "SHK_REFUSED": ("remote_refused", "nothing ran; fix the cause, then retry once")}


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
    """GPUs in a TRES string: gres/gpu=N is the total and gres/gpu:TYPE=N its typed share."""
    generic, typed = 0, 0
    for key, _, value in (part.partition("=") for part in tres.split(",")):
        if key == "gres/gpu" or key.startswith("gres/gpu:"):
            if not value.isdecimal():
                raise ValueError(f"TRES {tres!r}")
            if key == "gres/gpu":
                generic += int(value)
            else:
                typed += int(value)
    return max(generic, typed)


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


def register(subparsers) -> None:
    submit_p = subparsers.add_parser("submit", help="submit one logical task (refuses live; done/failed need --resubmit)")
    for flag, options in (("--task", {"required": True}), ("--campaign", {"default": "default"}),
                          ("--script", {"required": True}), ("--partition", {"required": True}),
                          ("--cpus", {"type": int, "required": True}), ("--mem", {"required": True, "help": "16G or 16000M"}),
                          ("--time", {"required": True, "help": "2h, 90m or H:MM:SS"}), ("--gpus", {"type": int, "default": 0}),
                          ("--tasks", {"type": int, "default": 1}), ("--array", {"metavar": "N[%M]"}), ("--constraint", {}),
                          ("--signal", {}), ("--after-ok", {"nargs": "+", "default": [], "metavar": "JOBID"}), ("--run-dir", {}),
                          ("--courtesy-ack", {"action": "store_true"}), ("--resubmit", {"metavar": "REASON"}),
                          ("--dry-run", {"action": "store_true", "help": "offline: print the spec and sbatch argv"})):
        submit_p.add_argument(flag, **options)
    requeue = submit_p.add_mutually_exclusive_group()
    requeue.add_argument("--requeue", action="store_true")
    requeue.add_argument("--no-requeue", dest="requeue", action="store_false")
    submit_p.set_defaults(handler=submit, requeue=None)
    status_p = subparsers.add_parser("status", help="state of logical tasks from one squeue and one sacct call")
    which = status_p.add_mutually_exclusive_group(required=True)
    which.add_argument("--task", action="append")
    which.add_argument("--name", action="append")
    which.add_argument("--all", action="store_true")
    status_p.add_argument("--campaign", default="default")
    status_p.add_argument("--run-dir", action="append", help="once per --task (default <remote_root>/runs/<name>)")
    status_p.add_argument("--since", type=int, metavar="DAYS", help="sacct window (default 30, 7 with --all)")
    status_p.set_defaults(handler=status)
    cancel_p = subparsers.add_parser("cancel", help="scancel the live jobs of one logical task (only when asked)")
    which = cancel_p.add_mutually_exclusive_group(required=True)
    which.add_argument("--task")
    which.add_argument("--name")
    cancel_p.add_argument("--campaign", default="default")
    cancel_p.set_defaults(handler=cancel)
    occupancy_p = subparsers.add_parser("occupancy", help="running and pending jobs per user on listed partitions")
    occupancy_p.add_argument("--partition", action="append", required=True)
    occupancy_p.set_defaults(handler=occupancy)


def _iso(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _read(ctx, args, tools) -> tuple:
    result = executors.check(executors.run_program(ctx.executor(), "query", args), "query")
    read, now = parse_read(result.stdout, tools), ctx.clock()
    return read, {"executor": ctx.executor().kind, "user": read["user"], "host": read["host"],
                  "queried_at": _iso(now), "next_check_after": _iso(now + CADENCE)}


def _mutate(ctx, program, args, done, stdin=None) -> tuple:
    """(marker, value, lines); marker "unknown" when the reply is lost or has no single known marker."""
    result = executors.run_program(ctx.executor(), program, args, stdin=stdin, mutation=True)
    if result.status == "unknown":
        return "unknown", "no reply", []
    lines = executors.check(result, program).stdout.splitlines()
    marks = [line.partition(" ") for line in lines if line.partition(" ")[0] in (*REFUSALS, *done, "SHK_UNPARSED")]
    mark, _, value = marks[0] if len(marks) == 1 else ("unknown", "", f"{len(marks)} terminal markers")
    if mark in REFUSALS:
        text = " ".join(line[8:] for line in lines if line.startswith("SHK_ERR|"))[-2000:]
        raise ShkError(EXIT_REFUSED, REFUSALS[mark][0], f"{program}: {mark} {value} {text}".strip(), REFUSALS[mark][1])
    return ("unknown", "unparsed sbatch reply", lines) if mark == "SHK_UNPARSED" else (mark, value, lines)


def _unknown(ctx, name, select, detail) -> Outcome:
    return Outcome({"ok": False, "action": "unknown", "name": name, "detail": detail,
                    "next": f"shk status {select} (not before {_iso(ctx.clock() + CADENCE)})"}, EXIT_UNKNOWN)


def _task(ctx, campaign, task) -> tuple:
    found = ctx.require("project")
    name = spec.job_name(found.project, spec.checked_text("campaign", campaign), spec.checked_text("task", task))
    return found, name, f"--task {shlex.quote(task)} --campaign {shlex.quote(campaign)}"


def _name(name) -> str:
    if not spec.NAME.fullmatch(name):
        raise ShkError(EXIT_LOCAL, "spec_invalid", f"--name {name!r} is not a shk job name")
    return name


def _courtesy(read, built) -> dict:
    users, totals = occupancy_summary(read["o"].get(built.profile.name, []))
    mine = users.get(read["user"], {})
    busy = mine.get("running_jobs", 0) + mine.get("pending_jobs", 0)
    others = any(user != read["user"] and counts["running_jobs"] for user, counts in users.items())
    long = built.document["resources"]["minutes"] > 120
    return {"occupancy": {"users": users, "totals": totals}, "courtesy": built.profile.courtesy,
            "warnings": [f"others are running on {built.profile.name} and you have {busy} jobs there; the courtesy "
                         "budget is about 2 jobs x 2 h"] if others and (busy >= 2 or long) else []}


def submit(args, ctx) -> Outcome:
    built = spec.build(args, ctx.require("project", "remote_root"), ctx.clock())
    report = {"name": built.name, "run_dir": built.run_dir, "argv": built.argv, "script_sha256": built.sha256}
    if args.dry_run:
        return Outcome({"action": "dry_run", **report, "spec": built.document})
    borrowed = built.profile.borrowed
    read, meta = _read(ctx, ["names", "30", built.profile.name if borrowed else "-", built.name, built.run_dir],
                       ("squeue", "sacct", "occupancy") if borrowed else ("squeue", "sacct"))
    rows = parse_rows(read["q"], read["a"], {built.name}.__contains__)[0]
    found = resolve_name(rows.get(built.name, []), read["spec_files"].get(built.name, 0), read["spec"].get(built.name))
    if found["state"] == "live" or (found["state"] in ("done", "failed") and not args.resubmit):
        hint = REFUSALS["SHK_LIVE"][1] if found["state"] == "live" else "find the cause, then add --resubmit REASON"
        raise ShkError(EXIT_REFUSED, found["state"], f"{built.name} is {found['state']} "
                       f"(newest job {found['current']['job_id']})", hint)
    report.update(_courtesy(read, built) if borrowed else {})
    stamp = f"{ctx.clock():%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"
    mark, value, _ = _mutate(ctx, "submit", [built.name, built.run_dir, stamp, built.sha256, built.text, "--",
                                             *built.argv], ("SHK_SUBMITTED",), stdin=built.script)
    if mark == "unknown":
        select = _task(ctx, args.campaign, args.task)[2] + (f" --run-dir {built.run_dir}" if args.run_dir else "")
        return _unknown(ctx, built.name, select, value)
    return Outcome({"action": "submitted", "job_id": value, "state_before": found["state"], **report, **meta})


def status(args, ctx) -> Outcome:
    since = (7 if args.all else 30) if args.since is None else args.since
    if not 1 <= since <= 3650:
        raise ShkError(EXIT_LOCAL, "spec_invalid", "--since must be 1-3650 days")
    selected, runs = [(_name(name), None, {}) for name in args.name or ()], args.run_dir or [None] * len(args.task or ())
    if args.task and len(runs) != len(args.task):
        raise ShkError(EXIT_LOCAL, "spec_invalid", "give --run-dir once per --task, or not at all")
    for task, run in zip(args.task or (), runs):
        found, name, _ = _task(ctx, args.campaign, task)
        run = spec.run_directory(run, found, name) if run or found.remote_root else None
        selected.append((name, run, {"campaign": args.campaign, "task": task}))
    project = getattr(ctx.config, "project", None) if args.all else None
    keep = (lambda n: n.startswith(f"shk.{project}." if project else "shk.")) if args.all else {s[0] for s in selected}.__contains__
    read, meta = _read(ctx, ["all" if args.all else "names", str(since), "-",
                             *(part for name, run, _ in selected for part in (name, run or "-"))], ("squeue", "sacct"))
    rows, malformed, truncated = parse_rows(read["q"], read["a"], keep)
    names = [{"name": name, **extra, **resolve_name(rows.get(name, []), read["spec_files"].get(name, 0),
                                                    read["spec"].get(name), malformed[name] + malformed[None])}
             for name, _, extra in (selected or [(name, None, {}) for name in sorted(rows)])]
    code = EXIT_ANOMALY if any(entry["current"] and entry["current"]["anomalies"] for entry in names) else EXIT_OK
    return Outcome({**meta, "names": names, "truncated": truncated}, code)


def cancel(args, ctx) -> Outcome:
    _, name, select = _task(ctx, args.campaign, args.task) if args.task else (None, _name(args.name), f"--name {args.name}")
    mark, value, lines = _mutate(ctx, "cancel", [name], ("SHK_CANCELLED", "SHK_NONE"))
    if mark == "unknown":
        return _unknown(ctx, name, select, value)
    return Outcome({"action": "cancelled" if mark == "SHK_CANCELLED" else "nothing_live", "name": name,
                    "jobs": [line[8:] for line in lines if line.startswith("SHK_JOB|")],
                    "executor": ctx.executor().kind, "next_check_after": _iso(ctx.clock() + CADENCE)})


def occupancy(args, ctx) -> Outcome:
    profiles = {name: partitions.profile(name) for name in args.partition}
    read, meta = _read(ctx, ["occupancy", "-", "-", *profiles], ("occupancy",))
    report = {}
    for name, profile in profiles.items():
        users, totals = occupancy_summary(read["o"].get(name, []))
        report[name] = {"profile": asdict(profile), "users": users, "totals": totals}
    return Outcome({**meta, "partitions": report})
