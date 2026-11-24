"""Packaged site records, the site-env.sh prelude and the Marlowe partition table."""

import dataclasses
import json
import os
import secrets
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fakes import SRC, TRIPWIRE

sys.path.insert(0, str(SRC))
from sherlock_kit import executor, rates, sites

DATA = SRC / "sherlock_kit" / "data"
PRELUDE = "PATH=$PATH:$1\nSLURM_CONF=$2\nexport PATH SLURM_CONF\nshift 2\n"
# Arguments a shell would mangle if the prelude quoted, split, globbed or shifted them wrongly.
ARGV = ["", "a b", "*", "$(id)", "'q'", 'd"q', "new\nline", "-x", "--", "\\"]


class Recorder:
    """An executor that records the argv it is asked to run."""

    kind = "ssh"

    def __init__(self, site):
        self.site = site
        self.argv = None

    def run(self, argv, **options):
        self.argv = argv
        return executor.Result("complete")


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
        self.assertIsNone(found.rates)
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
        self.assertEqual(found.rates, "rates-marlowe.json")
        self.assertEqual(rates.load(found.rates).currency, "USD")
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


class PreludeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.bin = self.tmp / "site-bin"
        self.bin.mkdir()
        self.conf = self.tmp / "site.conf"
        self.site = dataclasses.replace(
            sites.MARLOWE, slurm=sites.SlurmEnvironment(bin=str(self.bin), conf=str(self.conf))
        )

    def tool(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def test_the_prelude_is_fixed_text(self):
        text = executor.program_text("site-env")
        self.assertEqual(
            "".join(line + "\n" for line in text.splitlines() if not line.startswith("#")),
            PRELUDE,
        )

    def test_argv_per_site(self):
        sherlock, marlowe = Recorder(sites.SHERLOCK), Recorder(sites.MARLOWE)
        self.assertEqual(executor.run_program(sherlock, "query", ["a", "b"]).site, sites.SHERLOCK)
        self.assertEqual(
            sherlock.argv, ["sh", "-c", executor.program_text("query"), "shk-query", "a", "b"]
        )
        self.assertEqual(executor.run_program(marlowe, "query", ["a", "b"]).site, sites.MARLOWE)
        self.assertEqual(
            marlowe.argv,
            [
                "sh",
                "-c",
                executor.program_text("site-env") + executor.program_text("query"),
                "shk-query",
                "/cm/shared/apps/slurm/current/bin",
                "/cm/shared/apps/slurm/var/etc/slurm/slurm.conf",
                "a",
                "b",
            ],
        )

    def shells(self):
        """("sh", {}) and, with bash installed, bash as sh (Sherlock's sh) first on PATH."""
        yield "sh", {}
        bash = shutil.which("bash")
        if bash:
            shim = self.tmp / "bash-as-sh"
            shim.mkdir(exist_ok=True)
            if not (shim / "sh").exists():
                (shim / "sh").symlink_to(bash)
            yield "bash", {"PATH": f"{shim}{os.pathsep}{os.environ['PATH']}"}

    def test_later_arguments_reach_a_real_program_unchanged(self):
        # preflight.sh runs "$@" --test-only: a uniquely named stub that only the appended
        # directory holds receives the arguments after BIN and CONF, and SLURM_CONF.
        stub = f"shk-prelude-probe-{secrets.token_hex(4)}"
        self.assertIsNone(shutil.which(stub))
        self.tool(stub, 'printf "%s\\0" "$SLURM_CONF" "$@"')
        for shell, env in self.shells():
            with self.subTest(shell=shell), patch.dict(os.environ, env):
                ex = executor.LocalExecutor(command_timeout=30, site=self.site)
                result = executor.run_program(ex, "preflight", [stub, *ARGV], stdin=b"#!/bin/sh\n")
                lines = executor.records(result.stdout)
                self.assertEqual(lines[-2:], ["SHK_RC sbatch 0", "SHK_END"], result)
                encoded = "".join(
                    line.removeprefix("SHK_OUTHEX|")
                    for line in lines
                    if line.startswith("SHK_OUTHEX|")
                )
                words = bytes.fromhex(encoded).decode().split("\0")
                self.assertEqual(words, [str(self.conf), *ARGV, "--test-only", ""])

    def test_the_appended_directory_never_shadows_an_earlier_path_entry(self):
        # The suite's tripwire is first on PATH; clients in the site directory come after it.
        for name in ("sbatch", "squeue", "sacct"):
            self.tool(name, f'echo "{name} $*" >>"{self.tmp}/calls"')
        ex = executor.LocalExecutor(command_timeout=30, site=self.site)
        lines = executor.records(executor.run_program(ex, "doctor").stdout)
        tools = dict(line.split(" ")[1:3] for line in lines if line.startswith("SHK_TOOL "))
        for name in ("sbatch", "squeue", "sacct"):
            self.assertEqual(tools[name], str(TRIPWIRE / name))
        self.assertFalse((self.tmp / "calls").exists())

    def test_the_packaged_marlowe_environment_runs_where_its_paths_are_absent(self):
        ex = executor.LocalExecutor(command_timeout=30, site=sites.MARLOWE)
        result = executor.run_program(ex, "manifest", [str(self.tmp / "absent dir")])
        self.assertEqual(
            (result.status, result.stdout), ("complete", "SHK_ERROR no_directory\nSHK_END\n")
        )


if __name__ == "__main__":
    unittest.main()
