"""Transfer: SHA256SUMS manifests, push and pull with real rsync through fake_ssh exec mode."""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from fakes import SRC, install_fake_ssh

sys.path.insert(0, str(SRC))
from sherlock_kit import cli, executor, transfer
from sherlock_kit.errors import ShkError

H, G = "ab" * 32, "cd" * 32
# The documented job-side command (`cd DIR && find ...`), run here with DIR as "$1".
ONE_LINER = (
    "cd \"$1\" && find . -name '.shk*' -prune -o -type f ! -name SHA256SUMS -print0 "
    "| sort -z | xargs -0 sha256sum > SHA256SUMS"
)
VALIDATORS = {
    "ok": "import pathlib, sys; sys.exit(0 if pathlib.Path(sys.argv[1], 'result.json').is_file() else 3)",
    "bad": "import sys; print('science says no', file=sys.stderr); sys.exit(3)",
    "writes": "import pathlib, sys; (pathlib.Path(sys.argv[1]) / 'report.txt').write_text('fine')",
}
CONTROL_CHECK = ["-O", "check", "-oBatchMode=yes", "sherlock-plain"]
DTN_CHECK = ["-O", "check", "-oBatchMode=yes", "sherlock-dtn"]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


class StubControl:
    """A control executor whose manifest.sh reply is fixed text."""

    kind = "ssh"

    def __init__(self, stdout: str):
        self.stdout = stdout

    def run(self, argv, **options):
        return executor.Result("complete", self.stdout, "", 0)


class ManifestParseTests(unittest.TestCase):
    def test_sha256sum_forms(self):
        self.assertEqual(
            transfer.parse_manifest(f"{H}  ./a/b c\n{G.upper()} *z\n\n"), {"a/b c": H, "z": G}
        )

    def test_each_refusal_names_the_broken_rule(self):
        cases = [
            ("", "lists no files"),
            (f"\\{H}  a\\nb\n", "rename the file"),
            (f"{H}  /abs\n", "absolute"),
            (f"{H}  ../x\n", "'..'"),
            (f"{H}  a/../b\n", "'..'"),
            (f"{H}  a//b\n", "'..'"),
            (f"{H}  a/\n", "'..'"),
            (f"{H}  ./.\n", "'..'"),
            (f"{H}  .shk-lock\n", "reserved for shk"),
            (f"{H}  d/.shk-stage/x\n", "reserved for shk"),
            (f"{H}  SHA256SUMS\n", "reserved for the manifest"),
            (f"{H} a\n", "want '<64 hex"),
            (f"{H[:-1]}  a\n", "want '<64 hex"),
            ("garbage\n", "want '<64 hex"),
            (f"{H}  a\n{G}  ./a\n", "listed twice"),
        ]
        for text, reason in cases:
            with self.subTest(text=text[:80]), self.assertRaises(ShkError) as caught:
                transfer.parse_manifest(text)
            self.assertEqual(
                (caught.exception.code, caught.exception.token), (1, "manifest_invalid")
            )
            self.assertIn(reason, caught.exception.message)

    def test_failure_tail_is_banner_free_one_line_and_bounded(self):
        script = (
            f'echo "{executor.BANNER} x" >&2; printf %s {"0" * 1000} >&2; printf "\\n tail\\n" >&2'
        )
        with self.assertRaises(ShkError) as caught:
            transfer._rsync(["sh", "-c", script + "; exit 3"], 10)
        self.assertEqual((caught.exception.code, caught.exception.token), (1, "transfer_failed"))
        self.assertEqual(caught.exception.message, "rsync exit 3: " + "0" * 495 + " tail")

    def test_a_truncated_manifest_reply_is_malformed(self):
        for reply in ("SHK_COUNT 1\nSHK_M|x\n", "SHK_COUNT many\nSHK_END\n", "SHK_END\n"):
            with self.subTest(reply=reply), self.assertRaises(ShkError) as caught:
                transfer._read_manifest(StubControl(reply), "/scratch/run")
            self.assertEqual(
                (caught.exception.code, caught.exception.token), (1, "malformed_reply")
            )
            self.assertIn("doctor --remote", caught.exception.hint)


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
        self.write(
            self.remote,
            {
                "result.json": '{"value": 42}\n',
                "nested/log.txt": "log\n",
                "with space": "s\n",
                ".shk/shk.p.t.0123456789.42.json": "{}\n",
            },
        )  # submit's spec files in a run dir
        self.sums()
        self.write(self.project, {"results/.keep": ""})
        self.configure()
        self.dest = self.project / "results" / "run1"
        self.stage = self.dest.parent / ".run1.shk-stage"

    def write(self, root: Path, files: dict) -> None:
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)

    def configure(self, *settings: str) -> None:
        lines = [f"remote_root = {json.dumps(str(self.root))}", *settings, "[validators]"]
        lines += [
            f"{name} = {json.dumps([sys.executable, '-c', code])}"
            for name, code in VALIDATORS.items()
        ]
        (self.project / "sherlock-kit.toml").write_text("\n".join(lines) + "\n")

    def sums(self, remote: Path | None = None) -> None:
        subprocess.run(["sh", "-c", ONE_LINER, "sh", str(remote or self.remote)], check=True)

    def expected(self) -> dict:
        return {
            name: data
            for name, data in tree(self.remote).items()
            if name != "SHA256SUMS" and not name.startswith(".shk/")
        }

    def context(self, hostname="lab-ubuntu", executor="ssh", clock=None):
        return cli.Context(
            project_dir=self.project, executor=executor, env={}, hostname=hostname, clock=clock
        )

    def shk(self, *argv, **where):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(list(argv), modules=[transfer], context=self.context(**where))
        return code, json.loads(out.getvalue())

    def pull(self, *extra, **where):
        return self.shk("pull", str(self.remote), str(self.dest), *extra, **where)

    def receipt(self) -> dict:
        return json.loads((self.dest.parent / ".run1.shk-receipt.json").read_text())

    def assert_error(self, result, code, token):
        self.assertEqual((result[0], result[1].get("error")), (code, token), result[1])

    def assert_refused(self, result, code, token):
        self.assert_error(result, code, token)
        self.assertFalse(self.dest.exists())


