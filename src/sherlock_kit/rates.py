"""Packaged recharge rates per site, schema_version 1, and the split of a usage range into segments.

A site with a recharge names its table (data/rates-marlowe.json for Marlowe); a site record never
holds a price. The Marlowe table is a copy, kept by releases, of the "Recharge rates" on
https://marlowe-research.stanford.edu/access/ for Basic Access (base project account, QOS normal,
partition preempt): read_on is the date the page was read, source its address, and a new rate is
a data change plus a release, never a live fetch. Periods are site-local dates with an exclusive
end, sorted and not overlapping; a gap between them, or a date outside them, has no published
rate. Prices per GPU-hour and per CPU-hour are decimal strings, read as Decimal. parse(data)
checks a table once and raises ValueError for a missing key, a price that is not a decimal
string, a period that does not end after its start, and periods out of order or overlapping;
load(name) reads a packaged file through it.

The site announces an increase at least one month ahead, so a table is firm only until
firm_until, read_on plus one calendar month (add_month: the same day, clamped to the month's last
day). segments(table, start, end, today) splits [start, end) at every first of a month, every
period start and end, and firm_until, so each segment lies in one month, in one period or gap,
and on one side of firm_until. An end that is None or after today means now: the range then runs
to the end of today and its last segment has end None. A segment from firm_until on is
provisional (priced, but the site may have announced a new rate since read_on); one outside every
period has period None and is not priced. site_today(table, now) is the date at the aware instant
now in the table's time zone, never the machine's. numerator(gpu_seconds, cpu_seconds, period)
is the exact Decimal gpu_seconds x gpu_hour + cpu_seconds x cpu_hour, and cost divides it by
3600 once, so a cost that lies exactly on a half cent stays there; a sum of costs adds the
numerators and divides once. money and hours round half up to 0.01 and return strings.
"""

from __future__ import annotations

import calendar
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import cache
from importlib import resources
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

PRICE = re.compile(r"[0-9]+(\.[0-9]+)?")
HOUR = 3600  # seconds
HUNDREDTH = Decimal("0.01")


@dataclass(frozen=True)
class Period:
    """One published rate: site-local dates [start, end) and the prices per GPU- and CPU-hour."""

    start: date
    end: date
    gpu_hour: Decimal
    cpu_hour: Decimal


@dataclass(frozen=True)
class RateTable:
    """A site's packaged rates: the tier, where and when they were read, and their periods."""

    currency: str
    timezone: str
    tier: str
    tier_text: str
    source: str
    read_on: date
    periods: tuple[Period, ...]

    @property
    def firm_until(self) -> date:
        """The first date the table may no longer hold: read_on plus one calendar month."""
        return add_month(self.read_on)

    @property
    def covered(self) -> tuple[date, date] | None:
        """The first period's start and the last period's end; None for a table without periods."""
        if not self.periods:
            return None
        return self.periods[0].start, self.periods[-1].end


@dataclass(frozen=True)
class Segment:
    """Part of a usage range in one month, in one period or gap, and on one side of firm_until.

    end None means now; month is the YYYY-MM of start; period None means no published rate.
    """

    start: date
    end: date | None
    month: str
    period: Period | None
    provisional: bool


def _value(data: dict[str, Any], key: str) -> Any:
    if key not in data:
        raise ValueError(f"rate table: missing key {key!r}")
    return data[key]


def _text(data: dict[str, Any], key: str) -> str:
    value = _value(data, key)
    if not isinstance(value, str):
        raise ValueError(f"rate table: {key} must be a string, got {value!r}")
    return value


def _date(data: dict[str, Any], key: str) -> date:
    return date.fromisoformat(_text(data, key))


def _price(data: dict[str, Any], key: str) -> Decimal:
    value = _value(data, key)
    if not isinstance(value, str) or PRICE.fullmatch(value) is None:
        raise ValueError(
            f'rate table: {key} must be a decimal string such as "0.25", got {value!r}'
        )
    return Decimal(value)


