import copy
import json
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
import sherlock_artifacts
from sherlock_artifacts import (ATTEMPT_SIDECAR_LIMIT, TransferError, attempt_record, attempt_sidecar_path, build_manifest,
                               durable_json, fetch_bundle, read_attempt_sidecar, rsync_transfer)
from sherlock_orchestration import SafetyError, canonical, digest

D = 'a' * 64
PRODUCER = dict(attempt='1' * 32, cluster='sherlock', principal='fixture', code_digest=D, input_digest=D, runtime_digest=D, policy_digest=D)
VALIDATOR = dict(validator_path='/authorized/workload/validate.py', validator_digest='c' * 64, validator_function='check_result')


def concurrent_fetch(source, destination, manifest, queue):
    try:
        sidecar = attempt_record(PRODUCER['attempt'], PRODUCER, VALIDATOR, manifest)
        answer = fetch_bundle(manifest, Path(destination), lambda stage, m: rsync_transfer(source, stage, m), source_manifest=lambda: manifest, validator=lambda root: (root / 'result.json').read_text() == '{"value":42}\n', validator_digest=D, attempt_record=sidecar)
        queue.put(('ok', answer['recovered']))
    except Exception as exc:
        queue.put(('error', str(exc)))


class ArtifactFixture(unittest.TestCase):
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

    def sidecar(self, **overrides):
        record = attempt_record(PRODUCER['attempt'], PRODUCER, VALIDATOR, self.manifest)
        record.update(overrides)
        return record

    def metadata_files(self):
        return sorted(path.name for path in self.base.iterdir() if path.name.startswith('.result.'))

    def recording_writer(self, written):
        original = durable_json
        def record(path, value):
            written.append(Path(path).name)
            original(path, value)
        return patch('sherlock_artifacts.durable_json', side_effect=record)

    def manifest_for(self, attempt_id):
        manifest = copy.deepcopy(self.manifest)
        manifest['producer']['attempt'] = attempt_id
        return manifest


class ArtifactTests(ArtifactFixture):
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
        self.assertEqual(read_attempt_sidecar(self.dest), self.sidecar())
        self.assertEqual([name for name in self.metadata_files() if 'attempt' in name], ['.result.shk-attempt.json'])


