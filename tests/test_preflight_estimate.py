"""The start estimate of shk submit --preflight: the parser, and the reply through preflight.sh.

The estimate line is Slurm's will-run message (src/api/allocate.c 641-647 at slurm-25-11-9-1).
LIVE is the line observed on Sherlock (25.11.8) on stderr; every other line is synthetic.
"""

import itertools
import sys
import unittest
from unittest.mock import patch

import test_control
from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import control, slurm

JOB = "48350340"
LIVE = (
    f"sbatch: Job {JOB} to start at 2026-10-22T13:24:32 a using 1 processors"
    " on nodes sh02-01n56 in partition normal"
)
OTHER = (
    "sbatch: Job 48350341 to start at 2026-10-22T14:00:00 a using 1 processors"
    " on nodes sh03-01n01 in partition owners"
)
UNKNOWN = LIVE.replace("2026-10-22T13:24:32", "Unknown")
HETEROGENEOUS = (
    f"sbatch: Job {JOB} to start at 2026-10-22T13:24:32 using 2 processors on sh02-01n[56-57]"
)
DEBUG = (
    f"sbatch: debug:  JobId={JOB} to start at 2026-10-22T13:24:32 on cluster sherlock"
    " using 1 processors on nodes sh02-01n56 in partition normal"
)
NOTE = "sbatch: job_submit: requests without --mem get 4000 MB per CPU"
JOB_SUBMIT = "sbatch: job_submit: jobs on normal are expected to start at most 2 h after submission"
PREEMPTS = "sbatch:   Preempts: 47000001,47000002"
REJECTED = (
    "sbatch: error: QOSMaxSubmitJobPerUserLimit\n"
    "allocation failure: Job violates accounting/QOS policy "
    "(job submit limit, user's size and/or time limits)\n"
)


def estimate(status, line=None, start=None, nodes=None, partition=None):
    """The parser's object in contract key order (the reply adds observed_at last)."""
    return {
        "status": status,
        "start": start,
        "start_zone": "sbatch-local",
        "nodes": nodes,
        "partition": partition,
        "line": line,
    }


def live():
    return estimate("parsed", LIVE, "2026-10-22T13:24:32", "sh02-01n56", "normal")


