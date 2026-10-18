"""Lossless own-UID Slurm evidence and compatibility summaries for detailed status."""

from __future__ import annotations

import argparse
import re
from collections import Counter
from typing import TYPE_CHECKING, Any

from sherlock_kit import control, executor
from sherlock_kit.errors import EXIT_ANOMALY, EXIT_LOCAL, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context
    from sherlock_kit.slurm import Read, Rows

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
QUEUE = (
    "JobID",
    "JobIDRaw",
    "State",
    "Partition",
    "Elapsed",
    "AllocCPUS",
    "Submit",
    "Start",
    "End",
    "NodeList",
    "UID",
    "Comment",
    "JobName",
)
NORMALIZED = {
    "JobID": "job_id",
    "JobIDRaw": "job_id_raw",
    "Cluster": "cluster",
    "DBIndex": "db_index",
    "State": "state",
    "Partition": "partition",
    "ElapsedRaw": "elapsed_seconds",
    "AllocCPUS": "allocated_cpus",
    "AllocTRES": "allocated_tres",
    "Submit": "submitted_at",
    "Start": "started_at",
    "End": "ended_at",
    "Restarts": "restarts",
    "ExitCode": "exit_code",
    "NodeList": "nodes",
    "JobName": "name",
    "Comment": "comment",
    "UID": "uid",
}
TOOLS = ("squeue", "sacct", "scontrol")
NUMBERS = {"ElapsedRaw", "AllocCPUS", "Restarts", "UID"}
TIMES = {"Submit", "Start", "End"}
MISSING = {"", "Unknown", "N/A", "None", "(null)", "None assigned"}
FIELD = re.compile(r"(?:^| )([A-Za-z][A-Za-z0-9_/:]*)=")


def fields(line: str) -> dict[str, str]:
    """Preserve space-containing scontrol values using the next named field boundary."""
    matches = list(FIELD.finditer(line))
    return {
        match[1]: line[
            match.end() : matches[i + 1].start() if i + 1 < len(matches) else len(line)
        ].strip()
        for i, match in enumerate(matches)
    }


def _elapsed(value: str) -> int | None:
    if value in MISSING or value == "INVALID":
        return None
    days, sep, clock = value.partition("-")
    parts = (clock if sep else days).split(":")
    if not all(part.isdecimal() for part in parts) or len(parts) > 3:
        return None
    result = 0
    for part in parts:
        result = 60 * result + int(part)
    return result + (int(days) * 86400 if sep and days.isdecimal() else 0)


def normalized(values: dict[str, str], raw: str) -> dict[str, Any]:
    """The public record contract, with absent evidence represented by null."""
    record: dict[str, Any] = dict.fromkeys(NORMALIZED.values())
    for key, label in NORMALIZED.items():
        value = values.get(key, "")
        if value in MISSING:
            record[label] = None
        elif key in NUMBERS:
            record[label] = int(value)
        elif key in TIMES:
            record[label] = value + "Z" if "T" in value and not value.endswith("Z") else value
        else:
            record[label] = value
    if "Elapsed" in values:
        record["elapsed_seconds"] = _elapsed(values["Elapsed"])
    # squeue prints requested CPUs, and a placeholder start, for a job without nodes (pending,
    # or cancelled before any allocation); pending end times are estimates.
    if "Elapsed" in values:
        if values.get("State") == "PENDING" or values.get("NodeList", "") in MISSING:
            record.update(allocated_cpus=None, started_at=None)
        if values.get("State") not in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "TIMEOUT",
            "PREEMPTED",
            "NODE_FAIL",
            "OUT_OF_MEMORY",
            "BOOT_FAIL",
            "DEADLINE",
            "REVOKED",
        }:
            record["ended_at"] = None
    record["raw"] = raw
    return record


def _selected(value: str, ids: list[str]) -> bool:
    return not ids or any(
        value == job or ("_" not in job and value.startswith(job + "_")) for job in ids
    )


def _allocation_complete(data: dict[str, str]) -> bool:
    """Missing TRES is valid only for a job that never had an allocation, in any state.

    Pending, held and cancelled-before-start rows print AllocCPUS 0, ElapsedRaw 0, no Start
    and no AllocTRES; a started job without AllocTRES is incomplete evidence.
    """
    return data["AllocTRES"] not in MISSING or (
        data["Start"] in MISSING and data["ElapsedRaw"] == "0" and data["AllocCPUS"] == "0"
    )


