"""usage.sh, the read-only accounting program of shk usage, run locally against UsageSlurm.

The real program runs behind the Marlowe site-env.sh prelude through executor.run_program; the
prelude appends Marlowe's Slurm directory to PATH, so the fake wrappers first on PATH answer.
Rows copy the shapes of the Slurm 25.05.2 output read on Marlowe.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import MARLOWE_ACCOUNT, SRC
from fakes.usage_slurm import UsageSlurm

sys.path.insert(0, str(SRC))
from sherlock_kit import executor, sites

NOVEMBER = [
    f"slurm|{MARLOWE_ACCOUNT}|||cpu|443956",
    f"slurm|{MARLOWE_ACCOUNT}|||gres/gpu|220812",
    f"slurm|{MARLOWE_ACCOUNT}|USER|USER|cpu|443956",
    f"slurm|{MARLOWE_ACCOUNT}|USER|USER|gres/gpu|220812",
]
TRES = "billing=2,cpu=2,gres/gpu=1,mem=24G,node=1"
RUNS = [
    f"100008|COMPLETED|{TRES}|459|2026-11-23T06:18:01|2026-11-23T06:25:40",
    f"100010|PREEMPTED|{TRES}|629|2026-11-23T06:24:40|2026-11-23T06:35:09",
    f"100011|RUNNING|{TRES}|1200|2026-11-23T19:30:00|Unknown",
    "100012|PENDING||0|Unknown|Unknown",
]
OCTOBER_BANNER = (
    "Cluster/Account/User Utilization 2026-10-01T00:00:00 - 2026-10-31T23:59:59 (2678400 secs)"
)
# The live banner: November 1 holds the end of daylight saving time, one more hour.
NOVEMBER_BANNER = (
    "Cluster/Account/User Utilization 2026-11-01T00:00:00 - 2026-11-23T19:59:59 (1976400 secs)"
)
SREPORT_HEAD = ["-P", "-t", "seconds", "-T", "cpu,gres/gpu", "cluster", "AccountUtilizationByUser"]
SACCT_FORMAT = "--format=JobIDRaw,State,AllocTRES,ElapsedRaw,Start,End"


class UsageProgram(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name)

    def usage(self, *boundaries, env=None, **knobs):
        """(fake, records) of one run of usage.sh ACCOUNT BOUNDARIES... on Marlowe."""
        fake = UsageSlurm(self.tmp, **knobs)
        ex = executor.LocalExecutor(command_timeout=30, site=sites.MARLOWE)
        with patch.dict(os.environ, {**fake.env(), **(env or {})}):
            result = executor.run_program(ex, "usage", [MARLOWE_ACCOUNT, *boundaries])
        self.assertEqual((result.status, result.returncode, result.stderr), ("complete", 0, ""))
        return fake, executor.records(result.stdout)

    def test_records_of_two_segments(self):
        _, records = self.usage(
            "2026-10-01",
            "2026-11-01",
            "now",
            sreport={("2026-11-01", "now"): NOVEMBER},
            sacct=RUNS,
        )
        self.assertEqual(
            records,
            [
                "SHK_USER USER",
                "SHK_ASSOC 1",
                "SHK_RC sacctmgr 0",
                f"SHK_W|1|{OCTOBER_BANNER}",
                f"SHK_W|2|{NOVEMBER_BANNER}",
                *(f"SHK_R|2|{row}" for row in NOVEMBER),
                "SHK_RC sreport 0",
                *(f"SHK_A|{row}" for row in RUNS),
                "SHK_RC sacct 0",
                "SHK_END",
            ],
        )

    def test_the_calls_are_the_documented_argv(self):
        fake, _ = self.usage("2026-10-01", "2026-11-01", "now")
        self.assertEqual([call["argv"] for call in fake.calls("id")], [["-un"]])
        self.assertEqual(
            [call["argv"] for call in fake.calls("sacctmgr")],
            [
                [
                    "-nP",
                    "show",
                    "assoc",
                    "where",
                    "user=USER",
                    f"account={MARLOWE_ACCOUNT}",
                    "format=account",
                ]
            ],
        )
        self.assertEqual(
            [call["argv"] for call in fake.calls("sreport")],
            [
                [
                    *SREPORT_HEAD,
                    f"Accounts={MARLOWE_ACCOUNT}",
                    "Start=2026-10-01",
                    "End=2026-11-01",
                ],
                [*SREPORT_HEAD, f"Accounts={MARLOWE_ACCOUNT}", "Start=2026-11-01", "End=now"],
            ],
        )
        self.assertEqual(
            [call["argv"] for call in fake.calls("sacct")],
            [
                [
                    "-X",
                    "-D",
                    "-n",
                    "-P",
                    "-u",
                    "USER",
                    "-A",
                    MARLOWE_ACCOUNT,
                    "-S",
                    "2026-10-01",
                    "-E",
                    "now",
                    SACCT_FORMAT,
                ]
            ],
        )

    def test_one_segment_that_ends_on_a_date(self):
        fake, records = self.usage("2026-11-01", "2026-11-10")
        self.assertEqual(
            records[3:5],
            [
                "SHK_W|1|Cluster/Account/User Utilization 2026-11-01T00:00:00 - "
                "2026-11-09T23:59:59 (781200 secs)",
                "SHK_RC sreport 0",
            ],
        )
        self.assertEqual(
            fake.calls("sacct")[0]["argv"][8:12], ["-S", "2026-11-01", "-E", "2026-11-10"]
        )

    def test_missing_association(self):
        _, records = self.usage("2026-11-01", "now", assoc=False)
        self.assertEqual(records[1:3], ["SHK_ASSOC 0", "SHK_RC sacctmgr 0"])
        self.assertEqual(records[-1], "SHK_END")

    def test_a_failing_tool_is_reported(self):
        expected = {
            "sacctmgr": ["SHK_ASSOC 0", "SHK_RC sacctmgr 1"],
            "sreport": ["SHK_RC sreport 1"],
            "sacct": ["SHK_RC sacct 1"],
        }
        for tool, lines in expected.items():
            with self.subTest(tool=tool):
                fake = UsageSlurm(self.tmp, sreport={("2026-11-01", "now"): NOVEMBER}, rc={tool: 1})
                ex = executor.LocalExecutor(command_timeout=30, site=sites.MARLOWE)
                with patch.dict(os.environ, fake.env()):
                    result = executor.run_program(
                        ex, "usage", [MARLOWE_ACCOUNT, "2026-10-01", "2026-11-01", "now"]
                    )
                self.assertEqual((result.status, result.returncode), ("complete", 0))
                self.assertIn(f"{tool}: error: Problem talking to the database", result.stderr)
                records = executor.records(result.stdout)
                for line in lines:
                    self.assertIn(line, records)
                self.assertEqual(records[-1], "SHK_END")
                if tool == "sreport":
                    self.assertFalse([r for r in records if r.startswith(("SHK_W|", "SHK_R|"))])
                    self.assertEqual(len(fake.calls("sreport")), 2)

    def test_inherited_slurm_variables_are_scrubbed(self):
        inherited = {
            "SACCT_FORMAT": "JobID,State",
            "SLURM_TIME_FORMAT": "relative",
            "SLURM_CONF": "/etc/slurm/elsewhere.conf",
            "LC_ALL": "en_US.UTF-8",
        }
        fake, _ = self.usage("2026-11-01", "now", env=inherited)
        for tool in ("sacctmgr", "sreport", "sacct"):
            with self.subTest(tool=tool):
                env = fake.calls(tool)[0]["env"]
                self.assertNotIn("SACCT_FORMAT", env)
                self.assertNotIn("SLURM_TIME_FORMAT", env)
                self.assertEqual(
                    (env["LC_ALL"], env["SLURM_CONF"]), ("C", sites.MARLOWE.slurm.conf)
                )


if __name__ == "__main__":
    unittest.main()
