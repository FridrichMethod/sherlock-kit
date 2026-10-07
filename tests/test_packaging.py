"""Opt-in frozen archive/wheel installation, using an isolated build interpreter.

Set SHERLOCK_KIT_BUILD_PYTHON to a temporary venv interpreter with setuptools/wheel.
The test itself needs no network and never installs in the real home environment.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = os.environ.get("SHERLOCK_KIT_BUILD_PYTHON")


@unittest.skipUnless(BUILD_PYTHON, "set SHERLOCK_KIT_BUILD_PYTHON to an isolated packaging interpreter")
class FrozenInstallTests(unittest.TestCase):
    def test_verified_archive_build_installs_independently(self):
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        policy = subprocess.check_output(["git", "show", f"{revision}:SHERLOCK.md"], cwd=ROOT)
        projection_command = "import sherlock_kit as k; import json; print(json.dumps({'identity': k.policy_identity(), 'policy': k.policy_text(), 'projection': k.policy_projection()}))"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            archive = temp / "source.zip"
            subprocess.run(["git", "archive", "--format=zip", f"--output={archive}", revision], cwd=ROOT, check=True)
            source = temp / "source"
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(source)
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            env["SHERLOCK_KIT_BUILD_REVISION"] = revision
            wheels = temp / "wheels"
            subprocess.run([BUILD_PYTHON, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                            "--wheel-dir", str(wheels), str(source)], cwd=temp, env=env,
                           check=True, capture_output=True, text=True, timeout=120)
            wheel, = wheels.glob("*.whl")
            installed = temp / "installed"
            subprocess.run([BUILD_PYTHON, "-m", "venv", str(installed)], check=True, capture_output=True, timeout=60)
            python = installed / "bin/python"
            subprocess.run([str(python), "-m", "pip", "install", "--no-index", str(wheel)],
                           cwd=temp, env=env, check=True, capture_output=True, timeout=60)
            output = subprocess.check_output([str(python), "-c", projection_command], cwd=temp, env=env, text=True)
            result = json.loads(output)
            expected = {"schema_version": 1, "code_revision": revision,
                        "policy_sha256": hashlib.sha256(policy).hexdigest(), "install_mode": "frozen"}
            self.assertEqual(result["identity"], expected)
            self.assertEqual(result["policy"].encode(), policy)
            self.assertIn(expected["policy_sha256"], result["projection"])
            pin = temp / "pin.json"
            pin.write_text(json.dumps(expected))
            instructions = temp / "instructions.md"
            instructions.write_text(result["projection"])
            doctor = subprocess.run([str(installed / "bin/shk"), "doctor", "--advertised-identity", str(pin),
                                     "--claude-instructions", str(instructions), "--codex-instructions", str(instructions)],
                                    cwd=temp, env=env, capture_output=True, text=True, check=True, timeout=10)
            report = json.loads(doctor.stdout)
            self.assertEqual(report["advertised_identity"], "complete")
            self.assertEqual(report["instructions"], {"claude": "complete", "codex": "complete"})


if __name__ == "__main__":
    unittest.main()