class ManifestCommandTests(TransferCase):
    def test_round_trip_matches_sha256sum_and_is_deterministic(self):
        bundle = self.tmp / "bundle"
        self.write(
            bundle,
            {
                "b.txt": "b\n",
                "a dir/x": "x\n",
                ".shk-lock": "",
                ".shk-x/y": "",
                "sub/SHA256SUMS": "",
            },
        )
        (bundle / "link").symlink_to("b.txt")
        code, body = self.shk("manifest", str(bundle), hostname="sh02-ln04", executor="auto")
        self.assertEqual((code, body["files"], body["skipped"]), (0, 2, ["link"]), body)
        text = (bundle / "SHA256SUMS").read_text()
        self.assertEqual(text, f"{sha('x' + chr(10))}  a dir/x\n{sha('b' + chr(10))}  b.txt\n")
        subprocess.run(["sha256sum", "--check", "--quiet", "SHA256SUMS"], cwd=bundle, check=True)
        self.assertEqual(self.shk("manifest", str(bundle))[0], 0)
        self.assertEqual((bundle / "SHA256SUMS").read_text(), text)
        self.assertEqual(self.fake.calls(), [])


class PullTests(TransferCase):
    def test_exact_inventory_receipt_and_idempotent_rerun(self):
        code, body = self.pull()
        self.assertEqual(
            (code, body["action"], body["files"], body["bytes"]), (0, "pulled", 3, 20), body
        )
        self.assertEqual(tree(self.dest), self.expected())
        digest = hashlib.sha256((self.remote / "SHA256SUMS").read_bytes()).hexdigest()
        self.assertEqual(
            (self.receipt()["manifest_sha256"], self.receipt()["remote"]),
            (digest, str(self.remote)),
        )
        self.assertEqual(
            sorted(p.name for p in self.dest.parent.iterdir()),
            [".keep", ".run1.shk-lock", ".run1.shk-receipt.json", "run1"],
        )
        code, body = self.pull()
        self.assertEqual(
            (code, body["action"], body["validator"]), (0, "already_present", None), body
        )
        checks = [call[-1] for call in self.fake.calls() if "-O" in call]
        self.assertEqual(
            checks, ["sherlock-plain", "sherlock-dtn", "sherlock-plain", "sherlock-plain"]
        )
        dials = [
            call[call.index("-oProxyCommand=false") + 1 :]
            for call in self.fake.calls()
            if "-O" not in call
        ]
        self.assertEqual(
            [(dial[0], dial[1].split()[0]) for dial in dials],  # manifest.sh, rsync, re-read, rerun
            [
                ("sherlock-plain", "sh"),
                ("sherlock-dtn", "rsync"),
                ("sherlock-plain", "sh"),
                ("sherlock-plain", "sh"),
            ],
        )

    def test_an_idempotent_rerun_needs_no_dtn_master(self):
        self.assertEqual(self.pull()[0], 0)
        os.environ.update(FAKE_SSH_MASTER="none", FAKE_SSH_MASTER_HOSTS="sherlock-dtn")
        before = len(self.fake.calls())
        code, body = self.pull()
        self.assertEqual((code, body["action"]), (0, "already_present"), body)
        self.assertNotIn(DTN_CHECK, self.fake.calls()[before:])

    def test_unusable_extra_or_missing_remote_files_are_refused(self):
        absent = self.shk("pull", str(self.root / "absent"), str(self.dest))
        self.assert_refused(absent, 1, "manifest_unavailable")
        self.assertIn("not a directory", absent[1]["message"])
        unlisted = self.shk("pull", str(self.root), str(self.dest))
        self.assert_refused(unlisted, 1, "manifest_unavailable")
        self.assertIn("SHA256SUMS is missing", unlisted[1]["message"])
        self.assertNotEqual(absent[1]["hint"], unlisted[1]["hint"])
        (self.remote / "extra").write_text("x")
        self.assert_refused(self.pull(), 1, "manifest_incomplete")
        (self.remote / "nested" / "log.txt").unlink()
        result = self.pull()
        self.assert_refused(result, 1, "transfer_failed")
        self.assertIn("No such file", result[1]["message"])
        self.assertNotIn("Automated tools", result[1]["message"])

    def test_a_symlinked_manifest_is_read(self):
        sums = self.remote / "SHA256SUMS"
        sums.rename(self.root / "sums-v2.txt")
        sums.symlink_to(self.root / "sums-v2.txt")
        self.assertEqual(self.pull()[1]["action"], "pulled")

    def test_a_hash_mismatch_never_promotes_and_the_stage_stays_private(self):
        (self.remote / "result.json").write_text('{"value": 13}\n')
        result = self.pull()
        self.assert_refused(result, 1, "verify_failed")
        self.assertNotIn("rm -r", result[1]["hint"])
        self.assertEqual(self.stage.stat().st_mode & 0o777, 0o700)
        self.assert_refused(self.pull(), 1, "verify_failed")
        (self.remote / "result.json").write_text('{"value": 42}\n')
        self.assertEqual(self.pull()[0], 0)  # resumes: --checksum refetches only the changed file
        self.assertFalse(self.stage.exists())
        self.assertEqual(tree(self.dest), self.expected())

    def test_leftovers_in_the_stage_are_pruned_before_the_transfer(self):
        self.write(
            self.stage,
            {
                ".result.json.AbC123": "partial",
                "nested/.log.txt.Zx81Qa": "partial",
                "stale": "from an older SHA256SUMS",
                "gone/x": "older",
                "result.json": "outdated\n",
                "with space/inside": "a directory where a listed file belongs",
            },
        )
        code, body = self.pull()
        self.assertEqual((code, body["action"]), (0, "pulled"), body)
        self.assertEqual(tree(self.dest), self.expected())

    def test_a_pull_killed_by_its_timeout_resumes(self):
        (self.remote / "big.bin").write_bytes(os.urandom(4 << 20))
        self.sums()
        self.configure("transfer_timeout = 2")
        with patch.object(
            transfer, "PULL", (*transfer.PULL, "--bwlimit=64")
        ):  # about a minute for big.bin
            result = self.pull()
        self.assert_refused(result, 1, "transfer_failed")
        self.assertEqual(result[1]["message"], "rsync timed out after 2 s")
        self.assertIn("transfer_timeout", result[1]["hint"])
        leftovers = [path.name for path in self.stage.rglob("*")]
        self.assertTrue(
            any(re.fullmatch(r"\.big\.bin\.\w{6}", name) for name in leftovers), leftovers
        )
        self.configure()
        code, body = self.pull()
        self.assertEqual((code, body["action"]), (0, "pulled"), body)
        self.assertEqual(tree(self.dest), self.expected())

    def test_validator_gates_promotion(self):
        self.assert_refused(self.pull("--validate", "nope"), 1, "validator_unknown")
        self.assertEqual(self.fake.calls(), [])
        result = self.pull("--validate", "bad")
        self.assert_refused(result, 1, "validator_failed")
        self.assertIn("science says no", result[1]["message"])
        self.assertEqual(self.pull("--validate", "ok")[0], 0)
        self.assertEqual(self.receipt()["validator"], "ok")

    def test_validators_run_only_from_a_configuration_the_user_owns(self):
        with patch.object(transfer, "_owned_by_me", return_value=False):
            result = self.pull("--validate", "ok")
        self.assert_refused(result, 3, "validator_untrusted")
        self.assertEqual(self.fake.calls(), [])
        self.assertFalse(self.dest.exists())
        self.assertEqual(self.pull("--validate", "ok")[0], 0)

    def test_a_validator_that_writes_into_its_input_still_promotes(self):
        code, body = self.pull("--validate", "writes")
        self.assertEqual((code, body["action"], body["validator"]), (0, "pulled", "writes"), body)

    def test_a_requested_validator_runs_even_when_the_data_is_present(self):
        self.assertEqual(self.pull()[0], 0)
        result = self.pull("--validate", "bad")
        self.assert_error(result, 3, "destination_exists")
        self.assertIn("without validator 'bad'", result[1]["message"])
        self.assert_error(self.pull("--validate", "bad", "--replace"), 1, "validator_failed")
        self.assertEqual(tree(self.dest), self.expected())
        self.assertEqual(list(self.dest.parent.glob(".run1.shk-old-*")), [])
        code, body = self.pull("--validate", "ok", "--replace")
        self.assertEqual((code, body["action"], body["validator"]), (0, "pulled", "ok"), body)
        for extra in ((), ("--validate", "ok")):
            self.assertEqual(self.pull(*extra)[1]["action"], "already_present")

    def test_receipt_records_the_promotion_time(self):
        moments = {"now": datetime(2026, 10, 11, 8, 0, tzinfo=UTC)}
        original = transfer._rsync

        def an_hour_long_rsync(*args, **kwargs):
            original(*args, **kwargs)
            moments["now"] = datetime(2026, 10, 11, 9, 30, tzinfo=UTC)

        with patch.object(transfer, "_rsync", an_hour_long_rsync):
            self.assertEqual(self.pull(clock=lambda: moments["now"])[0], 0)
        self.assertEqual(self.receipt()["created_utc"], "2026-10-11T09:30:00Z")

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
        args = cli.build_parser([transfer]).parse_args(["pull", str(self.remote), str(self.dest)])
        original = transfer._rsync

        def slow(*a, **k):
            time.sleep(0.3)  # hold the lock while the other pull starts
            original(*a, **k)

        with patch.object(transfer, "_rsync", slow), ThreadPoolExecutor(2) as pool:
            actions = list(
                pool.map(lambda _: transfer.pull(args, self.context()).payload["action"], range(2))
            )
        self.assertEqual(sorted(actions), ["already_present", "pulled"])

    def test_rsync_gets_the_list_on_stdin_and_failures_map_to_exit_codes(self):
        real, lists = executor._spawn, []

        def failing(argv, data, timeout):
            if argv[0] != "rsync":
                return real(argv, data, timeout)
            lists.append(set(data.split(b"\0")))
            if timeout == 7:
                raise subprocess.TimeoutExpired(argv, timeout)
            return 12, b"", f"{executor.BANNER}.\nrsync: connection unexpectedly closed\n".encode()

        with patch.object(executor, "_spawn", failing):
            result = self.pull()
            self.assert_refused(result, 4, "transfer_failed")
            self.assertEqual(
                result[1]["message"], "rsync exit 12: rsync: connection unexpectedly closed"
            )
            self.configure("transfer_timeout = 7")
            result = self.pull()
        self.assert_refused(result, 1, "transfer_failed")
        self.assertEqual(result[1]["message"], "rsync timed out after 7 s")
        self.assertEqual(lists, [{b"nested/log.txt", b"result.json", b"with space", b""}] * 2)


