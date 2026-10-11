"""Transfer: SHA256SUMS manifests, push and pull with real rsync through fake_ssh exec mode."""
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fakes import SRC, install_fake_ssh

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, executor, transfer  # noqa: E402
from sherlock_kit.errors import ShkError  # noqa: E402

H, G = "ab" * 32, "cd" * 32
ONE_LINER = 'cd "$1" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS'
VALIDATORS = {"ok": "import pathlib, sys; sys.exit(0 if (pathlib.Path(sys.argv[1]) / 'result.json').is_file() else 3)",
              "bad": "import sys; print('science says no', file=sys.stderr); sys.exit(3)",
              "mutate": "import pathlib, sys; (pathlib.Path(sys.argv[1]) / 'extra').write_text('x')"}
DTN_CHECK = ["-O", "check", "-oBatchMode=yes", "sherlock-dtn"]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class ManifestParseTests(unittest.TestCase):
    def test_sha256sum_forms(self):
        self.assertEqual(transfer.parse_manifest(f"{H}  ./a/b c\n{G.upper()} *z\n\n"), {"a/b c": H, "z": G})

    def test_refusals(self):
        bad = ["", f"\\{H}  a\\nb\n", f"{H}  /abs\n", f"{H}  ../x\n", f"{H}  a/../b\n", f"{H}  a//b\n", f"{H}  a/\n",
               f"{H}  ./.\n", f"{H}  .shk-lock\n", f"{H}  d/.shk-stage/x\n", f"{H}  SHA256SUMS\n", f"{H}  x\ry\n",
               f"{H}  x\\y\n", f"{H} a\n", f"{H[:-1]}  a\n", "garbage\n", f"{H}  a\n{G}  ./a\n", f"{H}  a\n{H}  a/b\n",
               "".join(f"{H}  f{i}\n" for i in range(10001)), f"{H}  {'x' * (4 << 20)}\n"]
        for text in bad:
            with self.subTest(text=text[:80]), self.assertRaises(ShkError) as caught:
                transfer.parse_manifest(text)
            self.assertEqual((caught.exception.code, caught.exception.token), (1, "manifest_invalid"))

    def test_stderr_tail_is_filtered_redacted_and_bounded(self):
        secret = "/p/.run1.shk-files-x1"
        raw = f"{executor.BANNER} first.\nrsync: failed to open files-from file {secret}: \x1b[31mgone\x00\n".encode()
        self.assertEqual(transfer._tail(raw, (secret,)), "rsync: failed to open files-from file <files-from>: [31mgone")
        self.assertEqual(transfer._tail(b"x" * 1000 + b"\x7f tail\n"), "x" * 495 + " tail")


class TransferCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.tmp = Path(temporary.name).resolve()
        self.fake = install_fake_ssh(self.tmp)
        environment = patch.dict(os.environ, self.fake.env())
        environment.start()
        self.addCleanup(environment.stop)
        self.root, self.project = self.tmp / "scratch", self.tmp / "project"
        self.remote = self.root / "run 1"
        self.write(self.remote, {"result.json": '{"value": 42}\n', "nested/log.txt": "evidence\n", "with space": "s\n"})
        self.sums()
        lines = [f"remote_root = {json.dumps(str(self.root))}", "[validators]"]
        lines += [f"{name} = {json.dumps([sys.executable, '-c', code])}" for name, code in VALIDATORS.items()]
        self.write(self.project, {"sherlock-kit.toml": "\n".join(lines) + "\n", "results/.keep": ""})
        self.dest = self.project / "results" / "run1"
        self.stage = self.dest.parent / ".run1.shk-stage"

    def write(self, root: Path, files: dict) -> None:
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)

    def sums(self) -> None:
        subprocess.run(["sh", "-c", ONE_LINER, "sh", str(self.remote)], check=True)

    def shk(self, *argv, hostname="lab-ubuntu", executor="ssh"):
        context = cli.Context(project_dir=self.project, executor=executor, env={}, hostname=hostname)
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv), modules=[transfer], context=context)
        return code, json.loads(out.getvalue())

    def pull(self, *extra):
        return self.shk("pull", str(self.remote), str(self.dest), *extra)

    def assert_error(self, result, code, token):
        self.assertEqual((result[0], result[1].get("error")), (code, token), result[1])

    def assert_refused(self, result, code, token):
        self.assert_error(result, code, token)
        self.assertFalse(self.dest.exists())


