"""The packaged Marlowe rate table, site-local today, usage segments and Decimal rounding."""

import copy
import json
import os
import sys
import time
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest import mock

from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import rates

RAW = SRC / "sherlock_kit" / "data" / "rates-marlowe.json"


class RatesCase(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(RAW.read_text(encoding="utf-8"))
        self.table = rates.load("rates-marlowe.json")
        self.p1, self.p2 = self.table.periods

    def rows(self, found):
        return [(s.start, s.end, s.month, s.period, s.provisional) for s in found]


class RateTableTests(RatesCase):
    def test_packaged_marlowe_table(self):
        self.assertEqual(self.raw["schema_version"], 1)
        table, p1, p2 = self.table, self.p1, self.p2
        self.assertEqual(len(table.periods), 2)
        self.assertEqual(
            (p1.start, p1.end, p1.gpu_hour, p1.cpu_hour),
            (date(2026, 1, 1), date(2026, 9, 1), Decimal("0.20"), Decimal("0.005")),
        )
        self.assertEqual(
            (p2.start, p2.end, p2.gpu_hour, p2.cpu_hour),
            (date(2026, 9, 1), date(2027, 9, 1), Decimal("0.25"), Decimal("0.005")),
        )
        self.assertEqual(str(p1.gpu_hour), "0.20")
        self.assertEqual(table.read_on, date(2026, 11, 23))
        self.assertEqual(table.firm_until, date(2026, 12, 23))
        self.assertEqual(table.covered, (date(2026, 1, 1), date(2027, 9, 1)))
        self.assertEqual(table.timezone, "America/Los_Angeles")
        self.assertEqual(table.currency, "USD")
        self.assertEqual(table.tier, "basic")
        self.assertEqual(
            table.tier_text, "Basic Access: base project account, QOS normal, partition preempt"
        )
        self.assertEqual(table.source, "https://marlowe-research.stanford.edu/access/")
        self.assertEqual(rates.parse(self.raw), table)
        self.assertIsNone(rates.parse({**self.raw, "periods": []}).covered)

    def test_invalid_tables_are_refused(self):
        def changed(edit):
            data = copy.deepcopy(self.raw)
            edit(data)
            return data

        def overlap(data):
            data["periods"][0]["end"] = "2026-10-01"

        def unsorted(data):
            data["periods"].reverse()

        def empty(data):
            data["periods"][0]["end"] = data["periods"][0]["start"]

        def backwards(data):
            data["periods"][1]["end"] = "2026-08-01"

        def float_price(data):
            data["periods"][0]["gpu_hour"] = 0.2

        def word_price(data):
            data["periods"][1]["cpu_hour"] = "abc"

        def nan_price(data):
            data["periods"][1]["gpu_hour"] = "NaN"

        def no_read_on(data):
            del data["read_on"]

        def no_price(data):
            del data["periods"][0]["gpu_hour"]

        cases = [
            overlap,
            unsorted,
            empty,
            backwards,
            float_price,
            word_price,
            nan_price,
            no_read_on,
            no_price,
        ]
        for edit in cases:
            with self.subTest(edit.__name__), self.assertRaises(ValueError):
                rates.parse(changed(edit))

    def test_add_month(self):
        cases = {
            date(2026, 11, 23): date(2026, 12, 23),
            date(2027, 1, 31): date(2027, 2, 28),
            date(2028, 1, 31): date(2028, 2, 29),
            date(2026, 12, 15): date(2027, 1, 15),
        }
        for day, expected in cases.items():
            with self.subTest(day=day):
                self.assertEqual(rates.add_month(day), expected)

    def test_today_is_site_local(self):
        # 05:00 UTC on Dec 1 is 21:00 on Nov 30 in Los Angeles, whatever the machine's zone.
        self.addCleanup(time.tzset)
        with mock.patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
            time.tzset()
            now = datetime(2026, 12, 1, 5, 0, tzinfo=UTC)
            self.assertEqual(rates.site_today(self.table, now), date(2026, 11, 30))
            # Daylight saving time: UTC-7 in July.
            summer = datetime(2026, 7, 1, 6, 59, tzinfo=UTC)
            self.assertEqual(rates.site_today(self.table, summer), date(2026, 6, 30))
            summer = datetime(2026, 7, 1, 7, 0, tzinfo=UTC)
            self.assertEqual(rates.site_today(self.table, summer), date(2026, 7, 1))


class SegmentTests(RatesCase):
    def test_segments_split_at_months_rates_and_firm_until(self):
        found = rates.segments(self.table, date(2025, 12, 1), None, date(2026, 11, 23))
        expected = [(date(2025, 12, 1), date(2026, 1, 1), "2025-12", None, False)]
        expected += [
            (date(2026, m, 1), date(2026, m + 1, 1), f"2026-{m:02d}", self.p1, False)
            for m in range(1, 9)
        ]
        expected += [
            (date(2026, 9, 1), date(2026, 10, 1), "2026-09", self.p2, False),
            (date(2026, 10, 1), date(2026, 11, 1), "2026-10", self.p2, False),
            (date(2026, 11, 1), None, "2026-11", self.p2, False),
        ]
        self.assertEqual(len(expected), 12)
        self.assertEqual(self.rows(found), expected)

    def test_segments_after_firm_until_are_provisional(self):
        found = rates.segments(self.table, date(2026, 12, 1), None, date(2027, 3, 10))
        self.assertEqual(
            self.rows(found),
            [
                (date(2026, 12, 1), date(2026, 12, 23), "2026-12", self.p2, False),
                (date(2026, 12, 23), date(2027, 1, 1), "2026-12", self.p2, True),
                (date(2027, 1, 1), date(2027, 2, 1), "2027-01", self.p2, True),
                (date(2027, 2, 1), date(2027, 3, 1), "2027-02", self.p2, True),
                (date(2027, 3, 1), None, "2027-03", self.p2, True),
            ],
        )

    def test_segments_after_the_table_are_unpriced(self):
        found = rates.segments(self.table, date(2027, 8, 1), date(2027, 10, 1), date(2027, 12, 1))
        self.assertEqual(
            self.rows(found),
            [
                (date(2027, 8, 1), date(2027, 9, 1), "2027-08", self.p2, True),
                (date(2027, 9, 1), date(2027, 10, 1), "2027-09", None, True),
            ],
        )

    def test_explicit_end_and_end_after_today(self):
        today = date(2026, 11, 23)
        november = [(date(2026, 11, 1), None, "2026-11", self.p2, False)]
        for end in (None, date(2026, 11, 24), date(2027, 1, 1)):
            with self.subTest(end=end):
                found = rates.segments(self.table, date(2026, 11, 1), end, today)
                self.assertEqual(self.rows(found), november)
        found = rates.segments(self.table, date(2026, 11, 1), today, today)
        self.assertEqual(self.rows(found), [(date(2026, 11, 1), today, "2026-11", self.p2, False)])
        found = rates.segments(self.table, date(2026, 10, 1), date(2026, 11, 1), today)
        self.assertEqual(
            self.rows(found), [(date(2026, 10, 1), date(2026, 11, 1), "2026-10", self.p2, False)]
        )
        found = rates.segments(self.table, date(2026, 10, 15), date(2026, 11, 10), today)
        self.assertEqual(
            self.rows(found),
            [
                (date(2026, 10, 15), date(2026, 11, 1), "2026-10", self.p2, False),
                (date(2026, 11, 1), date(2026, 11, 10), "2026-11", self.p2, False),
            ],
        )
        # A start that is not before the end gives no segment.
        self.assertEqual(rates.segments(self.table, today, today, today), [])
        self.assertEqual(rates.segments(self.table, date(2026, 11, 24), None, today), [])

    def test_cost_and_rounding(self):
        value = rates.cost(220812, 443956, self.p2)
        self.assertEqual(
            value,
            Decimal(220812) / 3600 * Decimal("0.25") + Decimal(443956) / 3600 * Decimal("0.005"),
        )
        self.assertIsInstance(value, Decimal)
        self.assertEqual(rates.money(value), "15.95")
        self.assertEqual(rates.hours(220812), "61.34")
        self.assertEqual(rates.hours(Decimal(443956)), "123.32")
        self.assertEqual(rates.money(Decimal("0.005")), "0.01")
        self.assertEqual(rates.money(Decimal(0)), "0.00")
        self.assertEqual(rates.hours(0), "0.00")


if __name__ == "__main__":
    unittest.main()
