import copy
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sherlock_artifacts import TransferError, build_manifest, fetch_bundle, rsync_transfer
from sherlock_orchestration import SafetyError, digest

D = 'a' * 64
PRODUCER = dict(attempt='1' * 32, cluster='sherlock', principal='fixture', code_digest=D, input_digest=D, runtime_digest=D, policy_digest=D)


def concurrent_fetch(source, destination, manifest, queue):
    try:
        answer = fetch_bundle(manifest, Path(destination), lambda stage, m: rsync_transfer(source, stage, m), source_manifest=lambda: manifest, validator=lambda root: (root / 'result.json').read_text() == '{"value":42}\n', validator_digest=D)
        queue.put(('ok', answer['recovered']))
    except Exception as exc:
        queue.put(('error', str(exc)))


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        (self.source / 'result.json').write_text('{"value":42}\n')
        (self.source / 'nested').mkdir()
        (self.source / 'nested' / 'log.txt').write_text('immutable evidence\n')
        self.manifest = build_manifest(self.source, PRODUCER)
        self.dest = self.base / 'result'

    def tearDown(self):
        self.temp.cleanup()

    def transfer(self, stage, manifest):
        rsync_transfer(str(self.source), stage, manifest)

    def fetch(self, **kwargs):
        options = dict(source_manifest=lambda: self.manifest, validator=lambda root: (root / 'result.json').read_text() == '{"value":42}\n', validator_digest=D, expected_identity=PRODUCER)
        options.update(kwargs)
        return fetch_bundle(self.manifest, self.dest, self.transfer, **options)

    def test_real_rsync_fetch_exact_inventory_and_receipt(self):
        result = self.fetch()
        self.assertFalse(result['recovered'])
        self.assertEqual(result['receipt']['manifest_sha256'], digest(self.manifest))
        self.assertEqual((self.dest / 'nested' / 'log.txt').read_text(), 'immutable evidence\n')
        self.assertTrue(self.fetch()['recovered'])

    def test_crash_boundaries_recover_without_partial_final(self):
        for point in ('intent_committed', 'transferred', 'verified', 'promoted', 'receipt_committed'):
            with self.subTest(point=point):
                self.dest = self.base / point
                def crash(value):
                    if value == point:
                        raise RuntimeError('simulated process termination')
                with self.assertRaises(RuntimeError):
                    self.fetch(fault=crash)
                self.assertEqual(self.dest.exists(), point in ('promoted', 'receipt_committed'))
                self.fetch()
                self.assertTrue(self.dest.exists())

    def test_promoted_bundle_receipt_recovers_without_remote(self):
        def crash(point):
            if point == 'promoted':
                raise RuntimeError()
        with self.assertRaises(RuntimeError):
            self.fetch(fault=crash)
        self.fetch(source_manifest=lambda: (_ for _ in ()).throw(ConnectionError()))
        self.assertTrue((self.base / '.result.shk-receipt.json').is_file())

    def test_partial_corrupt_and_same_mtime_resume_repairs(self):
        def crash(point):
            if point == 'transferred':
                raise RuntimeError()
        with self.assertRaises(RuntimeError):
            self.fetch(fault=crash)
        stage = self.base / ('.result.shk-stage-' + digest(self.manifest))
        target = stage / 'result.json'
        source_stat = (self.source / 'result.json').stat()
        target.write_text('{"value":13}\n')
        os.utime(target, ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns))
        self.fetch()
        self.assertEqual((self.dest / 'result.json').read_text(), '{"value":42}\n')

    def test_changed_manifest_and_invalid_science_never_promote(self):
        changed = copy.deepcopy(self.manifest)
        changed['producer']['input_digest'] = 'b' * 64
        with self.assertRaisesRegex(SafetyError, 'changed'):
            self.fetch(source_manifest=lambda: changed)
        self.assertFalse(self.dest.exists())
        with self.assertRaisesRegex(SafetyError, 'scientific'):
            self.fetch(validator=lambda _: False)
        self.assertFalse(self.dest.exists())

    def test_mutating_validator_cannot_certify_wrong_bytes(self):
        def validator(root):
            (root / 'result.json').write_text('changed')
            return True
        with self.assertRaisesRegex(SafetyError, 'checksum'):
            self.fetch(validator=validator)
        self.assertFalse(self.dest.exists())

    def test_path_escapes_reserved_names_duplicate_and_limits(self):
        for path in ('../escape', '/absolute', '.', 'nested/../escape', '.shk-receipt', 'nested/.shk-private', 'one\\two'):
            bad = copy.deepcopy(self.manifest)
            bad['files'][0]['path'] = path
            with self.subTest(path=path), self.assertRaises(SafetyError):
                fetch_bundle(bad, self.dest, lambda *_: self.fail('invalid manifest reached transfer'), source_manifest=lambda: bad, validator=lambda _: True, validator_digest=D)
        with self.assertRaises(SafetyError):
            self.fetch(max_bytes=1)
        with self.assertRaises(SafetyError):
            self.fetch(max_items=1)
        with self.assertRaises(SafetyError):
            self.fetch(expected_identity={**PRODUCER, 'attempt': 'other'})

    def test_unexpected_empty_directory_cannot_be_promoted(self):
        stage = self.base / ('.result.shk-stage-' + digest(self.manifest))
        stage.mkdir(mode=0o700)
        (stage / 'unexpected-empty').mkdir()
        with self.assertRaisesRegex(SafetyError, 'directory inventory'):
            self.fetch()
        self.assertFalse(self.dest.exists())

    def test_symlink_source_stage_and_destination_ancestors(self):
        external = self.base / 'external'
        external.write_text('secret')
        (self.source / 'escape').symlink_to(external)
        with self.assertRaises(SafetyError):
            build_manifest(self.source, PRODUCER)
        stage = self.base / ('.result.shk-stage-' + digest(self.manifest))
        stage.mkdir(mode=0o700)
        (stage / 'escape').symlink_to(external)
        with self.assertRaises(SafetyError):
            self.fetch()
        linked_parent = self.base / 'linked'
        linked_parent.symlink_to(self.base, target_is_directory=True)
        self.dest = linked_parent / 'output'
        with self.assertRaises(SafetyError):
            self.fetch()

    def test_stale_extra_stage_and_conflicting_existing_final_preserved(self):
        stage = self.base / ('.result.shk-stage-' + digest(self.manifest))
        stage.mkdir(mode=0o700)
        (stage / 'unexpected').write_text('unrelated')
        with self.assertRaisesRegex(SafetyError, 'inventory'):
            self.fetch()
        self.dest.mkdir()
        (self.dest / 'unrelated').write_text('preserve')
        with self.assertRaises(SafetyError):
            self.fetch()
        self.assertEqual((self.dest / 'unrelated').read_text(), 'preserve')

    def test_disk_full_and_transfer_failure_preserve_state(self):
        with patch('sherlock_artifacts.shutil.disk_usage', return_value=shutil._ntuple_diskusage(1, 1, 0)), self.assertRaisesRegex(SafetyError, 'capacity'):
            self.fetch()
        self.assertFalse(self.dest.exists())
        def fail(stage, manifest):
            (stage / 'result.json').write_text('partial')
            raise OSError('disk full')
        self.transfer = fail
        with self.assertRaises(OSError):
            self.fetch()
        self.assertFalse(self.dest.exists())
        self.assertTrue((self.base / '.result.shk-transaction.json').is_file())

    def leftover_file_lists(self):
        return [path.name for path in self.base.iterdir() if path.name.startswith('.shk-files-')]

    def test_rsync_failure_raises_transfer_error_with_bounded_sanitized_tail(self):
        stage = self.base / 'stage'
        stage.mkdir(mode=0o700)
        with self.assertRaises(TransferError) as caught:
            rsync_transfer(str(self.base / 'missing-source'), stage, self.manifest)
        error = caught.exception
        self.assertIsInstance(error, SafetyError)
        self.assertIsInstance(error.returncode, int)
        self.assertNotIn(error.returncode, (0, 255))
        self.assertIn('No such file or directory', error.stderr_tail)
        self.assertIn(error.stderr_tail, str(error))
        self.assertIn(f'exit status {error.returncode}', str(error))
        self.assertLessEqual(len(error.stderr_tail), 500)
        self.assertFalse(any(ord(c) < 32 or ord(c) == 127 for c in error.stderr_tail))
        self.assertNotIn('.shk-files-', str(error))
        self.assertEqual(self.leftover_file_lists(), [])

    def test_transfer_error_redacts_file_list_and_bounds_long_output(self):
        stage = self.base / 'stage'
        stage.mkdir(mode=0o700)
        def leak(argv, **kwargs):
            name, = [a[len('--files-from='):] for a in argv if a.startswith('--files-from=')]
            self.assertTrue(Path(name).is_file())
            raise subprocess.CalledProcessError(1, argv, stderr=f'rsync: failed to open files-from file {name}: No such file\n'.encode())
        with patch('sherlock_artifacts.subprocess.run', side_effect=leak), self.assertRaises(TransferError) as caught:
            rsync_transfer(str(self.source), stage, self.manifest)
        self.assertEqual(caught.exception.returncode, 1)
        self.assertNotIn('.shk-files-', str(caught.exception))
        self.assertNotIn(str(self.base), caught.exception.stderr_tail)
        self.assertIn('files-from', caught.exception.stderr_tail)
        def flood(argv, **kwargs):
            raise subprocess.CalledProcessError(12, argv, stderr=b'x' * 1000 + b'\x00\x7f tail\n')
        with patch('sherlock_artifacts.subprocess.run', side_effect=flood), self.assertRaises(TransferError) as caught:
            rsync_transfer(str(self.source), stage, self.manifest)
        self.assertEqual(caught.exception.returncode, 12)
        self.assertEqual(len(caught.exception.stderr_tail), 500)
        self.assertTrue(caught.exception.stderr_tail.endswith('x tail'))
        self.assertEqual(self.leftover_file_lists(), [])

    def test_rsync_timeout_raises_transfer_error_and_removes_file_list(self):
        stage = self.base / 'stage'
        stage.mkdir(mode=0o700)
        def slow(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs['timeout'], stderr=b'partial \x1b[31moutput\x1b[0m\n')
        with patch('sherlock_artifacts.subprocess.run', side_effect=slow), self.assertRaises(TransferError) as caught:
            rsync_transfer(str(self.source), stage, self.manifest, timeout=7)
        error = caught.exception
        self.assertIsInstance(error, SafetyError)
        self.assertIsNone(error.returncode)
        self.assertIn('7', str(error))
        self.assertIn('deadline', str(error))
        self.assertEqual(error.stderr_tail, 'partial [31moutput[0m')
        self.assertEqual(self.leftover_file_lists(), [])

    def test_fetch_transfer_error_keeps_transaction_then_resumes(self):
        self.transfer = lambda stage, m: rsync_transfer(str(self.base / 'missing-source'), stage, m)
        with self.assertRaises(TransferError):
            self.fetch()
        self.assertFalse(self.dest.exists())
        self.assertTrue((self.base / '.result.shk-transaction.json').is_file())
        self.transfer = lambda stage, m: rsync_transfer(str(self.source), stage, m)
        self.assertFalse(self.fetch()['recovered'])
        self.assertEqual((self.dest / 'result.json').read_text(), '{"value":42}\n')

    def test_concurrent_fetchers_one_promotion(self):
        queue = multiprocessing.Queue()
        workers = [multiprocessing.Process(target=concurrent_fetch, args=(str(self.source), str(self.dest), self.manifest, queue)) for _ in range(2)]
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=15) for _ in workers]
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(results), [('ok', False), ('ok', True)])


if __name__ == '__main__':
    unittest.main()