class ManifestCommandTests(TransferCase):
    def test_round_trip_matches_sha256sum_and_is_deterministic(self):
        bundle = self.tmp / "bundle"
        self.write(bundle, {"b.txt": "b\n", "a dir/x": "x\n", ".shk-lock": "", ".shk-stage/y": "y", "sub/SHA256SUMS": ""})
        (bundle / "link").symlink_to("b.txt")
        code, body = self.shk("manifest", str(bundle), hostname="sh02-ln04", executor="auto")
        self.assertEqual((code, body["files"], body["skipped"]), (0, 2, ["link"]), body)
        text = (bundle / "SHA256SUMS").read_text()
        self.assertEqual(text, f"{sha('x' + chr(10))}  a dir/x\n{sha('b' + chr(10))}  b.txt\n")
        subprocess.run(["sha256sum", "--check", "--quiet", "SHA256SUMS"], cwd=bundle, check=True)
        self.assertEqual(self.shk("manifest", str(bundle))[0], 0)
        self.assertEqual((bundle / "SHA256SUMS").read_text(), text)
        (bundle / "bad\\name").write_text("x")
        self.assertEqual(self.shk("manifest", str(bundle))[1]["error"], "manifest_invalid")
        self.assertEqual((bundle / "SHA256SUMS").read_text(), text)
        self.assertEqual(self.fake.calls(), [])


class PullTests(TransferCase):
    def test_exact_inventory_receipt_and_idempotent_rerun(self):
        code, body = self.pull()
        self.assertEqual((code, body["action"], body["files"], body["bytes"]), (0, "pulled", 3, 25), body)
        expected = tree(self.remote)
        del expected["SHA256SUMS"]
        self.assertEqual(tree(self.dest), expected)
        receipt = json.loads((self.dest.parent / ".run1.shk-receipt.json").read_text())
        digest = hashlib.sha256((self.remote / "SHA256SUMS").read_bytes()).hexdigest()
        self.assertEqual((receipt["manifest_sha256"], receipt["remote"], receipt["files"]), (digest, str(self.remote), 3))
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()),
                         [".keep", ".run1.shk-lock", ".run1.shk-receipt.json", "run1"])
        self.assertEqual(self.shk("pull", str(self.remote), str(self.dest))[1]["action"], "already_present")
        self.assertEqual(self.fake.calls()[0], DTN_CHECK)

    def test_extra_or_missing_remote_files_are_refused(self):
        (self.remote / "extra").write_text("x")
        self.assert_refused(self.pull(), 1, "manifest_incomplete")
        (self.remote / "nested" / "log.txt").unlink()
        result = self.pull()
        self.assert_refused(result, 1, "transfer_failed")
        self.assertIn("No such file", result[1]["message"])
        self.assertNotIn("Automated tools", result[1]["message"])

    def test_hash_mismatch_never_promotes_and_rerun_resumes(self):
        (self.remote / "result.json").write_text('{"value": 13}\n')
        self.assert_refused(self.pull(), 1, "verify_failed")
        self.assertEqual(self.stage.stat().st_mode & 0o777, 0o700)
        (self.remote / "result.json").write_text('{"value": 42}\n')
        self.assertEqual(self.pull()[0], 0)
        self.assertFalse(self.stage.exists())

    def test_stale_stage_entries_are_refused(self):
        self.write(self.stage, {"stale": "x"})
        self.stage.chmod(0o700)
        result = self.pull()
        self.assert_refused(result, 1, "verify_failed")
        self.assertIn("rm -r", result[1]["hint"])
        shutil.rmtree(self.stage)
        self.stage.mkdir()
        self.stage.chmod(0o755)
        self.assert_refused(self.pull(), 1, "stage_invalid")

    def test_validator_gates_promotion(self):
        self.assert_refused(self.pull("--validate", "nope"), 1, "validator_unknown")
        self.assertEqual(self.fake.calls(), [])
        result = self.pull("--validate", "bad")
        self.assert_refused(result, 1, "validator_failed")
        self.assertIn("science says no", result[1]["message"])
        self.assert_refused(self.pull("--validate", "mutate"), 1, "verify_failed")
        shutil.rmtree(self.stage)
        self.assertEqual(self.pull("--validate", "ok")[0], 0)
        self.assertEqual(json.loads((self.dest.parent / ".run1.shk-receipt.json").read_text())["validator"], "ok")

    def test_manifest_changed_mid_transfer_never_promotes(self):
        sums, original = self.remote / "SHA256SUMS", transfer._rsync

        def rsync_then_rewrite(*args, **kwargs):
            original(*args, **kwargs)
            sums.write_text(sums.read_text().replace("  ", " *"))
        with patch.object(transfer, "_rsync", rsync_then_rewrite):
            self.assert_refused(self.pull(), 1, "manifest_changed")
        self.assertEqual(self.pull()[0], 0)

    def test_replace_keeps_the_old_copy_until_the_swap(self):
        self.assertEqual(self.pull()[0], 0)
        (self.remote / "result.json").write_text("new\n")
        self.sums()
        self.assert_error(self.pull(), 3, "destination_exists")
        real = os.rename

        def full_disk(source, target):
            if Path(source).name == self.stage.name:
                raise OSError(28, "No space left on device")
            return real(source, target)
        with patch.object(os, "rename", full_disk):
            self.assert_error(self.pull("--replace"), 1, "promote_failed")
        self.assertEqual((self.dest / "result.json").read_text(), '{"value": 42}\n')
        self.assertEqual(sorted(self.dest.parent.glob(".run1.shk-old-*")), [])
        code, body = self.pull("--replace")
        self.assertEqual((code, (self.dest / "result.json").read_text()), (0, "new\n"))
        self.assertEqual((Path(body["replaced"]) / "result.json").read_text(), '{"value": 42}\n')

    def test_concurrent_pulls_promote_once(self):
        args, actions, original = SimpleNamespace(remote=str(self.remote), local=str(self.dest), validate=None,
                                                  replace=False), [], transfer._rsync

        def slow(*a, **k):
            time.sleep(0.3)
            original(*a, **k)

        def worker():
            context = cli.Context(project_dir=self.project, executor="ssh", env={}, hostname="lab-ubuntu")
            actions.append(transfer.pull(args, context).payload["action"])
        with patch.object(transfer, "_rsync", slow):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(60)
        self.assertEqual(sorted(actions), ["already_present", "pulled"])

    def test_rsync_failure_is_redacted_and_cleans_its_file_list(self):
        real = executor._spawn

        def failing(argv, data, timeout):
            if argv[0] != "rsync":
                return real(argv, data, timeout)
            if timeout == 7:
                raise subprocess.TimeoutExpired(argv, timeout)
            listed = next(a.split("=", 1)[1] for a in argv if a.startswith("--files-from="))
            return 12, b"", f"{executor.BANNER}.\nrsync: cannot read {listed}\n".encode()
        with patch.object(executor, "_spawn", failing):
            result = self.pull()
            self.assert_refused(result, 4, "transfer_failed")
            self.assertEqual(result[1]["message"], "rsync exit 12: rsync: cannot read <files-from>")
            config = self.project / "sherlock-kit.toml"
            config.write_text(config.read_text().replace("[validators]", "transfer_timeout = 7\n[validators]"))
            result = self.pull()
        self.assert_refused(result, 1, "transfer_failed")
        self.assertIn("transfer_timeout (7 s)", result[1]["message"])
        self.assertFalse((self.dest.parent / ".run1.shk-files").exists())


