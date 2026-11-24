"""shk usage on Marlowe: segments, sreport rows, own runs clipped to segments, prices, errors.

Each command runs the real usage.sh locally behind the Marlowe prelude against UsageSlurm
(tests/fakes/usage_slurm.py), with rows shaped like the Slurm 25.05.2 output read on Marlowe
and a fixed clock; the account, logins, names and job ids are synthetic. America/Los_Angeles
leaves daylight saving time at 02:00 on 2026-11-01, so November 1 has 25 hours there.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from fakes import MARLOWE_ACCOUNT, SRC, marlowe_config
from fakes.usage_slurm import UsageSlurm

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, executor, sites, usage

LIVE = datetime(2026, 11, 24, 3, 32, tzinfo=UTC)  # 2026-11-23 19:32 PST, the live read
TRES = "billing=2,cpu=2,gres/gpu=1,mem=24G,node=1"
SOURCE = "https://marlowe-research.stanford.edu/access/"
ASSUMPTIONS = [
    "An estimate from Slurm accounting and the packaged rates, not the bill; the site bills "
    "the research account (PTA) monthly.",
    "Every run counts for its whole elapsed time, preempted, failed and requeued runs "
    "included; own_runs.by_state shows their share.",
    "CPU time and GPU time are both priced, the CPUs of GPU jobs included.",
    "The 5,000 free introductory GPU-hours are not applied: they are a separate -free account "
    "(QOS medium, partition batch), not this account.",
    "sreport rolls usage up hourly, so the newest hour may be missing.",
]
ZERO = {"gpu_seconds": 0, "cpu_seconds": 0, "gpu_hours": "0.00", "cpu_hours": "0.00"}


def rows(login="", cpu=0, gpu=0, name=None):
    """sreport -P rows of one login ("" for the account row) for cpu and gres/gpu."""
    proper = login if name is None else name
    return [
        f"slurm|{MARLOWE_ACCOUNT}|{login}|{proper}|cpu|{cpu}",
        f"slurm|{MARLOWE_ACCOUNT}|{login}|{proper}|gres/gpu|{gpu}",
    ]


def used(cpu, gpu):
    """The sreport rows of an account whose only member with usage is USER."""
    return rows("", cpu, gpu) + rows("USER", cpu, gpu)


def run(job, state, elapsed, start, end, tres=TRES):
    return f"{job}|{state}|{tres}|{elapsed}|{start}|{end}"


def amount(gpu_seconds, cpu_seconds, gpu_hours, cpu_hours, cost=None):
    return {
        "gpu_seconds": gpu_seconds,
        "cpu_seconds": cpu_seconds,
        "gpu_hours": gpu_hours,
        "cpu_hours": cpu_hours,
        "cost": cost,
    }


def by_state(runs, gpu_seconds, cpu_seconds, gpu_hours, cpu_hours):
    return {
        "runs": runs,
        "gpu_seconds": gpu_seconds,
        "cpu_seconds": cpu_seconds,
        "gpu_hours": gpu_hours,
        "cpu_hours": cpu_hours,
    }


class UsageCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        marlowe_config(self.tmp, command_timeout="30")
        self.now = LIVE
        self.fake = None

    def shk(self, *argv, site="marlowe", **knobs):
        """(exit code, reply, reply text) of `shk usage ARGV` against a fresh UsageSlurm."""
        self.fake = UsageSlurm(self.tmp, **knobs)
        ctx = cli.Context(
            project_dir=self.tmp,
            executor="local",
            site=site,
            env={},
            hostname="lab-ubuntu",
            clock=lambda: self.now,
        )
        out = io.StringIO()
        with (
            patch.dict(os.environ, self.fake.env()),
            redirect_stdout(out),
            redirect_stderr(io.StringIO()),
        ):
            code = cli.main(["usage", *argv], modules=[usage], context=ctx)
        return code, json.loads(out.getvalue()), out.getvalue()

    def ok(self, *argv, **knobs):
        code, reply, _ = self.shk(*argv, **knobs)
        self.assertEqual((code, reply["ok"]), (0, True), reply)
        return reply

    def refused(self, code, token, *argv, **knobs):
        found, reply, _ = self.shk(*argv, **knobs)
        self.assertEqual((found, reply["ok"], reply["error"]), (code, False, token), reply)
        return reply

    def boundaries(self):
        """The Start and End of every sreport call, and the -S and -E of the sacct call."""
        windows = [tuple(call["argv"][-2:]) for call in self.fake.calls("sreport")]
        (sacct,) = self.fake.calls("sacct")
        return windows, (sacct["argv"][9], sacct["argv"][11])


class LiveShapes(UsageCase):
    def test_november_matches_the_live_sample(self):
        reply = self.ok(
            "--start",
            "2026-11-01",
            sreport={("2026-11-01", "now"): used(443956, 220812)},
            sacct=[
                run(100001, "PREEMPTED", 874, "2026-11-23T00:51:02", "2026-11-23T01:05:36"),
                run(100002, "COMPLETED", 320, "2026-11-23T01:48:15", "2026-11-23T01:53:35"),
                run(100003, "PREEMPTED", 1013, "2026-11-23T01:48:15", "2026-11-23T02:05:08"),
            ],
        )
        self.assertEqual(
            list(reply),
            [
                "schema_version",
                "ok",
                "executor",
                "site",
                "account",
                "user",
                "range",
                "rates",
                "segments",
                "total",
                "warnings",
                "assumptions",
                "queried_at",
                "next_check_after",
            ],
        )
        self.assertEqual(
            (reply["executor"], reply["site"], reply["account"], reply["user"]),
            ("local", "marlowe", MARLOWE_ACCOUNT, "USER"),
        )
        self.assertEqual(reply["range"], {"start": "2026-11-01", "end": "now"})
        self.assertEqual(
            reply["rates"],
            {
                "source": SOURCE,
                "read_on": "2026-11-23",
                "firm_until": "2026-12-23",
                "tier": "basic",
                "tier_text": "Basic Access: base project account, QOS normal, partition preempt",
                "currency": "USD",
                "covered": {"start": "2026-01-01", "end": "2027-09-01"},
            },
        )
        (segment,) = reply["segments"]
        self.assertEqual(
            list(segment),
            [
                "month",
                "start",
                "end",
                "reported",
                "rate",
                "provisional",
                "account",
                "own",
                "others",
                "own_runs",
            ],
        )
        self.assertEqual(
            (segment["month"], segment["start"], segment["end"], segment["provisional"]),
            ("2026-11", "2026-11-01", "now", False),
        )
        self.assertEqual(
            segment["reported"], {"start": "2026-11-01T00:00:00", "end": "2026-11-23T19:59:59"}
        )
        self.assertEqual(segment["rate"], {"gpu_hour": "0.25", "cpu_hour": "0.005"})
        november = amount(220812, 443956, "61.34", "123.32", "15.95")
        self.assertEqual(segment["account"], november)
        self.assertEqual(segment["account"]["cost"], "15.95")
        self.assertEqual(segment["account"]["gpu_hours"], "61.34")
        self.assertEqual(segment["own"], segment["account"])
        self.assertEqual(segment["others"]["gpu_seconds"], 0)
        self.assertEqual(segment["others"], {**ZERO, "cost": "0.00"})
        self.assertEqual(
            segment["own_runs"],
            {
                "runs": 3,
                "by_state": {
                    "COMPLETED": by_state(1, 320, 640, "0.09", "0.18"),
                    "PREEMPTED": by_state(2, 1887, 3774, "0.52", "1.05"),
                },
            },
        )
        totals = {"provisional_cost": "0.00", "unpriced_gpu_hours": "0.00"}
        totals["unpriced_cpu_hours"] = "0.00"
        self.assertEqual(
            reply["total"],
            {
                "account": {**november, **totals},
                "own": {**november, **totals},
                "others": {**ZERO, "cost": "0.00", **totals},
                "cost_complete": True,
            },
        )
        self.assertIs(reply["total"]["cost_complete"], True)
        self.assertEqual(reply["warnings"], [])
        self.assertEqual(len(reply["assumptions"]), 5)
        self.assertEqual(reply["assumptions"], ASSUMPTIONS)
        self.assertEqual(
            (reply["queried_at"], reply["next_check_after"]),
            ("2026-11-24T03:32:00Z", "2026-11-24T03:33:00Z"),
        )
        self.assertEqual(
            self.boundaries(), ([("Start=2026-11-01", "End=now")], ("2026-11-01", "now"))
        )

    def test_others_are_aggregated_without_names(self):
        code, reply, text = self.shk(
            "--start",
            "2026-11-01",
            sreport={
                ("2026-11-01", "now"): [
                    *rows("", 443956 + 7200, 220812 + 3600),
                    *rows("USER", 443956, 220812),
                    *rows("someone", 7200, 3600, name="Some Body"),
                ]
            },
        )
        self.assertEqual(code, 0, reply)
        (segment,) = reply["segments"]
        self.assertEqual(segment["others"]["gpu_seconds"], 3600)
        self.assertEqual(segment["others"], amount(3600, 7200, "1.00", "2.00", "0.26"))
        self.assertEqual(segment["own"], amount(220812, 443956, "61.34", "123.32", "15.95"))
        self.assertEqual(segment["account"]["gpu_seconds"], 224412)
        self.assertEqual(reply["total"]["others"]["cost"], "0.26")
        for word in ("someone", "Some Body"):
            self.assertNotIn(word, text)


class Segments(UsageCase):
    def test_gap_segment_is_unpriced(self):
        reply = self.ok(
            "--start",
            "2025-12-01",
            sreport={
                ("2025-12-01", "2026-01-01"): used(471636, 0),
                ("2026-11-01", "now"): used(443956, 220812),
            },
        )
        segments = reply["segments"]
        self.assertEqual(len(segments), 12)
        december = segments[0]
        self.assertEqual((december["month"], december["end"]), ("2025-12", "2026-01-01"))
        self.assertIsNone(december["rate"])
        self.assertIsNone(december["account"]["cost"])
        for party in ("account", "own", "others"):
            self.assertIsNone(december[party]["cost"])
        self.assertEqual(december["account"]["cpu_hours"], "131.01")
        self.assertEqual(segments[1]["rate"], {"gpu_hour": "0.20", "cpu_hour": "0.005"})
        self.assertEqual(segments[-1]["account"]["cost"], "15.95")
        total = reply["total"]["account"]
        self.assertEqual(total["unpriced_cpu_hours"], december["account"]["cpu_hours"])
        self.assertEqual(total["unpriced_gpu_hours"], "0.00")
        self.assertEqual(
            (total["cpu_seconds"], total["cpu_hours"], total["cost"]),
            (471636 + 443956, "254.33", "15.95"),
        )
        self.assertIs(reply["total"]["cost_complete"], False)
        self.assertEqual(
            reply["warnings"],
            ["2025-12-01 to 2026-01-01: no packaged rate; usage shown, not priced"],
        )
        windows, sacct = self.boundaries()
        self.assertEqual(windows[0], ("Start=2025-12-01", "End=2026-01-01"))
        self.assertEqual(windows[-1], ("Start=2026-11-01", "End=now"))
        self.assertEqual(sacct, ("2025-12-01", "now"))

    def test_stale_table_warnings(self):
        self.now = datetime(2027, 10, 15, 12, 0, tzinfo=UTC)
        reply = self.ok(
            "--start",
            "2027-08-01",
            sreport={
                ("2027-08-01", "2027-09-01"): used(72000, 36000),
                ("2027-09-01", "2027-10-01"): used(0, 7200),
            },
            now_end="2027-10-15T05:59:59",
        )
        segments = reply["segments"]
        self.assertEqual(
            [(s["start"], s["end"], s["provisional"]) for s in segments],
            [
                ("2027-08-01", "2027-09-01", True),
                ("2027-09-01", "2027-10-01", True),
                ("2027-10-01", "now", True),
            ],
        )
        self.assertEqual(segments[0]["rate"], {"gpu_hour": "0.25", "cpu_hour": "0.005"})
        self.assertEqual(segments[0]["account"]["cost"], "2.60")
        self.assertEqual([s["rate"] for s in segments[1:]], [None, None])
        total = reply["total"]["account"]
        provisional = sum(
            Decimal(s["account"]["cost"])
            for s in segments
            if s["provisional"] and s["rate"] is not None
        )
        self.assertEqual(Decimal(total["provisional_cost"]), provisional)
        self.assertEqual((total["provisional_cost"], total["cost"]), ("2.60", "2.60"))
        self.assertEqual(total["unpriced_gpu_hours"], "2.00")
        self.assertIs(reply["total"]["cost_complete"], False)
        self.assertEqual(
            reply["warnings"],
            [
                "2027-09-01 to 2027-10-01: no packaged rate; usage shown, not priced",
                "2027-10-01 to now: no packaged rate; usage shown, not priced",
                "usage from 2026-12-23 on is priced at the table read on 2026-11-23; the site "
                f"may have announced a new rate since: check {SOURCE} or upgrade shk",
                "the rate table ends 2027-09-01: upgrade shk for later rates",
            ],
        )

    def test_provisional_cost_leaves_out_the_firm_part(self):
        self.now = datetime(2027, 1, 15, 20, 0, tzinfo=UTC)
        reply = self.ok(
            "--start",
            "2026-12-01",
            sreport={
                ("2026-12-01", "2026-12-23"): used(0, 36000),
                ("2026-12-23", "2027-01-01"): used(0, 7200),
                ("2027-01-01", "now"): used(0, 3600),
            },
            now_end="2027-01-15T12:59:59",
        )
        self.assertEqual(
            [
                (s["month"], s["end"], s["provisional"], s["account"]["cost"])
                for s in reply["segments"]
            ],
            [
                ("2026-12", "2026-12-23", False, "2.50"),
                ("2026-12", "2027-01-01", True, "0.50"),
                ("2027-01", "now", True, "0.25"),
            ],
        )
        total = reply["total"]["account"]
        self.assertEqual((total["cost"], total["provisional_cost"]), ("3.25", "0.75"))
        self.assertIs(reply["total"]["cost_complete"], True)
        self.assertEqual(
            reply["warnings"],
            [
                "usage from 2026-12-23 on is priced at the table read on 2026-11-23; the site "
                f"may have announced a new rate since: check {SOURCE} or upgrade shk"
            ],
        )

    def test_end_now_uses_site_local_today(self):
        # 05:00 UTC on December 1 is 21:00 on November 30 in Los Angeles, whatever this
        # machine's zone: the range ends now, in November, without a December segment.
        self.now = datetime(2026, 12, 1, 5, 0, tzinfo=UTC)
        self.addCleanup(time.tzset)
        with patch.dict(os.environ, {"TZ": "Asia/Tokyo"}):
            time.tzset()
            for end in ((), ("--end", "2026-12-05")):
                with self.subTest(end=end):
                    reply = self.ok("--start", "2026-11-01", *end, now_end="2026-11-30T21:59:59")
                    (segment,) = reply["segments"]
                    self.assertEqual(
                        (segment["month"], segment["start"], segment["end"]),
                        ("2026-11", "2026-11-01", "now"),
                    )
                    self.assertEqual(reply["range"], {"start": "2026-11-01", "end": "now"})
                    self.assertEqual(
                        self.boundaries(),
                        ([("Start=2026-11-01", "End=now")], ("2026-11-01", "now")),
                    )

    def test_a_closed_range_ends_on_its_date(self):
        reply = self.ok(
            "--start",
            "2026-10-01",
            "--end",
            "2026-11-01",
            sreport={("2026-10-01", "2026-11-01"): used(7200, 3600)},
        )
        (segment,) = reply["segments"]
        self.assertEqual((segment["month"], segment["end"]), ("2026-10", "2026-11-01"))
        self.assertEqual(
            segment["reported"], {"start": "2026-10-01T00:00:00", "end": "2026-10-31T23:59:59"}
        )
        self.assertEqual(reply["range"], {"start": "2026-10-01", "end": "2026-11-01"})
        self.assertEqual(
            self.boundaries(),
            ([("Start=2026-10-01", "End=2026-11-01")], ("2026-10-01", "2026-11-01")),
        )


class OwnRuns(UsageCase):
    def test_run_across_a_month_boundary_is_split(self):
        # 01:00 on November 1 is the first 01:00 there (PDT): the run held two real hours.
        reply = self.ok(
            "--start",
            "2026-10-01",
            sacct=[
                run(
                    100001,
                    "COMPLETED",
                    7200,
                    "2026-10-31T23:00:00",
                    "2026-11-01T01:00:00",
                    "cpu=2,gres/gpu=1",
                )
            ],
        )
        october, november = reply["segments"]
        self.assertEqual((october["month"], november["month"]), ("2026-10", "2026-11"))
        for segment in (october, november):
            with self.subTest(month=segment["month"]):
                self.assertEqual(
                    segment["own_runs"],
                    {"runs": 1, "by_state": {"COMPLETED": by_state(1, 3600, 7200, "1.00", "2.00")}},
                )
                self.assertEqual(segment["own"]["gpu_seconds"], 0)

    def test_a_run_across_the_end_of_daylight_saving_time_is_split_in_real_seconds(self):
        # 22:00 PDT on October 31 to 03:00 PST on November 1 is six real hours (five on the wall
        # clock): two before midnight and four after it.
        reply = self.ok(
            "--start",
            "2026-10-01",
            sacct=[run(100002, "COMPLETED", 21600, "2026-10-31T22:00:00", "2026-11-01T03:00:00")],
        )
        october, november = (s["own_runs"]["by_state"]["COMPLETED"] for s in reply["segments"])
        self.assertEqual(october, by_state(1, 7200, 14400, "2.00", "4.00"))
        self.assertEqual(november, by_state(1, 14400, 28800, "4.00", "8.00"))

    def test_running_never_started_and_requeued_rows(self):
        # --start today: one segment from midnight to now (19:32 PST). The running job began
        # 24 h ago, so 19 h 32 min of its ElapsedRaw lie in the range.
        reply = self.ok(
            "--start",
            "2026-11-23",
            sacct=[
                run(100010, "RUNNING", 86400, "2026-11-22T19:32:00", "Unknown"),
                run(100011, "PENDING", 0, "Unknown", "Unknown", tres=""),
                run(100012, "CANCELLED by 1000", 0, "None", "2026-11-23T04:00:00", tres=""),
                run(100013, "PREEMPTED", 600, "2026-11-23T01:00:00", "2026-11-23T01:10:00"),
                run(100013, "PREEMPTED", 900, "2026-11-23T02:00:00", "2026-11-23T02:15:00"),
                run(100013, "COMPLETED", 1200, "2026-11-23T03:00:00", "2026-11-23T03:20:00"),
                run(100014, "CANCELLED by 1000", 300, "2026-11-23T05:00:00", "2026-11-23T05:05:00"),
            ],
        )
        (segment,) = reply["segments"]
        self.assertEqual((segment["start"], segment["end"]), ("2026-11-23", "now"))
        self.assertEqual(
            segment["own_runs"],
            {
                "runs": 5,
                "by_state": {
                    "CANCELLED": by_state(1, 300, 600, "0.08", "0.17"),
                    "COMPLETED": by_state(1, 1200, 2400, "0.33", "0.67"),
                    "PREEMPTED": by_state(2, 1500, 3000, "0.42", "0.83"),
                    "RUNNING": by_state(1, 70320, 140640, "19.53", "39.07"),
                },
            },
        )

    def test_shares_keep_fractions_until_the_reply(self):
        # 100 s over 3 wall-clock seconds around midnight: 33.3 s before and 66.7 s after, which
        # round half up to 33 and 67 and add up to the whole run.
        reply = self.ok(
            "--start",
            "2026-10-01",
            sacct=[
                run(100020, "COMPLETED", 100, "2026-10-31T23:59:59", "2026-11-01T00:00:02", "cpu=1")
            ],
        )
        october, november = (s["own_runs"]["by_state"]["COMPLETED"] for s in reply["segments"])
        self.assertEqual(october, by_state(1, 0, 33, "0.00", "0.01"))
        self.assertEqual(november, by_state(1, 0, 67, "0.00", "0.02"))


class Errors(UsageCase):
    def assert_no_call(self):
        self.assertEqual(self.fake.state()["calls"], [])

    def test_unknown_account_is_refused(self):
        reply = self.refused(1, "account_not_found", "--start", "2026-11-01", assoc=False)
        self.assertIn(MARLOWE_ACCOUNT, reply["message"])

    def test_failed_tool_is_malformed_read(self):
        for tool in ("sacct", "sreport", "sacctmgr"):
            with self.subTest(tool=tool):
                reply = self.refused(1, "malformed_read", "--start", "2026-11-01", rc={tool: 1})
                self.assertIn(f"{tool} rc=1", reply["message"])
                self.assertEqual(
                    reply["hint"],
                    "retry once after 60 s; shk --site marlowe doctor --remote checks the remote "
                    "tools",
                )

    def test_sherlock_has_no_rates(self):
        (self.tmp / "sherlock-kit.toml").write_text('project = "demo"\n')
        reply = self.refused(3, "no_rates", "--start", "2026-11-01", site=None)
        self.assertIn("shk --site marlowe usage", reply["hint"])
        self.assert_no_call()

    def test_invalid_range(self):
        cases = {
            "an empty range": ("--start", "2026-11-10", "--end", "2026-11-10"),
            "an end before the start": ("--start", "2026-11-10", "--end", "2026-11-01"),
            "a start after today": ("--start", "2026-11-24"),
        }
        for label, argv in cases.items():
            with self.subTest(label):
                self.refused(2, "invalid_range", *argv)
                self.assert_no_call()

    def test_missing_account_is_config_required_before_any_call(self):
        (self.tmp / "sherlock-kit.toml").write_text('site = "marlowe"\nproject = "demo-mw"\n')
        reply = self.refused(1, "config_required", "--start", "2026-11-01")
        self.assertIn("account", reply["message"])
        self.assert_no_call()

    def test_a_value_that_is_not_a_date_is_a_usage_error(self):
        for argv in (("--start", "2026-13-01"), ("--start", "2026-11-01", "--end", "soon"), ()):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
                    cli.main(["usage", *argv], modules=[usage])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn("usage", err.getvalue())


class Records(UsageCase):
    """Replies crafted beside the program: what the parser does with each record."""

    HEAD = ("SHK_USER USER", "SHK_ASSOC 1", "SHK_RC sacctmgr 0")
    TAIL = ("SHK_RC sreport 0", "SHK_RC sacct 0", "SHK_END")

    def answer(self, *lines):
        """`shk usage --start 2026-11-01` when usage.sh answers lines on Marlowe."""
        stdout = "".join(f"{line}\n" for line in lines)
        result = executor.Result("complete", stdout, "", 0, site=sites.MARLOWE)
        with patch.object(executor, "run_program", return_value=result):
            return self.shk("--start", "2026-11-01")

    def test_a_segment_without_a_banner_reports_null(self):
        code, reply, _ = self.answer(
            *self.HEAD, *(f"SHK_R|1|{r}" for r in used(3600, 0)), *self.TAIL
        )
        self.assertEqual(code, 0, reply)
        (segment,) = reply["segments"]
        self.assertIsNone(segment["reported"])
        self.assertEqual(segment["account"]["cpu_seconds"], 3600)

    def test_a_missing_end_marker_is_malformed(self):
        code, reply, _ = self.answer(*self.HEAD, *self.TAIL[:-1])
        self.assertEqual((code, reply["error"]), (1, "malformed_read"), reply)

    def test_a_malformed_row_names_nobody(self):
        row = f"slurm|{MARLOWE_ACCOUNT}|someone|Some Body|gres/gpu|many"
        code, reply, text = self.answer(*self.HEAD, f"SHK_R|1|{row}", *self.TAIL)
        self.assertEqual((code, reply["error"]), (1, "malformed_read"), reply)
        for word in ("someone", "Some Body"):
            self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
