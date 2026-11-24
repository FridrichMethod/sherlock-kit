"""shk usage: a Marlowe project account's usage and estimated cost per month and rate period.

Read-only and free: one run of data/remote/usage.sh prints the caller's login, the caller's
association with the configured account (sacctmgr), the account's sreport rows for each
segment and the caller's runs over the whole range (sacct -X -D) as SHK_ records. Its first
argument is the rate table's time zone, which it exports as TZ whatever TZ the remote
environment holds, so sreport reads the segment dates as site-local midnights and sacct prints
Start and End in site-local time, the zone this module reads them in. Before any
remote call, a site without a packaged rate table (Site.rates None: Sherlock) is
ShkError(3, "no_rates") and a range that rates.segments leaves empty is
ShkError(2, "invalid_range"); then the configuration's account is required. An incomplete read
(no SHK_END, a tool that exited nonzero) or a row that does not parse is
ShkError(1, "malformed_read"), and an account without an association of the caller is
ShkError(1, "account_not_found"): sreport prints nothing for an unknown account and for an
account without usage alike.

The range [--start, --end) of site-local dates, now when --end is absent or after today, is
split by rates.segments at every first of a month, every period start and end and the table's
firm_until. Per segment, account is the sreport row with an empty Login, own the row of the
caller's login and others the account minus own: other members are never named, and no other
login or Proper Name reaches the reply. own_runs comes from sacct: a row whose Start is no time
(a run that never started) is skipped, End Unknown means now, and Start, End and the segment
bounds (site-local midnights, now for an open end) are converted to UTC before any
subtraction, since aware datetimes of one zone subtract as wall-clock times, wrong across a
daylight saving change. A run's ElapsedRaw is shared among the segments its [Start, End)
overlaps, in proportion to the overlap (all of it to the segment holding Start when End equals
Start), times its AllocTRES cpu and gres/gpu counts, and summed per first word of State.

Seconds stay unrounded Decimal values; the reply rounds them half up to integers, and hours
and money to 0.01. A segment's cost is rates.cost of its seconds, one division of an exact
numerator; totals sum unrounded seconds, and their costs sum the exact rates.numerator values
of the priced segments and divide once, so a cost on a half cent rounds up. A segment outside
every rate period is not priced (cost null) and makes total.cost_complete false; a segment
from firm_until on is provisional, priced and also summed in provisional_cost. ASSUMPTIONS
states, in every reply, what the estimate counts and leaves out.
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sherlock_kit import executor, rates, sites, slurm
from sherlock_kit.errors import EXIT_LOCAL, EXIT_REFUSED, EXIT_USAGE, Outcome, ShkError

if TYPE_CHECKING:
    from sherlock_kit.cli import Context
    from sherlock_kit.slurm import Subparsers

Bounds = list[tuple[datetime, datetime]]  # per segment: its UTC start and end

TOOLS = ("sacctmgr", "sreport", "sacct")  # usage.sh prints SHK_RC TOOL RC for each
BANNER = re.compile(r"Utilization (\S+) - (\S+) \([0-9]+ secs\)")
SREPORT_ROW = "Cluster|Account|Login|Proper Name|TRES Name|Used"
SACCT_ROW = "JobIDRaw|State|AllocTRES|ElapsedRaw|Start|End"
ZERO = Decimal(0)
MICROSECOND = timedelta(microseconds=1)
ASSUMPTIONS = (
    "An estimate from Slurm accounting and the packaged rates, not the bill; the site bills "
    "the research account (PTA) monthly.",
    "Every run counts for its whole elapsed time, preempted, failed and requeued runs "
    "included; own_runs.by_state shows their share.",
    "CPU time and GPU time are both priced, the CPUs of GPU jobs included.",
    "The 5,000 free introductory GPU-hours are not applied: they are a separate -free account "
    "(QOS medium, partition batch), not this account.",
    "sreport rolls usage up hourly, so the newest hour may be missing.",
)


@dataclass(frozen=True)
class Seconds:
    """GPU-seconds and CPU-seconds, unrounded until the reply rounds them."""

    gpu: Decimal = ZERO
    cpu: Decimal = ZERO

    def __add__(self, other: Seconds) -> Seconds:
        """Both sums."""
        return Seconds(self.gpu + other.gpu, self.cpu + other.cpu)

    def __sub__(self, other: Seconds) -> Seconds:
        """Both differences."""
        return Seconds(self.gpu - other.gpu, self.cpu - other.cpu)


@dataclass(frozen=True)
class Run:
    """One started run of the caller: its UTC wall-clock interval, ElapsedRaw and TRES counts."""

    state: str  # the first word of State
    start: datetime
    end: datetime
    elapsed: int
    cpus: int
    gpus: int


@dataclass
class Read:
    """The records of one complete usage.sh reply; reported and used hold one item per segment."""

    reported: list[dict[str, str] | None]
    used: list[dict[str, Seconds]]  # sreport Login ("" for the account row) -> seconds
    user: str = ""
    associated: bool = False
    runs: list[Run] = field(default_factory=list)


def register(subparsers: Subparsers) -> None:
    """Register the read-only usage estimate."""
    parser = subparsers.add_parser(
        "usage", help="estimated Marlowe usage cost per month and rate period (read-only)"
    )
    parser.add_argument(
        "--start",
        type=_day,
        required=True,
        metavar="YYYY-MM-DD",
        help="the first date of the range, in the site's time zone",
    )
    parser.add_argument(
        "--end",
        type=_day,
        metavar="YYYY-MM-DD",
        help="the date after the range, in the site's time zone (default: now; a date after "
        "today means now)",
    )
    parser.set_defaults(handler=usage)


def _day(text: str) -> date:
    """A --start or --end date; anything else is an argparse usage error (exit 2)."""
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a date YYYY-MM-DD") from None


def _end(segment: rates.Segment) -> str:
    return segment.end.isoformat() if segment.end else "now"


def _malformed(result: executor.Result, problem: str) -> ShkError:
    return ShkError(
        EXIT_LOCAL,
        "malformed_read",
        f"{problem}. {result.stderr.strip()[-2000:]}".strip(),
        hint=f"retry once after 60 s; {sites.command(result.site)} doctor --remote checks "
        "the remote tools",
    )


def _records(result: executor.Result) -> list[str]:
    """The records of a complete reply: SHK_END and an SHK_RC 0 of every tool, else malformed."""
    lines = executor.records(result.stdout)
    codes = {
        tool: next((line.split()[-1] for line in lines if line.startswith(f"SHK_RC {tool} ")), None)
        for tool in TOOLS
    }
    if "SHK_END" not in lines or any(code != "0" for code in codes.values()):
        found = ", ".join(f"{tool} rc={code}" for tool, code in codes.items())
        raise _malformed(result, f"usage incomplete: end={'SHK_END' in lines}, {found}")
    return lines


def _instant(text: str, zone: ZoneInfo) -> datetime | None:
    """A site-local Slurm time such as 2026-11-23T06:18:01 as a UTC instant; None for no time."""
    try:
        local = datetime.fromisoformat(text)
    except ValueError:  # Unknown, None
        return None
    return local.replace(tzinfo=zone).astimezone(UTC)


def _window(banner: str) -> dict[str, str] | None:
    """The window an sreport banner names, as printed (site-local); None when it names none."""
    found = BANNER.search(banner)
    return {"start": found[1], "end": found[2]} if found else None


def _row(text: str, segment: int) -> tuple[str, Seconds]:
    """The Login ("" for the account) and the seconds of one sreport row of segment (from 1).

    The ValueError of a malformed row leaves the row out of its message: it may name a member.
    """
    fields = text.split("|")
    if len(fields) < 6 or not fields[-1].isdecimal():
        raise ValueError(f"an sreport row of segment {segment} is not {SREPORT_ROW} in seconds")
    used, tres = Decimal(fields[-1]), fields[-2]  # a Proper Name may hold "|"
    return fields[2], Seconds(
        gpu=used if tres == "gres/gpu" else ZERO, cpu=used if tres == "cpu" else ZERO
    )


def _run(row: str, zone: ZoneInfo, now: datetime) -> Run | None:
    """The run of one sacct row; None for a run that never started; ValueError when malformed."""
    try:
        _job, state, tres, elapsed, start, end = row.split("|")
        began = _instant(start, zone)
        if began is None:
            return None
        counts = dict(part.split("=", 1) for part in tres.split(",") if "=" in part)
        return Run(
            state=state.partition(" ")[0],  # CANCELLED by 1234 -> CANCELLED
            start=began,
            end=_instant(end, zone) or now,  # Unknown: still running
            elapsed=int(elapsed),
            cpus=int(counts.get("cpu", "0")),
            gpus=slurm.gpus(tres),
        )
    except ValueError:
        raise ValueError(f"the sacct row {row!r} is not {SACCT_ROW}") from None


def _parse(lines: list[str], count: int, zone: ZoneInfo, now: datetime) -> Read:
    """The records of a complete reply of count segments; ValueError for a malformed one."""
    read = Read(reported=[None for _ in range(count)], used=[{} for _ in range(count)])
    for line in lines:
        if line.startswith("SHK_USER "):
            read.user = line.removeprefix("SHK_USER ")
        elif line.startswith("SHK_ASSOC "):
            read.associated = line.removeprefix("SHK_ASSOC ") != "0"
        elif line.startswith("SHK_A|"):
            if (found := _run(line.removeprefix("SHK_A|"), zone, now)) is not None:
                read.runs.append(found)
        elif line.startswith(("SHK_W|", "SHK_R|")):
            kind, number, text = line.split("|", 2)
            index = int(number) - 1
            if kind == "SHK_W":
                read.reported[index] = _window(text)
            else:
                login, seconds = _row(text, index + 1)
                read.used[index][login] = read.used[index].get(login, Seconds()) + seconds
    if not read.user:
        raise ValueError("no login (SHK_USER)")
    return read


def _bounds(segments: list[rates.Segment], zone: ZoneInfo, now: datetime) -> Bounds:
    """Each segment as UTC instants: its site-local midnights, and now for an open end."""

    def midnight(day: date) -> datetime:
        return datetime.combine(day, time(), zone).astimezone(UTC)

    return [(midnight(s.start), midnight(s.end) if s.end else now) for s in segments]


def _shares(run: Run, bounds: Bounds) -> Iterator[tuple[int, Decimal]]:
    """(segment index, its share of the run's ElapsedRaw) for every segment the run overlaps."""
    span = (run.end - run.start) // MICROSECOND
    for index, (first, last) in enumerate(bounds):
        if span <= 0:  # End equals Start: all of ElapsedRaw goes to the segment holding Start
            if first <= run.start < last:
                yield index, Decimal(run.elapsed)
            continue
        overlap = (min(run.end, last) - max(run.start, first)) // MICROSECOND
        if overlap > 0:
            yield index, Decimal(run.elapsed) * overlap / span


def _whole(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _hours(seconds: Seconds) -> dict[str, Any]:
    """Seconds as integers and hours as strings, both rounded half up from unrounded values."""
    return {
        "gpu_seconds": _whole(seconds.gpu),
        "cpu_seconds": _whole(seconds.cpu),
        "gpu_hours": rates.hours(seconds.gpu),
        "cpu_hours": rates.hours(seconds.cpu),
    }


def _own_runs(runs: list[Run], bounds: Bounds) -> list[dict[str, Any]]:
    """Per segment: how many runs overlap it, and their shares per first word of State."""
    tallies: list[dict[str, tuple[int, Seconds]]] = [{} for _ in bounds]
    for run in runs:
        for index, share in _shares(run, bounds):
            count, total = tallies[index].get(run.state, (0, Seconds()))
            used = Seconds(gpu=share * run.gpus, cpu=share * run.cpus)
            tallies[index][run.state] = (count + 1, total + used)
    return [
        {
            "runs": sum(count for count, _ in tally.values()),
            "by_state": {
                state: {"runs": count, **_hours(total)}
                for state, (count, total) in sorted(tally.items())
            },
        }
        for tally in tallies
    ]


def _amount(seconds: Seconds, period: rates.Period | None) -> dict[str, Any]:
    """AMOUNT: seconds, hours and the cost at the period's prices (null without a period)."""
    cost = None if period is None else rates.money(rates.cost(seconds.gpu, seconds.cpu, period))
    return {**_hours(seconds), "cost": cost}


def _money(numerators: Iterable[Decimal]) -> str:
    """The cost of a sum of exact rates.numerator values: one division, rounded half up."""
    return rates.money(sum(numerators, ZERO) / rates.HOUR)


def _total(used: list[Seconds], segments: list[rates.Segment]) -> dict[str, Any]:
    """TOTAL: AMOUNT over every segment, its cost over the priced ones, and their parts."""
    priced = [
        (rates.numerator(seconds.gpu, seconds.cpu, s.period), s.provisional)
        for seconds, s in zip(used, segments)
        if s.period is not None
    ]
    unpriced = sum((seconds for seconds, s in zip(used, segments) if s.period is None), Seconds())
    return {
        **_hours(sum(used, Seconds())),
        "cost": _money(value for value, _ in priced),
        "provisional_cost": _money(value for value, provisional in priced if provisional),
        "unpriced_gpu_hours": rates.hours(unpriced.gpu),
        "unpriced_cpu_hours": rates.hours(unpriced.cpu),
    }


def _rate(period: rates.Period | None) -> dict[str, str] | None:
    return (
        None
        if period is None
        else {"gpu_hour": str(period.gpu_hour), "cpu_hour": str(period.cpu_hour)}
    )


def _rates(table: rates.RateTable) -> dict[str, Any]:
    covered = table.covered
    return {
        "source": table.source,
        "read_on": table.read_on.isoformat(),
        "firm_until": table.firm_until.isoformat(),
        "tier": table.tier,
        "tier_text": table.tier_text,
        "currency": table.currency,
        "covered": {"start": covered[0].isoformat(), "end": covered[1].isoformat()}
        if covered
        else None,
    }


def _warnings(table: rates.RateTable, segments: list[rates.Segment]) -> list[str]:
    """One per segment without a rate, then once for provisional prices and the table's end."""
    found = [
        f"{s.start} to {_end(s)}: no packaged rate; usage shown, not priced"
        for s in segments
        if s.period is None
    ]
    if any(s.provisional for s in segments):
        found.append(
            f"usage from {table.firm_until} on is priced at the table read on {table.read_on}; "
            f"the site may have announced a new rate since: check {table.source} or upgrade shk"
        )
    covered = table.covered
    if covered and any(s.start >= covered[1] for s in segments):
        found.append(f"the rate table ends {covered[1]}: upgrade shk for later rates")
    return found


def _segments(table: rates.RateTable, args: argparse.Namespace, today: date) -> list[rates.Segment]:
    """The segments of --start and --end, checked offline; an empty range is invalid_range."""
    found = rates.segments(table, args.start, args.end, today)
    if not found:
        closed = args.end is not None and args.end <= today
        end = args.end if closed else f"now, on {today} in {table.timezone}"
        raise ShkError(
            EXIT_USAGE,
            "invalid_range",
            f"--start {args.start} does not lie before the end of the range ({end})",
            hint="give a --start before --end, or one no later than today without --end",
        )
    return found


def usage(args: argparse.Namespace, ctx: Context) -> Outcome:
    """Read the account's and the caller's usage once and price it per segment (read-only)."""
    site = ctx.site
    if site.rates is None:
        priced = " or ".join(f"{sites.command(s)} usage" for s in sites.SITES.values() if s.rates)
        raise ShkError(
            EXIT_REFUSED,
            "no_rates",
            f"{site.display} has no packaged rate table: usage estimates the cost of a site "
            "with a recharge",
            hint=f"run {priced} in a project of that site",
        )
    table = rates.load(site.rates)
    zone, now = ZoneInfo(table.timezone), ctx.clock()
    segments = _segments(table, args, rates.site_today(table, now))
    account = ctx.require("account").account
    boundaries = [segments[0].start.isoformat(), *(_end(s) for s in segments)]
    arguments = [table.timezone, account, *boundaries]
    program = executor.run_program(ctx.executor(), "usage", arguments)
    result = executor.check(program, "usage")
    lines = _records(result)
    try:
        read = _parse(lines, len(segments), zone, now)
    except ValueError as exc:
        raise _malformed(result, f"usage: {exc}") from None
    if not read.associated:
        raise ShkError(
            EXIT_LOCAL,
            "account_not_found",
            f"{account} is not one of the accounts of {read.user} on {site.display}: Slurm "
            "lists no association of the two",
            hint="set account in the configuration to a base project account you are a member of",
        )
    account_used = [used.get("", Seconds()) for used in read.used]
    own_used = [used.get(read.user, Seconds()) for used in read.used]
    parties = {
        "account": account_used,
        "own": own_used,
        "others": [whole - own for whole, own in zip(account_used, own_used)],
    }
    own_runs = _own_runs(read.runs, _bounds(segments, zone, now))
    return Outcome({
        "executor": ctx.executor().kind,
        **sites.reply_fields(result.site),
        "account": account,
        "user": read.user,
        "range": {"start": args.start.isoformat(), "end": _end(segments[-1])},
        "rates": _rates(table),
        "segments": [
            {
                "month": s.month,
                "start": s.start.isoformat(),
                "end": _end(s),
                "reported": read.reported[i],
                "rate": _rate(s.period),
                "provisional": s.provisional,
                **{name: _amount(used[i], s.period) for name, used in parties.items()},
                "own_runs": own_runs[i],
            }
            for i, s in enumerate(segments)
        ],
        "total": {
            **{name: _total(used, segments) for name, used in parties.items()},
            "cost_complete": all(s.period is not None for s in segments),
        },
        "warnings": _warnings(table, segments),
        "assumptions": list(ASSUMPTIONS),
        **slurm._cadence(ctx),
    })
