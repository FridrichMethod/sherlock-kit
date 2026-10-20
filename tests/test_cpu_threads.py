"""Opt-in numerical budgets through real fixed remote shell programs, offline."""

import json
import os
import subprocess
import sys
from pathlib import Path

from fakes import SRC
from test_flows import Flow
from test_spec import Case

sys.path.insert(0, str(SRC))
from sherlock_kit import config, executor
from sherlock_kit.errors import ShkError

NUMERICAL = (
    "OMP_NUM_THREADS",
    "OMP_THREAD_LIMIT",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "BLIS_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "NUMEXPR_MAX_THREADS",
)


class CpuThreadTests(Case):
    def test_cli_budget_is_effective_frozen_env_and_preserves_original_script(self):
        built = self.build(cpu_thread_budget=1)
        policy = built.document["cpu_thread_policy"]
        self.assertEqual(policy["budget_per_task"], 1)
        expected = dict.fromkeys(NUMERICAL, "1")
        expected.update(OMP_MAX_ACTIVE_LEVELS="1", OMP_DYNAMIC="FALSE", MKL_DYNAMIC="FALSE")
        for prefix in ("", "APPTAINERENV_", "SINGULARITYENV_"):
            for name, value in expected.items():
                self.assertEqual(policy["environment"][prefix + name], value)
                self.assertIn(prefix + name + "=" + value, built.argv)
        self.assertEqual(built.argv[0], "env")
        self.assertEqual(built.script, (self.tmp / "job.sh").read_bytes())

    def test_config_budget_enabled_cli_explicit_override_and_off_default_unchanged(self):
        self.cfg = config.parse({
            "project": "demo",
            "remote_root": "/scratch/u/demo",
            "cpu_thread_budget": 1,
        })
        self.assertEqual(self.build().document["cpu_thread_policy"]["budget_per_task"], 1)
        self.assertEqual(
            self.build(cpu_thread_budget=2).document["cpu_thread_policy"]["budget_per_task"], 2
        )
        self.cfg = config.parse({"project": "demo", "remote_root": "/scratch/u/demo"})
        self.assertNotIn("cpu_thread_policy", self.build().document)
        self.assertEqual(self.build().argv[0], "sbatch")

    def test_bad_or_outside_per_task_allocation_budget_refused_offline(self):
        for bad in (0, -1, 3, True, "1", 1.5):
            with self.subTest(bad=bad), self.assertRaises(ShkError):
                self.build(cpu_thread_budget=bad)
        for bad in (0, -1, True, "1", 1.5):
            with self.subTest(config=bad), self.assertRaises(ShkError):
                config.parse({"cpu_thread_budget": bad})

    def test_configured_budget_above_cpus_names_both_sources(self):
        self.cfg = config.parse({
            "project": "demo",
            "remote_root": "/scratch/u/demo",
            "cpu_thread_budget": 3,
        })
        with self.assertRaises(ShkError) as caught:
            self.build()
        self.assertEqual(caught.exception.token, "spec_invalid")
        self.assertIn("cpu_thread_budget", caught.exception.message)
        self.assertIn("--cpu-thread-budget", caught.exception.message)

    def test_real_submit_shell_executes_guard_before_python_payload_and_cleanenv(self):
        """No Slurm runs: real packaged shell, fake sbatch, real Python children."""
        observed = self.tmp / "observed.json"
        script = self.tmp / "job.sh"
        script.write_text(
            f"#!{sys.executable}\n"
            "import json,os,subprocess,sys\n"
            "from pathlib import Path\n"
            "host=dict(os.environ)\n"
            "clean={k[len('APPTAINERENV_'):]:v for k,v in host.items() if k.startswith('APPTAINERENV_')}\n"
            "child=subprocess.run([sys.executable,'-c','import json,os;print(json.dumps(dict(os.environ)))'],env=clean,capture_output=True,text=True,check=True)\n"
            "Path(os.environ['OBSERVED']).write_text(json.dumps({'host':host,'container':json.loads(child.stdout)}))\n"
        )
        script.chmod(0o700)
        built = self.build(cpu_thread_budget=1, run_dir=str(self.tmp / "run"))
        tools = self.tmp / "tools"
        tools.mkdir()
        (tools / "squeue").write_text("#!/bin/sh\nexit 0\n")
        (tools / "sbatch").write_text(
            f"#!{sys.executable}\n"
            "import os,subprocess,sys\n"
            "subprocess.run([sys.executable,sys.argv[-1]],check=True)\nprint('12345')\n"
        )
        for path in tools.iterdir():
            path.chmod(0o700)
        env = {
            **os.environ,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "OBSERVED": str(observed),
        }
        for prefix in ("", "APPTAINERENV_", "SINGULARITYENV_"):
            for name in NUMERICAL:
                env[prefix + name] = "52"
        args = [built.name, built.run_dir, "stamp", built.sha256, built.text, "--", *built.argv]
        result = subprocess.run(
            ["sh", "-c", executor.program_text("submit"), "test-submit", *args],
            input=built.script,
            env=env,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"SHK_SUBMITTED 12345", result.stdout)
        evidence = json.loads(observed.read_bytes())
        for name, value in built.document["cpu_thread_policy"]["environment"].items():
            self.assertEqual(evidence["host"][name], value)
        for name in NUMERICAL:
            self.assertEqual(evidence["container"][name], "1")
        staged = next((Path(built.run_dir) / ".shk").glob("*.sh"))
        self.assertEqual(staged.read_bytes(), built.script)

    def test_real_preflight_shell_accepts_env_prefix_without_staging_payload(self):
        built = self.build(cpu_thread_budget=2)
        tools = self.tmp / "tools"
        tools.mkdir()
        sbatch = tools / "sbatch"
        sbatch.write_text(
            '#!/bin/sh\n[ "$OMP_NUM_THREADS" = 2 ] && [ "$APPTAINERENV_OMP_THREAD_LIMIT" = 2 ] && [ "$SINGULARITYENV_MKL_DYNAMIC" = FALSE ] || exit 9\nprintf "%s\\n" "$@"\ncat >/dev/null\n'
        )
        sbatch.chmod(0o700)
        result = subprocess.run(
            ["sh", "-c", executor.program_text("preflight"), "test-preflight", *built.argv],
            input=built.script,
            env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]},
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"SHK_RC sbatch 0", result.stdout)
        self.assertFalse((self.tmp / "run").exists())


class CpuThreadCliFlows(Flow):
    def test_actual_cli_without_value_defaults_one_and_submits_through_fake_scheduler(self):
        code, result = self.submit("--cpu-thread-budget")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["argv"][0], "env")
        frozen = json.loads(next(self.shk_dir().glob("*.json")).read_bytes())
        self.assertEqual(frozen["cpu_thread_policy"]["budget_per_task"], 1)
        self.assertEqual(
            next(self.shk_dir().glob("*.sh")).read_bytes(), (self.tmp / "job.sh").read_bytes()
        )

    def test_actual_config_opt_in_then_explicit_cli_override_and_allocation_rejection(self):
        self.config(cpu_thread_budget=1)
        code, result = self.submit("--dry-run", "--cpus", "2", "--cpu-thread-budget", "2")
        self.assertEqual(code, 0, result)
        self.assertEqual(result["spec"]["cpu_thread_policy"]["budget_per_task"], 2)
        code, result = self.submit("--dry-run", "--cpu-thread-budget", "2")
        self.assertEqual((code, result["error"]), (1, "spec_invalid"))
        self.assertFalse(self.remote.exists())