def parse(read: Read, uid: int, keep: Any, ids: list[str]) -> tuple[dict[str, Any], Rows]:
    """Keep every full record; derive summaries from the same accounting identities."""
    from sherlock_kit import slurm

    report: dict[str, Any] = {
        "queue": [],
        "accounting": [],
        "jobs": [],
        "malformed": [],
        "complete": True,
        "allocation_evidence_complete": True,
    }
    by_name: dict[str, list[slurm.Row]] = {}
    malformed: Counter[str | None] = Counter()
    for label, columns, lines in (
        ("queue", QUEUE, read.queue),
        ("accounting", ACCOUNTING, read.acct),
    ):
        for line in lines:
            values = line.split("|")
            try:
                if len(values) != len(columns):
                    raise ValueError("field count")
                data = dict(zip(columns, values, strict=True))
                if int(data["UID"]) != uid:
                    continue
                if not (
                    _selected(data["JobID"], ids) or (ids and data["JobIDRaw"] in ids)
                ) or not keep(data["JobName"]):
                    continue
                record = normalized(data, line)
                report[label].append(record)
                if label == "accounting" and any(
                    data[key] in MISSING
                    for key in (
                        "JobIDRaw",
                        "Cluster",
                        "DBIndex",
                        "ElapsedRaw",
                        "AllocCPUS",
                        "Restarts",
                    )
                ):
                    report["complete"] = False
                allocation_complete = label != "accounting" or _allocation_complete(data)
                report["allocation_evidence_complete"] &= allocation_complete
                report["complete"] &= allocation_complete
                legacy = dict(data)
                if label == "queue":
                    legacy = {key: data[key] for key in slurm.QUEUE_FIELDS}
                else:
                    for key in ("ElapsedRaw", "AllocCPUS", "Restarts"):
                        if data[key] in MISSING:
                            legacy[key] = "0"
                by_name.setdefault(data["JobName"], []).append(slurm._row(legacy))
            except ValueError:
                report["malformed"].append({"source": label, "raw": line})
                report["complete"] = False
                malformed[None] += 1
    seen = [
        value
        for label in ("queue", "accounting")
        for row in report[label]
        for value in (row["job_id"], row["job_id_raw"])
        if value is not None
    ]
    report["missing_job_ids"] = [
        job for job in ids if not any(_selected(value, [job]) for value in seen)
    ]
    report["complete"] = report["complete"] and not report["missing_job_ids"]
    return report, slurm.Rows(by_name, malformed, truncated=False)


def _jobs(stdout: str, uid: int, report: dict[str, Any]) -> None:
    """Keep the own control records of the queued physical IDs; other records are not evidence.

    show job ARRAY_ID also prints sibling elements that the selection or the queue read
    did not include; they are ignored, while every queued physical ID needs its record.
    """
    expected = {
        record["job_id_raw"] for record in report["queue"] if record["job_id_raw"] is not None
    }
    observed: set[str | None] = set()
    for line in stdout.splitlines():
        if not line.startswith("SHK_J|"):
            continue
        raw = line.removeprefix("SHK_J|")
        data = fields(raw)
        own = re.fullmatch(r"[^()]*\(([0-9]+)\)", data.get("UserId", ""))
        job = data.get("JobId")
        if not own or int(own[1]) != uid or job not in expected or job in observed:
            continue
        observed.add(job)
        report["jobs"].append({
            "job_id": job,
            "name": data.get("JobName"),
            "comment": data.get("Comment"),
            "uid": uid,
            "fields": data,
            "raw": raw,
        })
    report["missing_control_job_ids"] = sorted(expected - observed)
    report["control_identities_complete"] = not report["missing_control_job_ids"]
    if not report["control_identities_complete"]:
        report["complete"] = False