class ParseEstimate(unittest.TestCase):
    def test_recorded_live_line_parses(self):
        found = control.parse_estimate(LIVE + "\n", "")
        self.assertEqual(found, live())
        self.assertEqual(list(found), list(live()))

    def test_older_cluster_ranged_and_unprefixed_forms_parse(self):
        stamp = "2026-10-22T13:24:32"
        cases = (
            # 24.11 and older: no " a" between the time and "using".
            (LIVE.replace(f"{stamp} a using", f"{stamp} using"), "sh02-01n56", "normal"),
            (LIVE.replace(f"{stamp} a", f"{stamp} on cluster sherlock a"), "sh02-01n56", "normal"),
            (
                f"sbatch: Job {JOB} to start at {stamp} a using 8 processors"
                " on nodes sh03-12n[01-04,07],sh04-01n01 in partition gpu",
                "sh03-12n[01-04,07],sh04-01n01",
                "gpu",
            ),
            (LIVE.removeprefix("sbatch: "), "sh02-01n56", "normal"),
        )
        for line, nodes, partition in cases:
            with self.subTest(line=line):
                self.assertEqual(
                    control.parse_estimate(f"{line}\n", ""),
                    estimate("parsed", line, stamp, nodes, partition),
                )

    def test_one_estimate_line_among_other_lines_parses(self):
        # A job_submit message (free text, printed before the estimate) may hold the words too;
        # the -vv debug line and the Preempts line are no estimate lines.
        for lines in (
            [JOB_SUBMIT, LIVE],
            [LIVE, PREEMPTS],
            [DEBUG, LIVE],
            [LIVE, DEBUG],
            [JOB_SUBMIT, DEBUG, LIVE, PREEMPTS],
        ):
            with self.subTest(lines=lines):
                self.assertEqual(control.parse_estimate("\n".join(lines) + "\n", ""), live())

    def test_lines_are_stderr_then_stdout_each_split_on_lf(self):
        self.assertEqual(control.parse_estimate("", LIVE + "\n"), live())
        self.assertEqual(control.parse_estimate("sbatch: note", LIVE), live())
        # The unterminated last stderr line is not joined to the first stdout line.
        head, tail = LIVE.split(" on nodes ")
        self.assertEqual(
            control.parse_estimate(head, f" on nodes {tail}\n"), estimate("unparsed", head)
        )
        # An estimate line on each stream: two, and stderr's comes first.
        self.assertEqual(control.parse_estimate(OTHER, LIVE), estimate("unparsed", OTHER))
        # str.splitlines() would also break at CR, VT and FS, and read a field from the rest.
        for separator in ("\r", "\x0b", "\x1c"):
            with self.subTest(separator=separator):
                line = f"{LIVE}{separator}after"
                self.assertEqual(
                    control.parse_estimate(f"{line}\n", ""), estimate("unparsed", line)
                )

    def test_without_an_estimate_line_is_absent(self):
        for stderr, stdout in (
            ("", ""),
            ("\n", "\n\n"),
            (f"{NOTE}\n", "--test-only\n"),
            (f"{PREEMPTS}\n", ""),
            (REJECTED, ""),
        ):
            with self.subTest(stderr=stderr, stdout=stdout):
                self.assertEqual(control.parse_estimate(stderr, stdout), estimate("absent"))

    def test_unrecognized_estimates_are_unparsed_with_nulls(self):
        stamp = "2026-10-22T13:24:32"
        for line in (
            UNKNOWN,
            LIVE.replace(stamp, "None"),
            LIVE.replace(stamp, "####################"),
            LIVE.replace(stamp, "2026-10-22T13:24"),  # a custom format without seconds
            LIVE.replace(stamp, "22.10.2026-13:24:32"),
            LIVE.replace(stamp, f"{stamp}-0700"),  # a custom format that extends the ISO shape
            LIVE.replace(stamp, "Tomorr 13:24"),  # a relative time holds a space
            LIVE.replace("sh02-01n56", ""),  # an empty node list
            HETEROGENEOUS,
            LIVE + " (estimated)",
            " " + LIVE,
            DEBUG,
        ):
            with self.subTest(line=line):
                self.assertEqual(
                    control.parse_estimate(f"{NOTE}\n{line}\n{PREEMPTS}\n", ""),
                    estimate("unparsed", line),
                )

    def test_two_estimate_lines_are_unparsed(self):
        for lines in ([LIVE, OTHER], [LIVE, LIVE], [LIVE, UNKNOWN], [UNKNOWN, LIVE]):
            with self.subTest(lines=lines):
                self.assertEqual(
                    control.parse_estimate("\n".join(lines) + "\n", ""),
                    estimate("unparsed", lines[0]),
                )

    def test_unparsed_names_the_first_estimate_line_else_the_first_line_with_the_words(self):
        for stderr, stdout, line in (
            (f"{DEBUG}\n{UNKNOWN}\n", "", UNKNOWN),
            (f"{JOB_SUBMIT}\n{HETEROGENEOUS}\n{LIVE}\n{OTHER}\n", "", LIVE),
            (f"{DEBUG}\n", UNKNOWN, UNKNOWN),
            (f"{JOB_SUBMIT}\n{HETEROGENEOUS}\n", "", JOB_SUBMIT),
            (f"{HETEROGENEOUS}\n", DEBUG, HETEROGENEOUS),
        ):
            with self.subTest(stderr=stderr, stdout=stdout):
                self.assertEqual(control.parse_estimate(stderr, stdout), estimate("unparsed", line))

    def test_no_field_holds_the_job_id(self):
        for stderr in (LIVE, f"{DEBUG}\n{LIVE}", UNKNOWN, DEBUG, HETEROGENEOUS):
            with self.subTest(stderr=stderr):
                found = control.parse_estimate(stderr, "")
                self.assertNotIn(JOB, found.values())
                self.assertIn(JOB, found["line"])