def _period(data: dict[str, Any]) -> Period:
    period = Period(
        start=_date(data, "start"),
        end=_date(data, "end"),
        gpu_hour=_price(data, "gpu_hour"),
        cpu_hour=_price(data, "cpu_hour"),
    )
    if period.end <= period.start:
        raise ValueError(f"rate table: the period from {period.start} ends {period.end}")
    return period


def parse(data: dict[str, Any]) -> RateTable:
    """The rate table in a decoded JSON object, checked once.

    Raises ValueError for a missing key, a price that is not a decimal string (a JSON number
    included), a period that does not end after its start, or periods out of order or overlapping.
    """
    periods = tuple(_period(entry) for entry in _value(data, "periods"))
    for earlier, later in pairwise(periods):
        if later.start < earlier.end:
            raise ValueError(
                f"rate table: the period from {later.start} starts before {earlier.end}, "
                "the end of the period listed before it"
            )
    return RateTable(
        currency=_text(data, "currency"),
        timezone=_text(data, "timezone"),
        tier=_text(data, "tier"),
        tier_text=_text(data, "tier_text"),
        source=_text(data, "source"),
        read_on=_date(data, "read_on"),
        periods=periods,
    )


@cache
def load(name: str) -> RateTable:
    """The packaged table data/<name>, checked by parse."""
    path = resources.files("sherlock_kit") / "data" / name
    return parse(json.loads(path.read_text(encoding="utf-8")))


def add_month(day: date) -> date:
    """The same day one calendar month later, clamped to that month's last day."""
    year, month = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def site_today(table: RateTable, now: datetime) -> date:
    """The site-local date at the aware instant now, in the table's time zone."""
    return now.astimezone(ZoneInfo(table.timezone)).date()


def _month_starts(start: date, stop: date) -> Iterator[date]:
    day = add_month(start.replace(day=1))
    while day < stop:
        yield day
        day = add_month(day)


def _period_at(table: RateTable, day: date) -> Period | None:
    return next((p for p in table.periods if p.start <= day < p.end), None)


def segments(table: RateTable, start: date, end: date | None, today: date) -> list[Segment]:
    """The segments of the site-local range [start, end), in order; end None or after today is now.

    The split points are every first of a month, every period start and end, and firm_until,
    where they lie strictly inside the range. An open range ends after today, and its last
    segment has end None. A start that is not before the end gives no segment.
    """
    stop = end if end is not None and end <= today else today + timedelta(days=1)
    if start >= stop:
        return []
    points = {
        *_month_starts(start, stop),
        *(p.start for p in table.periods),
        *(p.end for p in table.periods),
        table.firm_until,
    }
    cuts = sorted(day for day in points if start < day < stop)
    starts = [start, *cuts]
    ends: list[date | None] = [*cuts, stop if stop <= today else None]
    return [
        Segment(
            start=first,
            end=last,
            month=first.isoformat()[:7],
            period=_period_at(table, first),
            provisional=first >= table.firm_until,
        )
        for first, last in zip(starts, ends)
    ]


def numerator(gpu_seconds: Decimal | int, cpu_seconds: Decimal | int, period: Period) -> Decimal:
    """gpu_seconds x gpu_hour + cpu_seconds x cpu_hour: the cost times HOUR, exact."""
    return Decimal(gpu_seconds) * period.gpu_hour + Decimal(cpu_seconds) * period.cpu_hour


def cost(gpu_seconds: Decimal | int, cpu_seconds: Decimal | int, period: Period) -> Decimal:
    """The price of GPU-seconds and CPU-seconds at the period's rates: one division, unrounded."""
    return numerator(gpu_seconds, cpu_seconds, period) / HOUR


def _hundredths(value: Decimal) -> str:
    return str(value.quantize(HUNDREDTH, rounding=ROUND_HALF_UP))


def money(value: Decimal) -> str:
    """An amount rounded half up to cents, as a string such as "15.95"."""
    return _hundredths(value)


def hours(seconds: Decimal | int) -> str:
    """Seconds as hours rounded half up to 0.01, as a string such as "61.34"."""
    return _hundredths(Decimal(seconds) / HOUR)
