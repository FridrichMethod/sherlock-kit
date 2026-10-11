"""Offline submit checks: grammar, job name, #SBATCH denylist, profile gates, argv, frozen spec."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import config, spec
from sherlock_kit.errors import ShkError

NOW = datetime(2026, 10, 10, 12, 0, 5, tzinfo=UTC)
SCRIPT = b"#!/bin/bash -l\n#SBATCH --mail-type=END\necho hi\n"
NAME = "shk.demo.train-a." + hashlib.sha256(b'["demo","default","train-a"]').hexdigest()[:10]
RUN = "/scratch/u/demo/runs/" + NAME
HEAD = ["sbatch", "--parsable", f"--job-name={NAME}"]
TAIL = ["--ntasks=1", "--mem=4096M", "--time=90", f"--chdir={RUN}"]
SINGLE_LOGS = [f"--output={RUN}/slurm-%j.out", f"--error={RUN}/slurm-%j.err"]
ARRAY_LOGS = [f"--output={RUN}/slurm-%A_%a.out", f"--error={RUN}/slurm-%A_%a.err"]
EXPORT = f"--export=ALL,SHK_NAME={NAME}"
DEFAULTS = {
    "task": "train-a",
    "campaign": "default",
    "partition": "normal",
    "cpus": 2,
    "mem": "4G",
    "time": "90m",
    "gpus": 0,
    "tasks": 1,
    "array": None,
    "constraint": None,
    "signal": None,
    "requeue": None,
    "after_ok": [],
    "run_dir": None,
    "courtesy_ack": False,
    "resubmit": None,
    "dry_run": False,
}


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        table = {"project": "demo", "remote_root": "/scratch/u/demo/"}
        self.cfg = config.parse(table, self.tmp / "sherlock-kit.toml")
        (self.tmp / "job.sh").write_bytes(SCRIPT)

    def build(self, body=None, **overrides):
        if body is not None:
            (self.tmp / "job.sh").write_bytes(body)
        args = Namespace(**{**DEFAULTS, "script": str(self.tmp / "job.sh"), **overrides})
        return spec.build(args, self.cfg, NOW)

    def refused(self, code, token, **overrides):
        with self.assertRaises(ShkError) as caught:
            self.build(**overrides)
        error = caught.exception
        self.assertEqual((error.code, error.token), (code, token), error.message)
        return error


class GrammarTests(Case):
    def test_memory_time_and_array(self):
        memory = [spec.memory_mb(t) for t in ("16G", "16000M", "1g", "512m")]
        self.assertEqual(memory, [16384, 16000, 1024, 512])
        walltimes = [spec.minutes(t) for t in ("2h", "90m", "1:30:01", "0:00:30", "25:00:00")]
        self.assertEqual(walltimes, [120, 90, 91, 1, 1500])
        self.assertEqual(spec.array("100%10"), spec.Array(100, 10))
        self.assertEqual(spec.array("2"), spec.Array(2, None))
        self.assertEqual(spec.array("1000%1000"), spec.Array(1000, 1000))
        bad = [(spec.memory_mb, t) for t in ("16", "0G", "16T", "1.5G", "-1M", "")]
        bad += [(spec.minutes, t) for t in ("0m", "0:00:00", "1:60:00", "1:00", "2d", "1.5h", "")]
        bad += [(spec.array, t) for t in ("1", "1001", "4%0", "4%5", "x", "0-3", "4%", "")]
        for parse, text in bad:
            with (
                self.subTest(parse=parse.__name__, text=text),
                self.assertRaises(ShkError) as caught,
            ):
                parse(text)
            self.assertEqual((caught.exception.code, caught.exception.token), (1, "spec_invalid"))

    def test_other_fields_are_checked_offline(self):
        invalid = (
            {"cpus": 0},
            {"tasks": 0},
            {"gpus": -1},
            {"task": ""},
            {"campaign": ""},
            {"after_ok": ["0"]},
            {"after_ok": ["12;x"]},
            {"after_ok": ["11,12"]},
            {"after_ok": ["11?12"]},
            {"after_ok": ["afterany:11"]},
            {"after_ok": ["11", "12_"]},
            {"run_dir": "relative/run"},
            {"run_dir": "/scratch/../etc"},
            {"run_dir": "/scratch/%j"},
            {"run_dir": "/"},
        )
        for overrides in invalid:
            with self.subTest(overrides=overrides):
                self.refused(1, "spec_invalid", **overrides)
        self.refused(1, "script_unreadable", script=str(self.tmp / "absent.sh"))
        elsewhere = self.build(run_dir="/scratch/u/elsewhere/").run_dir
        self.assertEqual(elsewhere, "/scratch/u/elsewhere")
        huge = b"#!/bin/sh\n" + b"#" * spec.SCRIPT_LIMIT
        self.assertIn("1 MiB", self.refused(1, "spec_invalid", body=huge).message)

    def test_job_name_is_stable_hashed_and_bounded(self):
        self.assertEqual(spec.job_name("demo", "default", "train-a"), NAME)
        other = spec.job_name("demo", "pilot", "train-a")
        self.assertNotEqual(other, NAME)
        self.assertEqual(other.rsplit(".", 1)[0], NAME.rsplit(".", 1)[0])
        tasks = ("  Hello, World!! ", "!!!", "a" * 30, "abcdefghijklmnopqrs-tail")
        slugs = [spec.slug(task) for task in tasks]
        self.assertEqual(slugs, ["hello-world", "x", "a" * 20, "abcdefghijklmnopqrs"])
        longest = spec.job_name("p" * 24, "c", "Ünïcode task " * 5)
        self.assertEqual(len(longest), 60)
        for name in (NAME, other, longest):
            self.assertRegex(name, r"\A[a-z0-9.-]+\Z")
            self.assertTrue(spec.NAME.fullmatch(name))
        self.assertFalse(spec.job_name("foo-bar", "default", "t").startswith("shk.foo."))
        self.assertEqual(spec.canonical({"b": 1, "a": [1, 2]}), '{"a":[1,2],"b":1}')


class DirectiveTests(Case):
    DENIED = (
        "-p normal",
        "--partition=gpu",
        "--partition gpu",
        "-pgpu",
        "-A grp",
        "--account=grp",
        "-x sh01",
        "--exclude=sh01",
        "-G 1",
        "--gpus=1",
        "--gpus-per-node=2",
        "--gres=gpu:1",
        "-a 0-3",
        "--array=0-3",
        "-d afterok:1",
        "--dependency=singleton",
        "--begin=now+1hour",
        "-b now+1hour",
        "-bnow",
        "-Hb now",
        "-J other",
        "--job-name=other",
        "-c 2 -p gpu",
        "'--partition=gpu'",
        # getopt_long takes unambiguous prefixes; value-less short flags bundle before others
        "--part=gpu",
        "--beg=now+1hour",
        "--arr=0-3",
        "--accou=x",
        "--exclud=sh01",
        "--depend=afterok:1",
        "--gre=gpu:1",
        "--job=x",
        "-Hp gpu",
        "-HG 1",
        "-Ha 0-3",
        "-vHx sh01",
        "--comment='unbalanced -p gpu",
        "hetjob",
        "packjob",
    )
    ALLOWED = (
        "#SBATCH --mail-type=END",
        "#SBATCH -c 2 --mem=8G  # -p gpu is a comment",
        "  #SBATCH -p gpu",
        "# SBATCH -p gpu",
        "#SBATCH --gres-flags=enforce-binding",
        "#SBATCH -o out.log",
        "#SBATCH --comment='x y'",
        "#SBATCH --exclusive",
        "#SBATCH -",
        "#SBATCH --",
        "#SBATCH --acctg-freq=task=60",
        "#SBATCH --gpu-bind=closest",
        "#SBATCH -op.log",
        "#SBATCH -Hc 2",
    )

    def test_denylisted_directives_in_the_leading_block_are_refused(self):
        for directive in self.DENIED:
            with self.subTest(directive=directive):
                body = f"#!/bin/sh\n\n# note\n#SBATCH {directive}\necho\n".encode()
                error = self.refused(3, "sbatch_directive", body=body)
                self.assertIn("command line", error.hint)

    def test_other_directives_and_lines_sbatch_ignores_are_allowed(self):
        for line in self.ALLOWED:
            with self.subTest(line=line):
                self.assertEqual(self.build(body=f"#!/bin/sh\n{line}\necho\n".encode()).name, NAME)
        self.build(body=b"#!/bin/sh\necho start\n#SBATCH -p gpu\n")
        script = b"#!/bin/sh\n#SBATCH --a='b c' -d\n\n#SBATCH -x\nrun\n#SBATCH -J y\n"
        self.assertEqual(spec.directives(script), ["--a=b c", "-d", "-x"])
        unbalanced = spec.directives(b"#SBATCH --comment='unbalanced\n")
        self.assertEqual(unbalanced, ["--comment='unbalanced"])


class ProfileAndArgvTests(Case):
    def test_profile_gates(self):
        self.refused(3, "unknown_partition", partition="mystery")
        self.refused(3, "gpus_not_allowed", gpus=1)
        self.refused(3, "requeue_not_allowed", requeue=True)
        borrowed = self.refused(3, "courtesy_ack_required", partition="btrippe")
        self.assertIn("--courtesy-ack", borrowed.hint)
        built = self.build(partition="btrippe", courtesy_ack=True, gpus=1)
        self.assertTrue(built.profile.borrowed)

    def test_golden_sbatch_argv(self):
        partition, cpus = "--partition=normal", "--cpus-per-task=2"
        self.assertEqual(
            self.build().argv,
            [
                *HEAD,
                partition,
                cpus,
                "--no-requeue",
                *TAIL,
                *SINGLE_LOGS,
                EXPORT,
                "--dependency=singleton",
            ],
        )
        self.assertEqual(
            self.build(array="4%2").argv,
            [*HEAD, partition, "--array=0-3%2", cpus, "--no-requeue", *TAIL, *ARRAY_LOGS, EXPORT],
        )
        self.assertEqual(self.build(array="3", after_ok=["11"]).argv[-1], "--dependency=afterok:11")
        owners = self.build(
            partition="owners",
            gpus=1,
            constraint="GPU_SKU:H100_SXM5|GPU_SKU:A100",
            signal="B:USR1@60",
            after_ok=["11", "12_3"],
        )
        self.assertEqual(
            owners.argv,
            [
                *HEAD,
                "--partition=owners",
                cpus,
                "--requeue",
                "--open-mode=append",
                *TAIL,
                *SINGLE_LOGS,
                "-G",
                "1",
                "--constraint=GPU_SKU:H100_SXM5|GPU_SKU:A100",
                "--signal=B:USR1@60",
                EXPORT,
                "--dependency=afterok:11:12_3,singleton",
            ],
        )
        self.assertIn("--no-requeue", self.build(partition="owners", requeue=False).argv)
        owners_array = self.build(array="2", partition="owners").argv
        self.assertFalse(any(option.startswith("--dependency") for option in owners_array))

    def test_frozen_spec_document(self):
        built = self.build(resubmit="fixed the input path", array="4%2")
        doc = json.loads(built.text)
        self.assertEqual(built.text, spec.canonical(doc))
        self.assertEqual(
            set(doc),
            {
                "schema",
                "name",
                "project",
                "campaign",
                "task",
                "run_dir",
                "resources",
                "argv",
                "script",
                "resubmit_reason",
                "shk",
                "git",
                "created_utc",
            },
        )
        self.assertEqual(
            (doc["name"], doc["run_dir"], doc["argv"], doc["created_utc"]),
            (NAME, RUN, built.argv, "2026-10-10T12:00:05Z"),
        )
        digest = hashlib.sha256(SCRIPT).hexdigest()
        self.assertEqual(doc["script"], {"name": "job.sh", "sha256": digest})
        self.assertEqual((built.sha256, built.script), (doc["script"]["sha256"], SCRIPT))
        self.assertEqual(
            doc["resources"],
            {
                "partition": "normal",
                "cpus": 2,
                "tasks": 1,
                "gpus": 0,
                "mem_mb": 4096,
                "minutes": 90,
                "requeue": False,
                "array": {"count": 4, "throttle": 2},
                "constraint": None,
                "signal": None,
                "after_ok": [],
            },
        )
        self.assertEqual((doc["resubmit_reason"], doc["git"]), ("fixed the input path", None))
        self.assertTrue(doc["shk"].startswith("sherlock-kit 0.4.1 ("))

    def test_git_provenance_of_the_project(self):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        git = ["git", "-C", str(self.tmp), "-c", "user.name=t", "-c", "user.email=t@example.test"]
        for argv in (["init", "-q"], ["add", "job.sh"], ["commit", "-qm", "x"]):
            subprocess.run([*git, *argv], check=True, env=env)
        head = subprocess.run(
            [*git, "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(json.loads(self.build().text)["git"], {"head": head, "dirty": False})
        (self.tmp / "job.sh").write_bytes(SCRIPT + b"echo changed\n")
        self.assertEqual(json.loads(self.build().text)["git"], {"head": head, "dirty": True})


if __name__ == "__main__":
    unittest.main()
