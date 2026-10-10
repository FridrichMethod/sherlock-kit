"""Opt-in frozen archive/sdist/wheel installation with an isolated build interpreter.

Set SHERLOCK_KIT_BUILD_PYTHON to a temporary venv interpreter with setuptools/wheel.
The test itself needs no network and never installs in the real home environment.
"""
import hashlib
from email.parser import BytesParser
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD_PYTHON = os.environ.get("SHERLOCK_KIT_BUILD_PYTHON")


@unittest.skipUnless(BUILD_PYTHON, "set SHERLOCK_KIT_BUILD_PYTHON to an isolated packaging interpreter")
class FrozenInstallTests(unittest.TestCase):
    def test_verified_archive_and_sdist_install_independently(self):
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        policy = subprocess.check_output(["git", "show", f"{revision}:SHERLOCK.md"], cwd=ROOT)
        license_text = subprocess.check_output(["git", "show", f"{revision}:LICENSE"], cwd=ROOT)
        partitions = subprocess.check_output(["git", "show", f"{revision}:src/sherlock_kit_data/partitions.json"], cwd=ROOT)
        expected = {"schema_version": 1, "code_revision": revision,
                    "policy_sha256": hashlib.sha256(policy).hexdigest(), "install_mode": "frozen",
                    "partitions_sha256": hashlib.sha256(partitions).hexdigest()}
        projection_command = "import sherlock_kit as k; import json; print(json.dumps({'identity': k.policy_identity(), 'policy': k.policy_text(), 'projection': k.policy_projection()}))"
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            archive = temp / "source.zip"
            subprocess.run(["git", "archive", "--format=zip", f"--output={archive}", revision], cwd=ROOT, check=True)
            source = temp / "source"
            # Archive metadata must not accidentally inherit an unrelated outer HEAD.
            subprocess.run(["git", "init", "--quiet", str(temp)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(temp), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "--quiet", "-m", "unrelated outer checkout"], check=True, capture_output=True)
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(source)
            env = os.environ.copy()
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONHOME", None)
            env["PIP_NO_INDEX"] = "1"
            env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            env["PIP_NO_CACHE_DIR"] = "1"
            # Every interpreter and CLI runs with a synthetic home and state root.
            home = temp / "home"
            home.mkdir()
            env["HOME"] = str(home)
            env["SHERLOCK_KIT_STATE_ROOT"] = str(temp / "state")
            env["SHERLOCK_KIT_BUILD_REVISION"] = revision
            wheels = temp / "wheels"
            subprocess.run([BUILD_PYTHON, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                            "--wheel-dir", str(wheels), str(source)], cwd=temp, env=env,
                           check=True, capture_output=True, text=True, timeout=120)
            wheel, = wheels.glob("*.whl")
            distributions = temp / "distributions"
            distributions.mkdir()
            subprocess.run([BUILD_PYTHON, "-c",
                            "import setuptools.build_meta as b, sys; b.build_sdist(sys.argv[1])",
                            str(distributions)], cwd=source, env=env,
                           check=True, capture_output=True, text=True, timeout=120)
            distribution, = distributions.glob("*.tar.gz")
            unpacked = temp / "sdist"
            with tarfile.open(distribution) as bundle:
                bundle.extractall(unpacked, filter="data")
            sdist_source, = unpacked.iterdir()
            self.assertEqual((sdist_source / "LICENSE").read_bytes(), license_text)
            self.assertEqual((sdist_source / "SHERLOCK.md").read_bytes(), policy)
            self.assertEqual((sdist_source / "src/sherlock_kit_data/partitions.json").read_bytes(), partitions)
            self.assertEqual(json.loads((sdist_source / "src/sherlock_kit_data/build_identity.json").read_text()), expected)
            self.assertTrue((sdist_source / "src/sherlock_kit_data/adapters/codex/skills/sherlock-kit-operate/SKILL.md").is_file())
            # Frozen sdist metadata alone must survive an unrelated surrounding Git
            # checkout; no external revision override is supplied to this rebuild.
            env.pop("SHERLOCK_KIT_BUILD_REVISION")
            sdist_wheels = temp / "sdist-wheels"
            subprocess.run([BUILD_PYTHON, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
                            "--wheel-dir", str(sdist_wheels), str(sdist_source)], cwd=temp, env=env,
                           check=True, capture_output=True, text=True, timeout=120)
            sdist_wheel, = sdist_wheels.glob("*.whl")
            with zipfile.ZipFile(wheel) as direct, zipfile.ZipFile(sdist_wheel) as rebuilt:
                direct_files = {name: direct.read(name) for name in direct.namelist() if not name.endswith("/RECORD")}
                rebuilt_files = {name: rebuilt.read(name) for name in rebuilt.namelist() if not name.endswith("/RECORD")}
                self.assertEqual(direct_files, rebuilt_files)
                self.assertEqual(direct_files["sherlock_kit_data/partitions.json"], partitions)
                self.assertEqual(json.loads(direct_files["sherlock_kit_data/build_identity.json"]), expected)
                metadata_path, = [name for name in rebuilt_files if name.endswith(".dist-info/METADATA")]
                metadata = BytesParser().parsebytes(rebuilt_files[metadata_path])
                self.assertEqual(metadata["License-Expression"], "MIT")
                self.assertEqual(metadata.get_all("License-File"), ["LICENSE"])
                license_path, = [name for name in rebuilt_files if name.endswith(".dist-info/licenses/LICENSE")]
                self.assertEqual(rebuilt_files[license_path], license_text)
            installed = temp / "installed"
            subprocess.run([BUILD_PYTHON, "-m", "venv", str(installed)], env=env, check=True, capture_output=True, timeout=60)
            python = installed / "bin/python"
            subprocess.run([str(python), "-m", "pip", "install", "--no-index", str(sdist_wheel)],
                           cwd=temp, env=env, check=True, capture_output=True, timeout=60)
            def home_inventory():
                return {str(path.relative_to(home)): (path.stat().st_mode, path.stat().st_size, path.stat().st_mtime_ns)
                        for path in home.rglob("*")}
            home_before_runtime = home_inventory()
            output = subprocess.check_output([str(python), "-c", projection_command], cwd=temp, env=env, text=True)
            result = json.loads(output)
            self.assertEqual(result["identity"], expected)
            self.assertEqual(result["policy"].encode(), policy)
            self.assertIn(expected["policy_sha256"], result["projection"])
            packaged = subprocess.check_output([str(python), "-c", "import importlib.resources as r; import sherlock_orchestration, sherlock_artifacts, sherlock_commands, sherlock_guard, sherlock_partitions; print((r.files('sherlock_kit_data') / 'adapters/claude/.claude-plugin/plugin.json').read_text()); print((r.files('sherlock_kit_data') / 'adapters/codex/skills/sherlock-kit-operate/SKILL.md').is_file()); print('partitions=' + sherlock_partitions.partitions_sha256()); print(sorted(sherlock_partitions.partition_profiles()))"], cwd=temp, env=env, text=True)
            self.assertIn('"name": "sherlock-kit"', packaged)
            self.assertIn('True', packaged)
            self.assertIn("partitions=" + expected["partitions_sha256"], packaged)
            self.assertIn("['bigmem', 'bioe', 'btrippe', 'dev', 'gpu', 'normal', 'owners', 'possu', 'service', 'stat']", packaged)
            guard = subprocess.run([str(installed / "bin/shk"), "guard", "--client", "claude"], input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "watch -n 1 ssh sherlock-plain squeue"}}), cwd=temp, env=env, capture_output=True, text=True, check=True, timeout=10)
            self.assertEqual(json.loads(guard.stdout)["hookSpecificOutput"]["permissionDecision"], "deny")
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
            self.assertEqual(report["agent_integration"]["guard_runtime"], "available")
            self.assertEqual(report["agent_integration"]["adapter_bundle"], "available")
            self.assertEqual(report["agent_integration"]["enforcement"], "unverified")
            installed_license = subprocess.check_output([str(python), "-c",
                "import importlib.metadata as m, json; d=m.distribution('sherlock-kit'); "
                "print(json.dumps({'expression': d.metadata['License-Expression'], "
                "'files': d.metadata.get_all('License-File'), 'license': d.read_text('licenses/LICENSE')}))"],
                cwd=temp, env=env, text=True, timeout=10)
            self.assertEqual(json.loads(installed_license), {
                "expression": "MIT", "files": ["LICENSE"], "license": license_text.decode()})
            self.assertFalse((temp / "state").exists())
            self.assertEqual(home_inventory(), home_before_runtime)


if __name__ == "__main__":
    unittest.main()
