"""`shk wait`: repeat the status read of a selection until it needs the agent, or --timeout.

Each read is the read `shk status` makes for the same --task/--name/--all selection
(slurm.status_reader(): one query.sh run, one squeue and one sacct). After it wait decides, in
this order:

- anomaly (exit 6): the read is exit 6; names holds the names whose current job has anomalies;
- failed: a name is failed now that an earlier read of this wait saw live (an older failure, such
  as one --all finds, never counts);
- settled: no listed name is live (a selection without a live name settles at the first read);
- max_pending (with --max-pending): a live name whose current job had no running task at every
  read since the wait first read it so, for at least that long on Context.clock; a read with a
  running task resets the name;
- timeout: less than 60 s are left before --timeout ends.

Otherwise it sleeps through Context.sleep for the delay, or for what is left before the deadline
when that is shorter, and reads again, so consecutive reads are at least 60 s apart (site rule
10) and the last one can fall on the deadline. The first delay is --interval; after a read in
which no listed name has a running task and every name kept its state, current job id and task
counts, the delay doubles, up to max(30 min, --interval), and any other read resets it to
--interval. A ShkError of a read ends the wait with that error and exit code, without a retry.
The reply, printed once at the end, is the last read's status reply unchanged plus "wait":
{outcome, names, reads, started_at, timeout_at}. wait dispatches no mutation and writes nothing,
here or remotely; what it remembers between reads lives only in this process.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sherlock_kit import slurm, spec
from sherlock_kit.errors import EXIT_ANOMALY, EXIT_OK, Outcome

if TYPE_CHECKING:
    from sherlock_kit.cli import Context
    from sherlock_kit.slurm import Subparsers

FORMS = "2h, 90m or H:MM:SS"  # the forms of submit --time
LEAST = timedelta(minutes=1)
MAX_TIMEOUT = timedelta(hours=168)
BACKOFF_CAP = timedelta(minutes=30)


def register(subparsers: Subparsers) -> None:
    """Add the wait command, with the selection options of status."""
    parser = subparsers.add_parser(
        "wait", help="repeat the status read until a name needs attention, at most --timeout"
    )
    slurm.selection_arguments(parser)
    parser.add_argument(
        "--timeout", required=True, metavar="DUR", help=f"how long to wait, 1m to 168h: {FORMS}"
    )
    parser.add_argument(
        "--interval",
        default="5m",
        metavar="DUR",
        help="delay between reads, at least 1m (default 5m); while nothing runs or changes it "
        "doubles, up to 30m or --interval when that is longer",
    )
    parser.add_argument(
        "--max-pending",
        metavar="DUR",
        help="at least 1m: end when a live name has had no running task for this long",
    )
    parser.set_defaults(handler=wait)


def duration(
    option: str, text: str, bounds: str, least: timedelta, most: timedelta = timedelta.max
) -> timedelta:
    """A DUR in the forms of submit --time, from least to most; else exit 1 spec_invalid."""
    found = timedelta(seconds=spec._seconds(text))  # 0 for any other form
    if not least <= found <= most:
        raise spec._invalid(f"{option} {text!r}: use {FORMS}, {bounds}")
    return found


@dataclass
class _Watch:
    """What one wait remembers between its reads."""

    interval: timedelta
    max_pending: timedelta | None
    seen_live: set[str] = field(default_factory=set)
    idle_since: dict[str, datetime] = field(default_factory=dict)  # live without a running task
    observed: list[tuple[Any, ...]] | None = None  # what the backoff compares, per name
    delay: timedelta = timedelta(0)

    def decide(
        self, read: Outcome, now: datetime, deadline: datetime
    ) -> tuple[str, list[str]] | None:
        """The outcome of this read and its names, or None to read again."""
        entries = read.payload["names"]
        if read.code == EXIT_ANOMALY:
            return "anomaly", [
                entry["name"]
                for entry in entries
                if entry["current"] and entry["current"]["anomalies"]
            ]
        failed = [
            entry["name"]
            for entry in entries
            if entry["state"] == "failed" and entry["name"] in self.seen_live
        ]
        live = [entry for entry in entries if entry["state"] == "live"]
        self.seen_live.update(entry["name"] for entry in live)
        if failed:
            return "failed", failed
        if not live:
            return "settled", []
        self.idle_since = {
            entry["name"]: self.idle_since.get(entry["name"], now)
            for entry in live
            if not entry["current"]["tasks"]["running"]
        }
        limit = self.max_pending or timedelta.max
        stuck = [name for name, since in self.idle_since.items() if now - since >= limit]
        if stuck:
            return "max_pending", stuck
        if deadline - now < slurm.CADENCE:
            return "timeout", []
        return None

    def next_delay(self, entries: list[dict[str, Any]]) -> timedelta:
        """--interval after a change or a running task, else the last delay doubled and capped."""
        currents = [entry["current"] or {} for entry in entries]
        observed = [
            (entry["name"], entry["state"], current.get("job_id"), current.get("tasks"))
            for entry, current in zip(entries, currents)
        ]
        running = any(current and current["tasks"]["running"] for current in currents)
        if running or observed != self.observed:
            self.delay = self.interval
        else:
            self.delay = min(2 * self.delay, max(BACKOFF_CAP, self.interval))
        self.observed = observed
        return self.delay


def wait(args: argparse.Namespace, ctx: Context) -> Outcome:
    """`shk wait`: read, decide and sleep until an outcome; the module docstring gives the order."""
    timeout = duration("--timeout", args.timeout, "1m to 168h", LEAST, MAX_TIMEOUT)
    interval = duration("--interval", args.interval, "at least 1m", LEAST)
    pending = args.max_pending
    max_pending = (
        None if pending is None else duration("--max-pending", pending, "at least 1m", LEAST)
    )
    watch = _Watch(interval, max_pending)
    read = slurm.status_reader(args, ctx)
    started = ctx.clock()
    deadline = started + timeout
    reads = 0
    while True:
        found = read()
        reads += 1
        now = ctx.clock()
        decided = watch.decide(found, now, deadline)
        if decided is not None:
            outcome, names = decided
            summary = {
                "outcome": outcome,
                "names": names,
                "reads": reads,
                "started_at": slurm._iso(started),
                "timeout_at": slurm._iso(deadline),
            }
            code = EXIT_ANOMALY if outcome == "anomaly" else EXIT_OK
            return Outcome({**found.payload, "wait": summary}, code)
        ctx.sleep(min(watch.next_delay(found.payload["names"]), deadline - now).total_seconds())