def _summary_evidence(
    names: list[dict[str, Any]], rows: Rows, ids: list[str], report: dict[str, Any]
) -> None:
    """Mark history gaps and exact element selections without inventing sibling states."""
    partial = {
        str(row.base)
        for found in rows.by_name.values()
        for row in found
        if ids and row.array and str(row.base) not in ids
    }
    report["selection"] = {
        "mode": "job_ids" if ids else "names",
        "requested_job_ids": ids,
        "partial_array_ids": sorted(partial, key=int),
    }
    report["history_complete"] = not any(
        entry["current"] and entry["current"]["tasks"]["incomplete_history"] for entry in names
    )
    if partial or not report["history_complete"]:
        report["complete"] = False
    for entry in names:
        current = entry["current"]
        if current is None:
            continue
        if current["job_id"] in partial:
            entry.update(state="unknown", scope="partial_array")
            current["array"].update(count=None, count_known=False)
            current["tasks"].update(count=None, missing=None)
        if not report["complete"]:
            current["cost"]["final"] = False
            current["evidence_complete"] = False


def _returncodes(lines: list[str]) -> dict[str, int | None]:
    """The SHK_RC exit status of each tool, or None when the program never reported it."""
    found = {}
    for line in lines:
        words = line.split()
        if len(words) == 3 and words[0] == "SHK_RC" and words[2].isdecimal():
            found[words[1]] = int(words[2])
    return {tool: found.get(tool) for tool in TOOLS}


def status(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Query own selected IDs or names and retain evidence before any summary reduction."""
    from sherlock_kit import slurm

    ids = [control.checked_id(value) for value in args.job_id or []]
    since = slurm._since(args)
    selected = slurm._selection(args, ctx)
    mode = "ids" if ids else ("all" if args.all else "names")
    query = [mode, str(since), *ids]
    if args.all:
        query.append(slurm._all_prefix(ctx))
    for item in selected:
        query.extend((item.name, item.run_dir or "-"))
    result = executor.check(executor.run_program(ctx.executor(), "details", query), "details")
    lines = result.stdout.splitlines()
    ended, codes = "SHK_END" in lines, _returncodes(lines)
    # Partial tool evidence stays reviewable: parse it all, then refuse to summarize below.
    safe = result.stdout + "\nSHK_RC squeue 0\nSHK_RC sacct 0\nSHK_END\n"
    read = slurm.parse_read(safe, ("squeue", "sacct"))
    uid_line = [line.removeprefix("SHK_UID ") for line in lines if line.startswith("SHK_UID ")]
    if len(uid_line) != 1 or not uid_line[0].isdecimal():
        raise ShkError(EXIT_LOCAL, "malformed_read", "details query has no verified current UID")
    uid = int(uid_line[0])
    wanted = {item.name for item in selected}
    keep = (
        (lambda _name: True)
        if ids
        else (slurm._all_names(ctx) if args.all else lambda name: name in wanted)
    )
    report, rows = parse(read, uid, keep, ids)
    _jobs(result.stdout, uid, report)
    report["returncodes"] = codes
    report["complete"] = bool(report["complete"] and ended and set(codes.values()) == {0})
    meta = {
        "executor": ctx.executor().kind,
        "user": read.user,
        "uid": uid,
        "host": read.host,
        **slurm._cadence(ctx),
    }
    report.update(queried_at=meta["queried_at"], history_window_days=since)
    listed = selected or [slurm.Selected(name) for name in sorted(rows.by_name)]
    names = [
        {"name": item.name, **item.labels, **slurm._resolve(read, rows, item.name)}
        for item in listed
    ]
    _summary_evidence(names, rows, ids, report)
    if not ended or codes["squeue"] != 0 or codes["sacct"] != 0:
        # Like plain status: states derived from a failed queue or accounting read could
        # call a live or finished job new. The evidence is kept; the summaries are not.
        return Outcome(
            {
                "ok": False,
                "error": "malformed_read",
                "message": f"details query incomplete: end={ended}, rc={codes}",
                "hint": "retry once after 60 s; shk doctor --remote checks the remote tools",
                **meta,
                "details": report,
            },
            EXIT_LOCAL,
        )
    anomaly = any(entry["current"] and entry["current"]["anomalies"] for entry in names)
    payload: dict[str, Any] = {**meta, "names": names, "truncated": False}
    if args.details or ids:
        payload["details"] = report
    return Outcome(payload, EXIT_ANOMALY if anomaly else 0)