class RefusalAndPushTests(TransferCase):
    def test_refused_on_the_local_executor_and_without_a_master(self):
        pull = ["pull", str(self.remote), str(self.dest)]
        push = ["push", str(self.remote), str(self.root / "copy")]
        for argv in (pull, push):
            self.assert_error(
                self.shk(*argv, hostname="sh02-ln04", executor="auto"), 3, "transfer_on_sherlock"
            )
        self.assertEqual(self.fake.calls(), [])
        os.environ["FAKE_SSH_MASTER"] = "none"
        for argv, check in ((pull, CONTROL_CHECK), (push, DTN_CHECK)):
            before = len(self.fake.calls())
            self.assert_error(self.shk(*argv), 4, "auth_required")
            self.assertEqual(self.fake.calls()[before:], [check])
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), [".keep"])

    def test_push_copies_without_deleting_and_stays_under_remote_root(self):
        source, target = self.tmp / "local", self.root / "pushed"
        self.write(source, {"a b.txt": "1\n", "deep/x": "2\n"})
        (source / "link").symlink_to("a b.txt")
        self.write(target, {"keep": "remote only\n"})
        code, body = self.shk("push", str(source), str(target))
        self.assertEqual((code, body["action"]), (0, "pushed"), body)
        self.assertEqual(
            tree(target),
            {"a b.txt": b"1\n", "deep/x": b"2\n", "keep": b"remote only\n", "link": b"1\n"},
        )
        self.assertTrue((target / "link").is_symlink())
        calls = len(self.fake.calls())
        for remote in (self.tmp / "elsewhere", f"{self.root}-evil/x"):
            self.assertEqual(
                self.shk("push", str(source), str(remote))[1]["error"], "outside_remote_root"
            )
        for remote in ("relative/x", f"{self.root}/../x"):
            self.assertEqual(
                self.shk("push", str(source), remote, "--anywhere")[1]["error"],
                "invalid_remote_path",
            )
        self.assertEqual(len(self.fake.calls()), calls)
        self.assertEqual(
            self.shk("push", str(source), str(self.tmp / "elsewhere"), "--anywhere")[0], 0
        )
        self.assertEqual((self.tmp / "elsewhere" / "deep" / "x").read_text(), "2\n")


if __name__ == "__main__":
    unittest.main()
