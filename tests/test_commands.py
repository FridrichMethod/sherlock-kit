from contextlib import closing
from dataclasses import asdict
import hashlib
import io
import json
import os
import py_compile
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sherlock_commands import FIELDS, REMOTE_MANIFEST, load_validator, main, parse_accounting
from sherlock_orchestration import Coordinator, SafetyError, submission_argv
from test_orchestration import spec, LIMITS
from sherlock_artifacts import build_manifest, fetch_bundle, rsync_transfer
from sherlock_kit import RemoteResult


class CommandBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.coordinator = Coordinator(self.root / 'state')
        self.attempt = self.coordinator.admit(spec(), LIMITS)

    def accounting(self, **kwargs):
        values = {'JobID': '123', 'JobIDRaw': '123', 'User': 'fixture', 'JobName': 'shk-' + self.attempt['id'], 'State': 'COMPLETED', 'ElapsedRaw': '60', 'AllocCPUS': '1', 'AllocTRES': 'cpu=1,mem=1024M,node=1', 'Submit': '2026-10-07T23:00:00', 'Start': '2026-10-07T23:00:01', 'End': '2026-10-07T23:01:01', 'Restarts': '0', 'ExitCode': '0:0', 'Cluster': 'sherlock', 'DBIndex': '42'}
        values.update(kwargs)
        attempt = {**self.attempt, 'created': 1791414000}
        # Set the real matching lower bound without relying on the test's clock.
        from sherlock_commands import epoch
        attempt['created'] = epoch(values['Submit'])
        return parse_accounting('|'.join(values[field] for field in FIELDS), attempt)

    def test_actual_sacct_fallback_fields_and_identity(self):
        records = self.accounting()
        self.assertTrue(records[0]['accounting_complete'])
        self.assertEqual(records[0]['cpu_seconds'], 60)
        for fields in ({'User': 'other'}, {'Cluster': 'other'}, {'DBIndex': ''}, {'Start': '2026-10-07T22:59:00'}, {'End': '2026-10-07T22:59:59'}, {'AllocTRES': 'cpu=1,cpu=2'}, {'AllocTRES': 'cpu=1,bad'}, {'Restarts': '1'}, {'JobID': '123_0', 'JobIDRaw': '900'}):
            with self.subTest(fields=fields), self.assertRaises(SafetyError):
                self.accounting(**fields)

    def test_gpu_tres_typed_generic_and_missing(self):
        self.attempt['spec']['resources']['gpus'] = 1
        for tres in ('cpu=1,gres/gpu:h100=1', 'cpu=1,gres/gpu=1,gres/gpu:h100=1'):
            record = self.accounting(AllocTRES=tres)[0]
            self.assertTrue(record['accounting_complete'])
            self.assertEqual(record['gpu_seconds'], 60)
        with self.assertRaises(SafetyError):
            self.accounting(AllocTRES='cpu=1,gres/gpu=2,gres/gpu:h100=1')
        self.assertFalse(self.accounting(AllocTRES='cpu=1')[0]['accounting_complete'])

    def test_direct_rejection_is_unknown_but_pre_dispatch_proof_is_not_sent(self):
        script = self.root / 'job.sh'
        script.write_text('#!/bin/sh\nprintf hello\n')
        counter = self.root / 'count'
        sbatch = self.root / 'sbatch'
        sbatch.write_text('#!/bin/sh\ncat > "$SHK_PAYLOAD"\nprintf 1 >> "$SHK_COUNT"\nif test -n "${SBATCH_GRES-}"; then exit 13; fi\nprintf "%s\\n" "$SHK_ACK"\nexit "$SHK_RC"\n')
        sbatch.chmod(0o700)
        frozen = asdict(spec(remote_script=str(script), script_digest=hashlib.sha256(script.read_bytes()).hexdigest()))
        run_directory = self.root / 'new run'
        run_directory.mkdir()
        frozen['remote_run_directory'] = str(run_directory)
        env = {**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'], 'SHK_COUNT': str(counter), 'SHK_PAYLOAD': str(self.root / 'received'), 'SHK_ACK': '123', 'SHK_RC': '0', 'SBATCH_GRES': 'gpu:99'}
        command = submission_argv(self.attempt['id'], frozen)
        options = json.loads(command[-2])
        self.assertIn('--chdir=' + str(run_directory), options)
        self.assertIn('--output=' + str(run_directory) + '/slurm-%j.out', options)
        self.assertIn('--error=' + str(run_directory) + '/slurm-%j.err', options)
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '123')
        self.assertEqual((self.root / 'received').read_bytes(), script.read_bytes())
        env['SHK_RC'] = '137'
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        self.assertTrue(result.stdout.startswith('SHK_UNKNOWN:'))
        counter_before = counter.read_text()
        script.write_text('#!/bin/sh\n#SBATCH --gres=gpu:99\n')
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        self.assertTrue(result.stdout.startswith('SHK_NOT_SENT:'))
        self.assertEqual(counter.read_text(), counter_before)

    def test_final_cost_high_watermark_survives_missing_evidence(self):
        record = {key: self.attempt['spec'][key] for key in ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest')}
        record.update(attempt=self.attempt['id'], job_id='123', submitted_at=self.attempt['created'], state='COMPLETED', cpu_seconds=120, gpu_seconds=0, cost_known=True, accounting_complete=True)
        self.coordinator.reconcile(self.attempt['id'], [record])
        self.coordinator.reconcile(self.attempt['id'], [{**record, 'cost_known': False, 'accounting_complete': False}])
        self.assertEqual(self.coordinator.get(self.attempt['id'])['cost_known'], 1)
        with self.assertRaises(SafetyError):
            self.coordinator.reconcile(self.attempt['id'], [{**record, 'cpu_seconds': 1}])
        self.assertEqual(self.coordinator.get(self.attempt['id'])['cpu_seconds'], 120)

    def test_conflicting_ack_survives_transaction_error(self):
        def runner(argv):
            with self.coordinator.transaction() as db:
                db.execute("UPDATE attempts SET job_id='124',state='submitted' WHERE id=?", (self.attempt['id'],))
            return RemoteResult('complete', stdout='123', returncode=0, dispatched=True)
        with self.assertRaises(SafetyError):
            self.coordinator.dispatch(self.attempt['id'], runner)
        with closing(self.coordinator.connect()) as db:
            evidence = db.execute('SELECT body FROM evidence WHERE attempt=?', (self.attempt['id'],)).fetchone()[0]
        self.assertEqual(json.loads(evidence)['job_id'], '123')
        self.assertEqual(self.coordinator.get(self.attempt['id'])['job_id'], '124')

    def test_terminal_conflicting_ack_quarantines_entire_scope(self):
        def runner(argv):
            evidence = {key: self.attempt['spec'][key] for key in ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest')}
            evidence.update(attempt=self.attempt['id'], job_id='124', submitted_at=self.attempt['created'], state='COMPLETED', cpu_seconds=1, gpu_seconds=0, accounting_complete=True)
            self.coordinator.reconcile(self.attempt['id'], [evidence])
            return RemoteResult('complete', stdout='123', returncode=0, dispatched=True)
        with self.assertRaises(SafetyError):
            self.coordinator.dispatch(self.attempt['id'], runner)
        self.assertEqual(self.coordinator.get(self.attempt['id'])['job_id'], '124')
        with self.assertRaisesRegex(SafetyError, 'quarantined'):
            self.coordinator.admit(spec(task='unrelated-new-task'), LIMITS)

    def test_verified_validator_snapshot_ignores_stale_pyc_and_path_replacement(self):
        module = self.root / 'validator.py'
        module.write_text('def validate(root): return False\n')
        py_compile.compile(str(module), doraise=True)
        info = module.stat()
        module.write_text('def validate(root): return True \n')
        os.utime(module, ns=(info.st_atime_ns, info.st_mtime_ns))
        cached = list((self.root / '__pycache__').iterdir())
        before = {path: path.read_bytes() for path in cached}
        sha = hashlib.sha256(module.read_bytes()).hexdigest()
        self.assertTrue(load_validator(module, sha, 'validate')(self.root))
        self.assertEqual(before, {path: path.read_bytes() for path in cached})
        import builtins
        original_compile = builtins.compile
        def replace_path(source, filename, mode):
            module.write_text('raise RuntimeError("unverified source executed")\n')
            return original_compile(source, filename, mode)
        with patch('builtins.compile', side_effect=replace_path):
            self.assertTrue(load_validator(module, sha, 'validate')(self.root))
        with self.assertRaisesRegex(SafetyError, 'identity mismatch'):
            load_validator(module, sha, 'validate')

    def test_remote_canonical_scope_rejects_symlink_before_read(self):
        allowed = self.root / 'authorized'
        source = allowed / 'bundle'
        source.mkdir(parents=True)
        outside = self.root / 'outside.json'
        outside.write_text('{"files": []}')
        manifest = allowed / 'manifest.json'
        manifest.symlink_to(outside)
        result = subprocess.run([sys.executable, '-c', REMOTE_MANIFEST, str(allowed), str(source), str(manifest)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        manifest.unlink()
        manifest.write_text('{"files": []}')
        source.rmdir()
        source.symlink_to(self.root, target_is_directory=True)
        result = subprocess.run([sys.executable, '-c', REMOTE_MANIFEST, str(allowed), str(source), str(manifest)], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_frozen_manifest_conflict_and_local_recovery(self):
        manifest = {'producer': 'synthetic', 'files': []}
        self.coordinator.pin_manifest(self.attempt['id'], manifest)
        self.assertEqual(self.coordinator.pinned_manifest(self.attempt['id']), manifest)
        with self.assertRaises(SafetyError):
            self.coordinator.pin_manifest(self.attempt['id'], {'producer': 'changed'})

    def test_real_cli_recovers_promoted_bundle_without_remote(self):
        validator = self.root / 'validator.py'
        validator.write_text('def validate(root): return (root / "result.json").read_text() == "42\\n"\n')
        frozen = spec(task='fetch', validator_path=str(validator), validator_digest=hashlib.sha256(validator.read_bytes()).hexdigest(), validator_function='validate')
        admitted = self.coordinator.admit(frozen, LIMITS)
        source = self.root / 'source'
        source.mkdir()
        (source / 'result.json').write_text('42\n')
        expected = {key: admitted['spec'][key] for key in ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest')}
        expected['attempt'] = admitted['id']
        manifest = build_manifest(source, expected)
        self.coordinator.pin_manifest(admitted['id'], manifest)
        fetch_root = self.root / 'fetch'
        fetch_root.mkdir()
        destination = fetch_root / 'verified'
        from sherlock_orchestration import digest
        receipt_digest = digest({'sha256': frozen.validator_digest, 'function': 'validate'})
        def crash(point):
            if point == 'promoted':
                raise RuntimeError('receipt not committed')
        with self.assertRaises(RuntimeError):
            fetch_bundle(manifest, destination, lambda stage, m: rsync_transfer(str(source), stage, m), source_manifest=lambda: manifest, validator=lambda root: True, validator_digest=receipt_digest, fault=crash)
        config = self.root / 'config.json'
        config.write_text(json.dumps(dict(schema_version=1, state_root=str(self.coordinator.root), principal='fixture', cluster='sherlock', limits=LIMITS, transport={}, fetch_root=str(fetch_root), validator_roots=[str(self.root)], remote_roots={'control': '/authorized', 'data': '/authorized', 'namespace_verified': True})))
        config.chmod(0o600)
        arguments = ['fetch', '--local', '--config', str(config), '--attempt', admitted['id'], '--manifest', '/authorized/manifest.json', '--source-root', '/authorized/output', '--destination', str(destination)]
        with patch('sherlock_kit.run_remote', side_effect=AssertionError('network ran')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(arguments), 0)
        self.assertTrue(json.loads(output.getvalue())['recovered'])
        self.assertTrue((fetch_root / '.verified.shk-receipt.json').is_file())

    def test_fetch_actor_and_root_rejections_precede_any_network(self):
        config = self.root / 'config.json'
        fetch_root = self.root / 'fetch'
        fetch_root.mkdir()
        payload = dict(schema_version=1, state_root=str(self.coordinator.root), principal='wrong', cluster='sherlock', limits=LIMITS, transport={}, fetch_root=str(fetch_root), remote_roots={'control': '/allowed', 'data': '/allowed', 'namespace_verified': True})
        config.write_text(json.dumps(payload))
        config.chmod(0o600)
        arguments = ['fetch', '--config', str(config), '--attempt', self.attempt['id'], '--manifest', '/allowed/manifest.json', '--source-root', '/allowed/output', '--destination', str(fetch_root)]
        with patch('sherlock_kit.run_remote', side_effect=AssertionError('network ran')), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            main(arguments)
        payload['principal'] = 'fixture'
        config.write_text(json.dumps(payload))
        with patch('sherlock_kit.run_remote', side_effect=AssertionError('network ran')), patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            main(arguments)
        self.assertFalse((self.root / '.fetch.shk-lock').exists())


if __name__ == '__main__':
    unittest.main()
