"""Packaged site records, the site-env.sh prelude and the Marlowe partition table."""

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import sites

DATA = SRC / "sherlock_kit" / "data"


class RecordTests(unittest.TestCase):
    def test_sherlock_reproduces_the_constants_of_a_kit_without_sites(self):
        found = sites.site("sherlock")
        self.assertIs(found, sites.SHERLOCK)
        self.assertEqual(
            (found.name, found.display, found.control_host, found.data_host),
            ("sherlock", "Sherlock", "sherlock-plain", "sherlock-dtn"),
        )
        self.assertEqual((found.slurm, found.account), (None, None))
        self.assertEqual(found.instructions, "/etc/agents/AGENTS.md")
        self.assertFalse(found.test_before_submit)
        self.assertEqual(found.partitions, "partitions.json")
        self.assertEqual(
            dict(found.rules), {"workstation": "workstation.md", "cluster": "cluster.md"}
        )
        self.assertEqual(found.skill, "sherlock-kit")
        self.assertTrue(all((DATA / "rules" / name).is_file() for name in found.rules.values()))
        self.assertTrue((DATA / "skills" / found.skill / "SKILL.md").is_file())

    def test_marlowe(self):
        found = sites.site("marlowe")
        self.assertIs(found, sites.MARLOWE)
        self.assertEqual(
            (found.name, found.display, found.control_host, found.data_host),
            ("marlowe", "Marlowe", "marlowe-plain", "marlowe-plain"),
        )
        self.assertEqual(
            (found.slurm.bin, found.slurm.conf),
            ("/cm/shared/apps/slurm/current/bin", "/cm/shared/apps/slurm/var/etc/slurm/slurm.conf"),
        )
        self.assertEqual(found.account.pattern, "marlowe-[a-z]+[0-9]+")
        self.assertIsNone(found.instructions)
        self.assertTrue(found.test_before_submit)
        self.assertEqual(found.partitions, "partitions-marlowe.json")
        self.assertEqual(
            dict(found.rules),
            {"workstation": "marlowe-workstation.md", "cluster": "marlowe-cluster.md"},
        )
        self.assertEqual(found.skill, "sherlock-kit-marlowe")
        self.assertEqual(list(sites.SITES), ["sherlock", "marlowe"])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            found.control_host = "x"

    def test_the_marlowe_account_rule_takes_base_project_accounts_only(self):
        rule = sites.MARLOWE.account
        for good in ("marlowe-m000000", "marlowe-abc12"):
            self.assertTrue(rule.fullmatch(good), good)
        for bad in (
            "marlowe-users",
            "marlowe-m000000-pm01",
            "marlowe-000000",
            "MARLOWE-m000000",
            "marlowe-m000000 ",
            "m000000",
        ):
            self.assertIsNone(rule.fullmatch(bad), bad)

    def test_the_sherlock_predicate(self):
        here = sites.SHERLOCK.on_cluster
        self.assertTrue(here("sh02-ln04.stanford.edu", {}))
        self.assertTrue(here("anything", {"SLURM_CLUSTER_NAME": "sherlock"}))
        self.assertTrue(here("sh02-12n05", {"SLURM_JOB_ID": "1"}))
        self.assertFalse(here("sh02-12n05", {}))
        self.assertFalse(here("login-01", {"SLURM_CLUSTER_NAME": "slurm", "SLURM_JOB_ID": "1"}))

    def test_the_marlowe_predicate_is_its_slurm_configuration_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            conf = Path(temporary) / "slurm.conf"
            environment = sites.SlurmEnvironment(bin=temporary, conf=str(conf))
            with patch.object(sites, "MARLOWE_SLURM", environment):
                # Hostnames and the cluster name of a job identify nothing on Marlowe.
                for hostname, env in (
                    ("login-01", {}),
                    ("sh02-ln04", {"SLURM_CLUSTER_NAME": "sherlock"}),
                    ("n01", {"SLURM_CLUSTER_NAME": "slurm", "SLURM_JOB_ID": "1"}),
                ):
                    self.assertFalse(sites.MARLOWE.on_cluster(hostname, env))
                conf.mkdir()
                self.assertFalse(sites.MARLOWE.on_cluster("login-01", {}))
                conf.rmdir()
                conf.write_text("ClusterName=slurm\n")
                self.assertTrue(sites.MARLOWE.on_cluster("lab-ubuntu", {}))

    def test_selector_command_and_reply_fields(self):
        self.assertEqual(
            (sites.selector(sites.SHERLOCK), sites.command(sites.SHERLOCK)), ([], "shk")
        )
        self.assertEqual(sites.reply_fields(sites.SHERLOCK), {})
        self.assertEqual(sites.selector(sites.MARLOWE), ["--site", "marlowe"])
        self.assertEqual(sites.command(sites.MARLOWE), "shk --site marlowe")
        self.assertEqual(sites.reply_fields(sites.MARLOWE), {"site": "marlowe"})

    def test_the_marlowe_table_has_the_schema_of_the_sherlock_table(self):
        tables = {
            name: json.loads((DATA / found.partitions).read_text(encoding="utf-8"))
            for name, found in sites.SITES.items()
        }
        keys = {key for profile in tables["sherlock"]["partitions"].values() for key in profile}
        self.assertEqual(tables["marlowe"]["schema_version"], tables["sherlock"]["schema_version"])
        self.assertEqual(list(tables["marlowe"]["partitions"]), ["preempt"])
        preempt = tables["marlowe"]["partitions"]["preempt"]
        self.assertEqual(set(preempt), keys)
        self.assertEqual(
            {key: value for key, value in preempt.items() if key != "courtesy"},
            {
                "preemptible": True,
                "requeue": True,
                "borrowed": False,
                "gpus_allowed": True,
                "batch": True,
            },
        )
        self.assertNotRegex(preempt["courtesy"], r"[0-9]", "package data holds no numbers")


if __name__ == "__main__":
    unittest.main()