class EstimateFlow(test_control.ControlFlow):
    """Through the real preflight.sh, with a fake sbatch that prints its message on stderr."""

    sbatch = test_control.PreflightClassification.sbatch

    def preflight(self, stderr, rc=0):
        self.sbatch(stderr, rc)
        code, report = self.submit("--preflight")
        self.assertFalse(self.remote.exists())
        return code, report

    def test_live_line_through_the_real_preflight_program(self):
        code, report = self.preflight(LIVE + "\n")
        self.assertEqual((code, report["ok"], report["action"]), (0, True, "preflight"))
        self.assertEqual(
            list(report),
            [
                "schema_version",
                "ok",
                "action",
                "job_submitted",
                "name",
                "run_dir",
                "argv",
                "script_sha256",
                "spec",
                "preflight",
                "estimate",
                "executor",
                "queried_at",
                "next_check_after",
            ],
        )
        self.assertEqual(
            report["preflight"], {"returncode": 0, "stdout": "", "stderr": LIVE + "\n"}
        )
        self.assertEqual(report["estimate"], {**live(), "observed_at": report["queried_at"]})
        self.assertEqual(list(report["estimate"]), [*live(), "observed_at"])
        self.assertNotIn(JOB, report["estimate"].values())
        self.assertIn(JOB, report["estimate"]["line"])
        self.assertIn(JOB, report["preflight"]["stderr"])

    def test_observed_at_is_the_one_queried_at_of_the_reply(self):
        ticks = itertools.count()

        def moving(ctx):
            tick = next(ticks)
            return {"queried_at": f"tick-{tick}", "next_check_after": f"tick-{tick}-later"}

        with patch.object(slurm, "_cadence", side_effect=moving):
            code, report = self.preflight(LIVE + "\n")
        self.assertEqual(code, 0)
        self.assertEqual(report["estimate"]["observed_at"], report["queried_at"])
        self.assertEqual(report["next_check_after"], f"{report['queried_at']}-later")

    def test_other_lines_beside_the_estimate_through_the_real_preflight_program(self):
        code, report = self.preflight(f"{JOB_SUBMIT}\n{DEBUG}\n{LIVE}\n{PREEMPTS}\n")
        self.assertEqual(code, 0)
        self.assertEqual(report["estimate"], {**live(), "observed_at": report["queried_at"]})

    def test_accepted_reply_without_the_line_is_absent(self):
        code, report = self.preflight("")
        self.assertEqual((code, report["ok"]), (0, True))
        self.assertEqual(report["preflight"], {"returncode": 0, "stdout": "", "stderr": ""})
        self.assertEqual(
            report["estimate"], {**estimate("absent"), "observed_at": report["queried_at"]}
        )

    def test_rejected_reply_is_absent_and_still_exit_3(self):
        code, report = self.preflight(REJECTED, 1)
        self.assertEqual((code, report["ok"], report["action"]), (3, False, "preflight"))
        self.assertEqual(report["preflight"], {"returncode": 1, "stdout": "", "stderr": REJECTED})
        self.assertEqual(
            report["estimate"], {**estimate("absent"), "observed_at": report["queried_at"]}
        )

    def test_controller_transport_failure_is_still_exit_1(self):
        # Unchanged behavior: a controller that did not answer is a failed read, no reply object.
        code, report = self.preflight(
            "allocation failure: Unable to contact slurm controller (connect failure)\n", 1
        )
        self.assertEqual((code, report["error"]), (1, "malformed_read"))
        self.assertNotIn("estimate", report)

    def test_unparsed_estimate_through_the_real_preflight_program(self):
        code, report = self.preflight(f"{UNKNOWN}\n")
        self.assertEqual((code, report["ok"]), (0, True))
        self.assertEqual(
            report["estimate"],
            {**estimate("unparsed", UNKNOWN), "observed_at": report["queried_at"]},
        )
        self.assertEqual(report["preflight"]["stderr"], f"{UNKNOWN}\n")

    def test_stdout_lines_follow_stderr_lines_through_the_real_preflight_program(self):
        # sbatch --test-only prints nothing on stdout; its lines are still read, after stderr's.
        self.sbatch(f"{OTHER}\n", 0)
        tool = self.tools / "sbatch"
        tool.write_text(tool.read_text().replace("exit 0\n", f"echo '{LIVE}'\nexit 0\n"))
        code, report = self.submit("--preflight")
        self.assertEqual(code, 0)
        self.assertEqual(
            report["preflight"], {"returncode": 0, "stdout": f"{LIVE}\n", "stderr": f"{OTHER}\n"}
        )
        self.assertEqual(
            report["estimate"],
            {**estimate("unparsed", OTHER), "observed_at": report["queried_at"]},
        )