class AttemptSidecarTests(ArtifactFixture):
    def test_attempt_record_shape_and_sidecar_path(self):
        record = self.sidecar()
        self.assertEqual(set(record), {'schema_version', 'attempt', 'producer', 'validator_path', 'validator_digest', 'validator_function', 'manifest', 'manifest_sha256'})
        self.assertEqual(record['schema_version'], 1)
        self.assertEqual(record['attempt'], PRODUCER['attempt'])
        self.assertEqual(record['producer'], PRODUCER)
        self.assertEqual({key: record[key] for key in VALIDATOR}, VALIDATOR)
        self.assertEqual(record['manifest'], self.manifest)
        self.assertEqual(record['manifest_sha256'], digest(self.manifest))
        self.assertIsNot(record['producer'], PRODUCER)
        self.assertIsNot(record['manifest'], self.manifest)
        self.assertEqual(attempt_sidecar_path(self.dest), self.base / '.result.shk-attempt.json')
        linked_parent = self.base / 'linked'
        linked_parent.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(SafetyError):
            attempt_sidecar_path(linked_parent / 'result')

    def test_attempt_record_rejects_malformed_inputs(self):
        cases = [
            dict(attempt_id=''), dict(attempt_id=42), dict(attempt_id='bad\nid'),
            dict(producer={**PRODUCER, 'code_digest': 'xyz'}), dict(producer={k: v for k, v in PRODUCER.items() if k != 'cluster'}), dict(producer='fixture'),
            dict(validator={**VALIDATOR, 'validator_digest': 'short'}), dict(validator={**VALIDATOR, 'validator_function': 'not an identifier'}),
            dict(validator={**VALIDATOR, 'validator_path': 'relative/validate.py'}), dict(validator={k: v for k, v in VALIDATOR.items() if k != 'validator_path'}),
            dict(validator=None), dict(manifest=[]), dict(manifest={**self.manifest, 'producer': 'missing'}),
        ]
        for case in cases:
            arguments = dict(attempt_id=PRODUCER['attempt'], producer=PRODUCER, validator=VALIDATOR, manifest=self.manifest)
            arguments.update(case)
            with self.subTest(case=case), self.assertRaises(SafetyError):
                attempt_record(**arguments)

    def test_sidecar_written_before_transaction_and_read_back(self):
        written = []
        with self.recording_writer(written):
            result = self.fetch(attempt_record=self.sidecar())
        self.assertFalse(result['recovered'])
        self.assertEqual(written, ['.result.shk-attempt.json', '.result.shk-transaction.json', '.result.shk-receipt.json'])
        sidecar = attempt_sidecar_path(self.dest)
        self.assertTrue(sidecar.is_file())
        self.assertEqual(sidecar.stat().st_mode & 0o077, 0)
        self.assertEqual(sidecar.read_text(), canonical(self.sidecar()) + '\n')
        self.assertEqual(read_attempt_sidecar(self.dest), self.sidecar())
        self.assertEqual(result['receipt']['manifest_sha256'], digest(self.manifest))

    def test_sidecar_survives_crash_before_transaction_and_is_verified_on_recovery(self):
        def crash(point):
            if point == 'intent_committed':
                raise RuntimeError('simulated process termination')
        with self.assertRaises(RuntimeError):
            self.fetch(fault=crash, attempt_record=self.sidecar())
        self.assertTrue(attempt_sidecar_path(self.dest).is_file())
        self.assertTrue((self.base / '.result.shk-transaction.json').is_file())
        self.assertFalse(self.dest.exists())
        written = []
        with self.recording_writer(written):
            self.assertFalse(self.fetch(attempt_record=self.sidecar())['recovered'])
        self.assertEqual(written, ['.result.shk-transaction.json', '.result.shk-receipt.json'])
        self.assertEqual(read_attempt_sidecar(self.dest), self.sidecar())

    def test_recovery_branch_writes_missing_sidecar_and_refuses_conflicting_one(self):
        def crash(point):
            if point == 'promoted':
                raise RuntimeError()
        with self.assertRaises(RuntimeError):
            self.fetch(fault=crash)
        self.assertTrue(self.dest.is_dir())
        self.assertFalse(attempt_sidecar_path(self.dest).exists())
        written = []
        with self.recording_writer(written):
            self.assertTrue(self.fetch(attempt_record=self.sidecar(), source_manifest=lambda: (_ for _ in ()).throw(ConnectionError()))['recovered'])
        self.assertEqual(written, ['.result.shk-attempt.json', '.result.shk-receipt.json'])
        self.assertEqual(read_attempt_sidecar(self.dest), self.sidecar())
        self.assertTrue(self.fetch(attempt_record=self.sidecar())['recovered'])
        foreign = attempt_record('2' * 32, {**PRODUCER, 'attempt': '2' * 32}, VALIDATOR, self.manifest_for('2' * 32))
        durable_json(attempt_sidecar_path(self.dest), foreign)
        with self.assertRaisesRegex(SafetyError, 'existing attempt sidecar conflicts'):
            self.fetch(attempt_record=self.sidecar())
        self.assertEqual(read_attempt_sidecar(self.dest), foreign)

    def test_existing_sidecar_conflict_refused_before_transaction(self):
        other_validator = attempt_record(PRODUCER['attempt'], PRODUCER, {**VALIDATOR, 'validator_digest': 'd' * 64}, self.manifest)
        durable_json(attempt_sidecar_path(self.dest), other_validator)
        with self.assertRaisesRegex(SafetyError, 'existing attempt sidecar conflicts'):
            self.fetch(attempt_record=self.sidecar())
        self.assertFalse(self.dest.exists())
        self.assertFalse((self.base / '.result.shk-transaction.json').exists())
        self.assertEqual(read_attempt_sidecar(self.dest), other_validator)
        self.assertFalse(self.fetch(attempt_record=other_validator)['recovered'])
        self.assertEqual(read_attempt_sidecar(self.dest), other_validator)

    def test_sidecar_disagreeing_with_manifest_refused_before_lock(self):
        cases = [
            dict(manifest_sha256='b' * 64),
            dict(producer={**PRODUCER, 'input_digest': 'b' * 64}),
            dict(attempt='2' * 32),
            dict(manifest=self.manifest_for('2' * 32)),
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaisesRegex(SafetyError, 'attempt sidecar (disagrees with manifest|malformed)'):
                self.fetch(attempt_record=self.sidecar(**case))
            self.assertEqual(self.metadata_files(), [])
        with self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
            self.fetch(attempt_record={'schema_version': 2})
        with self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
            self.fetch(attempt_record='sidecar')
        self.assertEqual(self.metadata_files(), [])
        self.assertFalse(self.dest.exists())

    def test_sidecar_follows_the_manifest_passed_to_fetch(self):
        foreign = self.manifest_for('2' * 32)
        record = attempt_record('2' * 32, foreign['producer'], VALIDATOR, foreign)
        with self.assertRaisesRegex(SafetyError, 'attempt sidecar disagrees with manifest'):
            self.fetch(attempt_record=record)
        self.assertEqual(self.metadata_files(), [])

    def test_reader_refuses_symlink_public_oversized_missing_and_malformed(self):
        sidecar = attempt_sidecar_path(self.dest)
        with self.assertRaises(FileNotFoundError):
            read_attempt_sidecar(self.dest)
        target = self.base / 'elsewhere.json'
        durable_json(target, self.sidecar())
        sidecar.symlink_to(target)
        with self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
            read_attempt_sidecar(self.dest)
        sidecar.unlink()
        durable_json(sidecar, self.sidecar())
        for mode in (0o640, 0o604, 0o644):
            os.chmod(sidecar, mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
                read_attempt_sidecar(self.dest)
        os.chmod(sidecar, 0o600)
        self.assertEqual(read_attempt_sidecar(self.dest), self.sidecar())
        with patch('sherlock_artifacts.os.geteuid', return_value=os.geteuid() + 1), self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
            read_attempt_sidecar(self.dest)
        sidecar.unlink()
        (self.base / 'sidecar-dir').mkdir()
        sidecar.symlink_to(self.base / 'sidecar-dir')
        with self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
            read_attempt_sidecar(self.dest)
        sidecar.unlink()
        with open(sidecar, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write('{"padding":"' + 'x' * ATTEMPT_SIDECAR_LIMIT + '"}\n')
        with self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
            read_attempt_sidecar(self.dest)
        for content in ('{not json', '[]', 'null', '"text"', json.dumps({**self.sidecar(), 'schema_version': 2}),
                        json.dumps({k: v for k, v in self.sidecar().items() if k != 'validator_path'}),
                        json.dumps({**self.sidecar(), 'extra': True}), json.dumps({**self.sidecar(), 'manifest_sha256': 'b' * 64}),
                        json.dumps({**self.sidecar(), 'attempt': '2' * 32}), json.dumps({**self.sidecar(), 'validator_function': 'not valid'})):
            with open(sidecar, 'w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
            with self.subTest(content=content[:40]), self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
                read_attempt_sidecar(self.dest)
        linked_parent = self.base / 'linked'
        linked_parent.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(SafetyError):
            read_attempt_sidecar(linked_parent / 'result')

    def test_fetch_refuses_unsafe_existing_sidecar_under_lock(self):
        sidecar = attempt_sidecar_path(self.dest)
        durable_json(sidecar, self.sidecar())
        os.chmod(sidecar, 0o644)
        with self.assertRaisesRegex(SafetyError, 'attempt sidecar'):
            self.fetch(attempt_record=self.sidecar())
        self.assertFalse((self.base / '.result.shk-transaction.json').exists())
        os.chmod(sidecar, 0o600)
        self.assertFalse(self.fetch(attempt_record=self.sidecar())['recovered'])

    def test_attempt_record_none_keeps_previous_behaviour(self):
        written = []
        with self.recording_writer(written):
            result = self.fetch()
        self.assertFalse(result['recovered'])
        self.assertEqual(written, ['.result.shk-transaction.json', '.result.shk-receipt.json'])
        self.assertEqual(self.metadata_files(), ['.result.shk-lock', '.result.shk-receipt.json', '.result.shk-transaction.json'])
        self.assertFalse(attempt_sidecar_path(self.dest).exists())
        with self.recording_writer(written):
            self.assertTrue(self.fetch()['recovered'])
        self.assertFalse(attempt_sidecar_path(self.dest).exists())
        self.assertEqual(result, {'destination': str(self.dest), 'receipt': {'schema_version': 1, 'manifest_sha256': digest(self.manifest), 'producer': PRODUCER, 'validator_sha256': D}, 'recovered': False})


if __name__ == '__main__':
    unittest.main()
