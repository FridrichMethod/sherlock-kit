"""Offline submit checks: grammar, job name, #SBATCH denylist, profile gates, sbatch argv, frozen spec."""
from argparse import Namespace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fakes import SRC

sys.path.insert(0, str(SRC))
from sherlock_kit import config, spec  # noqa: E402
from sherlock_kit.errors import ShkError  # noqa: E402

NOW = datetime(2026, 10, 10, 12, 0, 5, tzinfo=timezone.utc)
SCRIPT = b"#!/bin/bash -l\n#SBATCH --mail-type=END\necho hi\n"
NAME = "shk.demo.train-a." + hashlib.sha256(b'["demo","default","train-a"]').hexdigest()[:10]
RUN = "/scratch/u/demo/runs/" + NAME
LOGS = [f"--chdir={RUN}", f"--output={RUN}/slurm-%j.out", f"--error={RUN}/slurm-%j.err"]
HEAD = ["sbatch", "--parsable", f"--job-name={NAME}"]
TAIL = ["--ntasks=1", "--mem=4096M", "--time=90"]
EXPORT = f"--export=ALL,SHK_NAME={NAME}"


class Case(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.cfg = config.parse({"project": "demo", "remote_root": "/scratch/u/demo/"}, self.tmp / "sherlock-kit.toml")
        (self.tmp / "job.sh").write_bytes(SCRIPT)

    def build(self, body=None, **overrides):
        if body is not None:
            (self.tmp / "job.sh").write_bytes(body)
        args = dict(task="train-a", campaign="default", script=str(self.tmp / "job.sh"), partition="normal", cpus=2,
                    mem="4G", time="90m", gpus=0, tasks=1, array=None, constraint=None, signal=None, requeue=None,
                    after_ok=[], run_dir=None, courtesy_ack=False, resubmit=None, dry_run=False)
        return spec.build(Namespace(**{**args, **overrides}), self.cfg, NOW)

    def refused(self, code, token, **overrides):
        with self.assertRaises(ShkError) as caught:
            self.build(**overrides)
        self.assertEqual((caught.exception.code, caught.exception.token), (code, token), caught.exception.message)
        return caught.exception


class GrammarTests(Case):
    def test_memory_time_and_array(self):
        self.assertEqual([spec.memory_mb(t) for t in ("16G", "16000M", "1g", "512m")], [16384, 16000, 1024, 512])
        self.assertEqual([spec.minutes(t) for t in ("2h", "90m", "1:30:01", "0:00:30", "25:00:00")], [120, 90, 91, 1, 1500])
        self.assertEqual(spec.array("100%10"), {"count": 100, "throttle": 10})
        self.assertEqual(spec.array("2"), {"count": 2, "throttle": None})
        self.assertEqual(spec.array("1000%1000"), {"count": 1000, "throttle": 1000})
        for parse, text in [(spec.memory_mb, t) for t in ("16", "0G", "16T", "1.5G", "-1M", "", None)] + \
                [(spec.minutes, t) for t in ("0m", "0:00:00", "1:60:00", "1:00", "2d", "1.5h", "")] + \
                [(spec.array, t) for t in ("1", "1001", "4%0", "4%5", "x", "0-3", "4%", "")]:
            with self.subTest(parse=parse.__name__, text=text), self.assertRaises(ShkError) as caught:
                parse(text)
            self.assertEqual((caught.exception.code, caught.exception.token), (1, "spec_invalid"))

    def test_other_fields_are_checked_offline(self):
        for overrides in ({"cpus": 0}, {"tasks": 0}, {"gpus": -1}, {"constraint": "cpu; false"}, {"constraint": "a||b"},
                          {"signal": "KILL@60"}, {"signal": "B:USR1@60\n"}, {"after_ok": ["0"]}, {"after_ok": ["12;x"]},
                          {"task": ""}, {"task": "a\nb"}, {"task": "x" * 129}, {"campaign": "tab\there"},
                          {"run_dir": "relative/run"}, {"run_dir": "/scratch/../etc"}, {"run_dir": "/scratch/%j"}, {"run_dir": "/"}):
            with self.subTest(overrides=overrides):
                self.refused(1, "spec_invalid", **overrides)
        self.refused(1, "script_unreadable", script=str(self.tmp / "absent.sh"))
        self.assertEqual(self.build(run_dir="/scratch/u/elsewhere/").run_dir, "/scratch/u/elsewhere")
        self.assertIn("1 MiB", self.refused(1, "spec_invalid", body=b"#!/bin/sh\n" + b"#" * spec.SCRIPT_LIMIT).message)

    def test_job_name_is_stable_hashed_and_bounded(self):
        self.assertEqual(spec.job_name("demo", "default", "train-a"), NAME)
        other = spec.job_name("demo", "pilot", "train-a")
        self.assertNotEqual(other, NAME)
        self.assertEqual(other.rsplit(".", 1)[0], NAME.rsplit(".", 1)[0])
        self.assertEqual([spec.slug(t) for t in ("  Hello, World!! ", "!!!", "a" * 30, "abcdefghijklmnopqrs-tail")],
                         ["hello-world", "x", "a" * 20, "abcdefghijklmnopqrs"])
        longest = spec.job_name("p" * 24, "c", "Ünïcode task " * 5)
        self.assertEqual(len(longest), 60)
        for name in (NAME, other, longest):
            self.assertRegex(name, r"\A[a-z0-9.-]+\Z")
            self.assertTrue(spec.NAME.fullmatch(name))
        self.assertFalse(spec.job_name("foo-bar", "default", "t").startswith("shk.foo."))
        self.assertEqual(spec.canonical({"b": 1, "a": [1, 2]}), '{"a":[1,2],"b":1}')


class DirectiveTests(Case):
    DENIED = ("-p normal", "--partition=gpu", "--partition gpu", "-pgpu", "-A grp", "--account=grp", "-x sh01",
              "--exclude=sh01", "-G 1", "--gpus=1", "--gpus-per-node=2", "--gres=gpu:1", "-a 0-3", "--array=0-3",
              "-d afterok:1", "--dependency=singleton", "--begin=now+1hour", "-J other", "--job-name=other",
              "--part=gpu", "-c 2 -p gpu", "-Hp gpu", "'--partition=gpu'")
    ALLOWED = ("#SBATCH --mail-type=END", "#SBATCH -c 2 --mem=8G  # -p gpu is a comment", "  #SBATCH -p gpu",
               "# SBATCH -p gpu", "#SBATCH --gres-flags=enforce-binding", "#SBATCH -o out.log", "#SBATCH --comment='x y'")

    def test_denylisted_directives_in_the_leading_block_are_refused(self):
        for directive in self.DENIED:
            with self.subTest(directive=directive):
                error = self.refused(3, "sbatch_directive", body=f"#!/bin/sh\n\n# note\n#SBATCH {directive}\necho\n".encode())
                self.assertIn("command line", error.hint)

    def test_other_directives_and_lines_sbatch_ignores_are_allowed(self):
        for line in self.ALLOWED:
            with self.subTest(line=line):
                self.assertEqual(self.build(body=f"#!/bin/sh\n{line}\necho\n".encode()).name, NAME)
        self.build(body=b"#!/bin/sh\necho start\n#SBATCH -p gpu\n")
        self.assertEqual(spec.directives(b"#!/bin/sh\n#SBATCH --a='b c' -d\n\n#SBATCH -x\nrun\n#SBATCH -J y\n"),
                         ["--a=b c", "-d", "-x"])
        self.assertEqual(spec.directives(b"#SBATCH --comment='unbalanced\n"), ["--comment='unbalanced"])


class ProfileAndArgvTests(Case):
    def test_profile_gates(self):
        self.refused(3, "unknown_partition", partition="mystery")
        self.refused(3, "gpus_not_allowed", gpus=1)
        self.refused(3, "requeue_not_allowed", requeue=True)
        self.assertIn("--courtesy-ack", self.refused(3, "courtesy_ack_required", partition="btrippe").hint)
        built = self.build(partition="btrippe", courtesy_ack=True, gpus=1)
        self.assertTrue(built.profile.borrowed)

    def test_golden_sbatch_argv(self):
        single = self.build()
        self.assertEqual(single.argv, [*HEAD, "--partition=normal", "--cpus-per-task=2", "--no-requeue", *TAIL, *LOGS,
                                       EXPORT, "--dependency=singleton"])
        arrays = [f"--chdir={RUN}", f"--output={RUN}/slurm-%A_%a.out", f"--error={RUN}/slurm-%A_%a.err"]
        self.assertEqual(self.build(array="4%2").argv, [*HEAD, "--partition=normal", "--array=0-3%2", "--cpus-per-task=2",
                                                        "--no-requeue", *TAIL, *arrays, EXPORT])
        self.assertEqual(self.build(array="3", after_ok=["11"]).argv[-1], "--dependency=afterok:11")
        owners = self.build(partition="owners", gpus=1, constraint="GPU_SKU:H100_SXM5|GPU_SKU:A100", signal="B:USR1@60",
                            after_ok=["11", "12_3"])
        self.assertEqual(owners.argv, [*HEAD, "--partition=owners", "--cpus-per-task=2", "--requeue", "--open-mode=append",
                                       *TAIL, *LOGS, "-G", "1", "--constraint=GPU_SKU:H100_SXM5|GPU_SKU:A100",
                                       "--signal=B:USR1@60", EXPORT, "--dependency=afterok:11:12_3,singleton"])
        self.assertIn("--no-requeue", self.build(partition="owners", requeue=False).argv)
        self.assertNotIn("--dependency=singleton", " ".join(self.build(array="2", partition="owners").argv))

    def test_frozen_spec_document(self):
        built = self.build(resubmit="fixed the input path", array="4%2")
        doc = json.loads(built.text)
        self.assertEqual(built.text, spec.canonical(doc))
        self.assertEqual(set(doc), {"schema", "name", "project", "campaign", "task", "run_dir", "resources", "argv",
                                    "script", "resubmit_reason", "shk", "git", "created_utc"})
        self.assertEqual((doc["name"], doc["run_dir"], doc["argv"], doc["created_utc"]), (NAME, RUN, built.argv, "2026-10-10T12:00:05Z"))
        self.assertEqual(doc["script"], {"name": "job.sh", "sha256": hashlib.sha256(SCRIPT).hexdigest()})
        self.assertEqual((built.sha256, built.script), (doc["script"]["sha256"], SCRIPT))
        self.assertEqual(doc["resources"]["array"], {"count": 4, "throttle": 2})
        self.assertEqual((doc["resubmit_reason"], doc["git"]), ("fixed the input path", None))
        self.assertTrue(doc["shk"].startswith("sherlock-kit 0.4.0 ("))

    def test_git_provenance_of_the_project(self):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        git = ["git", "-C", str(self.tmp), "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        subprocess.run([*git, "init", "-q"], check=True, env=env)
        subprocess.run([*git, "add", "job.sh"], check=True, env=env)
        subprocess.run([*git, "commit", "-qm", "x"], check=True, env=env)
        head = subprocess.run([*git, "rev-parse", "HEAD"], check=True, env=env, capture_output=True, text=True).stdout.strip()
        self.assertEqual(json.loads(self.build().text)["git"], {"head": head, "dirty": False})
        (self.tmp / "job.sh").write_bytes(SCRIPT + b"echo changed\n")
        self.assertEqual(json.loads(self.build().text)["git"], {"head": head, "dirty": True})


if __name__ == "__main__":
    unittest.main()