class RefusalAndPushTests(TransferCase):
    def test_refused_on_the_local_executor_and_without_a_master(self):
        argvs = (["pull", str(self.remote), str(self.dest)], ["push", str(self.remote), str(self.root / "copy")])
        for argv in argvs:
            self.assertEqual(self.shk(*argv, hostname="sh02-ln04", executor="auto")[1]["error"], "transfer_on_sherlock")
        self.assertEqual(self.fake.calls(), [])
        os.environ["FAKE_SSH_MASTER"] = "none"
        for argv in argvs:
            self.assert_error(self.shk(*argv), 4, "auth_required")
        self.assertEqual(self.fake.calls(), [DTN_CHECK] * 2)
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), [".keep"])

    def test_push_copies_without_deleting_and_stays_under_remote_root(self):
        source, target = self.tmp / "local", self.root / "pushed"
        self.write(source, {"a b.txt": "1\n", "deep/x": "2\n"})
        (source / "link").symlink_to("a b.txt")
        self.write(target, {"keep": "remote only\n"})
        code, body = self.shk("push", str(source), str(target))
        self.assertEqual((code, body["action"]), (0, "pushed"), body)
        self.assertEqual(tree(target), {"a b.txt": b"1\n", "deep/x": b"2\n", "keep": b"remote only\n", "link": b"1\n"})
        self.assertTrue((target / "link").is_symlink())
        calls = len(self.fake.calls())
        for remote in (self.tmp / "elsewhere", f"{self.root}-evil/x"):
            self.assertEqual(self.shk("push", str(source), str(remote))[1]["error"], "outside_remote_root")
        for remote in ("relative/x", f"{self.root}/../x", "/a\nb"):
            self.assertEqual(self.shk("push", str(source), remote, "--anywhere")[1]["error"], "invalid_remote_path")
        self.assertEqual(len(self.fake.calls()), calls)
        self.assertEqual(self.shk("push", str(source), str(self.tmp / "elsewhere"), "--anywhere")[0], 0)
        self.assertEqual((self.tmp / "elsewhere" / "deep" / "x").read_text(), "2\n")


if __name__ == "__main__":
    unittest.main()
