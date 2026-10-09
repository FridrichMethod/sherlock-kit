from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import py_compile
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sherlock_commands import (FIELDS, REMOTE_MANIFEST, gpu_count, installed_for_admission, load_validator, main, parse_accounting,
                               parse_rows, private_config, status)
from sherlock_orchestration import Coordinator, SUBMIT_TIME_TOLERANCE_SECONDS, SafetyError, canonical, submission_argv
from sherlock_partitions import partition_profile
from test_orchestration import spec, LIMITS
from sherlock_artifacts import TransferError, build_manifest, fetch_bundle, rsync_transfer
from sherlock_kit import RemoteResult, TransportConfig

IDENTITY = ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest')
MANY = {'cpus': 8, 'gpus': 0, 'tasks': 8, 'cpu_seconds': 36000}
CREATED = 1791414000  # 2026-10-07T23:00:00Z
PREEMPTION_MESSAGE = 'unexpected preemption on a non-preemptible partition; investigate, then `reconcile --acknowledge-preemption`'
SQUEUE_FORMAT = 'UserName:64,State:24,tres-alloc:128,TimeUsed:24,TimeLimit:24'


def stamp(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def sacct_line(attempt_id, created, **overrides):
    values = {'JobID': '123', 'JobIDRaw': '123', 'User': 'fixture', 'JobName': 'shk-' + attempt_id, 'State': 'COMPLETED', 'ElapsedRaw': '60', 'AllocCPUS': '1',
              'AllocTRES': 'cpu=1,mem=1024M,node=1', 'Submit': stamp(created), 'Start': stamp(created + 1), 'End': stamp(created + 61), 'Restarts': '0',
              'ExitCode': '0:0', 'Cluster': 'sherlock', 'DBIndex': '42'}
    values.update(overrides)
    return '|'.join(values[field] for field in FIELDS)


def squeue_line(user, state, tres, used='1:00', limit='2:00:00'):
    """squeue -O pads every column to its declared width; tres-alloc is empty for pending jobs."""
    return f'{user:<64}{state:<24}{tres:<128}{used:<24}{limit:<24}'


def complete(stdout):
    return RemoteResult('complete', stdout=stdout, returncode=0, dispatched=True)


def recorder(calls, stdout):
    def fake(transport, argv, mutation=False):
        calls.append(list(argv))
        return complete(stdout)
    return fake


class CommandBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.coordinator = Coordinator(self.root / 'state')
        self.attempt = self.coordinator.admit(spec(), LIMITS)
        self.configs = 0

    def accounting(self, **kwargs):
        # Set the real matching lower bound without relying on the test's clock.
        attempt = {**self.attempt, 'created': CREATED}
        return parse_accounting(sacct_line(self.attempt['id'], CREATED, **kwargs), attempt)

    def config_file(self, coordinator=None, limits=LIMITS, **extra):
        self.configs += 1
        config = self.root / f'config-{self.configs}.json'
        payload = {**dict(schema_version=1, state_root=str((coordinator or self.coordinator).root), principal='fixture', cluster='sherlock', limits=limits, transport={}), **extra}
        config.write_text(json.dumps(payload))
        config.chmod(0o600)
        return str(config)

    def run_main(self, arguments, remote):
        with patch('sherlock_kit.run_remote', side_effect=remote), patch('sys.stdout', new_callable=io.StringIO) as out, patch('sys.stderr', new_callable=io.StringIO) as err:
            try:
                code = main(arguments)
            except SystemExit as stop:
                code = stop.code
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def query_keys(self, coordinator=None):
        with closing((coordinator or self.coordinator).connect()) as db:
            return [row[0] for row in db.execute('SELECT key FROM queries')]

    def test_actual_sacct_fallback_fields_and_identity(self):
        records = self.accounting()
        self.assertTrue(records[0]['accounting_complete'])
        self.assertEqual(records[0]['cpu_seconds'], 60)
        self.assertEqual((records[0]['restart'], records[0]['restart_history_complete']), (0, True))
        self.assertEqual((records[0]['db_index'], records[0]['start_time']), ('42', CREATED + 1))
        self.assertEqual({key: records[0][key] for key in IDENTITY}, {key: self.attempt['spec'][key] for key in IDENTITY})
        for fields in ({'User': 'other'}, {'Cluster': 'other'}, {'DBIndex': ''}, {'Start': '2026-10-07T22:59:00'}, {'End': '2026-10-07T22:59:59'}, {'AllocTRES': 'cpu=1,cpu=2'}, {'AllocTRES': 'cpu=1,bad'}, {'JobID': '123_0', 'JobIDRaw': '900'},
                       {'State': ''}, {'Submit': stamp(CREATED - SUBMIT_TIME_TOLERANCE_SECONDS - 1)}, {'AllocCPUS': '2'}, {'ElapsedRaw': '-1'}, {'JobIDRaw': '0'}):
            with self.subTest(fields=fields), self.assertRaises(SafetyError):
                self.accounting(**fields)
        # Restart counts are parsed, not refused: reconcile decides whether a restart is an anomaly.
        restarted = self.accounting(Restarts='1')[0]
        self.assertEqual((restarted['restart'], restarted['restart_history_complete']), (1, False))
        self.assertEqual(self.accounting(Submit=stamp(CREATED - SUBMIT_TIME_TOLERANCE_SECONDS + 1), Start=stamp(CREATED - SUBMIT_TIME_TOLERANCE_SECONDS + 2))[0]['state'], 'COMPLETED')
        with self.assertRaisesRegex(SafetyError, 'malformed'):
            parse_rows('a|b|c\n')
        self.assertEqual(parse_rows(''), [])
        self.assertEqual(parse_rows(sacct_line('x', CREATED) + '|\n\n')[0]['JobName'], 'shk-x')
        with self.assertRaisesRegex(SafetyError, 'conflicts with acknowledgement'):
            parse_accounting(sacct_line(self.attempt['id'], CREATED), {**self.attempt, 'created': CREATED, 'job_id': '999'})

    def test_non_completed_sacct_row_shapes(self):
        unknown = {'Start': 'Unknown', 'End': 'Unknown', 'ElapsedRaw': '0', 'AllocCPUS': '0', 'AllocTRES': ''}
        cases = {'preempted': ({'State': 'PREEMPTED'}, 'PREEMPTED', True),
                 'boot_fail': ({'State': 'BOOT_FAIL'}, 'BOOT_FAIL', True),
                 'node_fail': ({'State': 'NODE_FAIL'}, 'NODE_FAIL', True),
                 'requeued': ({'State': 'REQUEUED'}, 'REQUEUED', True),
                 'requeued_open': ({'State': 'REQUEUED', 'End': 'Unknown'}, 'REQUEUED', False),
                 'cancelled_by': ({'State': 'CANCELLED by 1234'}, 'CANCELLED', True),
                 'completed_plus': ({'State': 'COMPLETED+'}, 'COMPLETED', True),
                 'pending': ({'State': 'PENDING', **unknown}, 'PENDING', False),
                 'running': ({'State': 'RUNNING', 'End': 'Unknown'}, 'RUNNING', False),
                 'completing': ({'State': 'COMPLETING', 'End': 'Unknown'}, 'COMPLETING', False),
                 'cancelled_before_start': ({'State': 'CANCELLED by 1234', **unknown, 'End': stamp(CREATED + 5)}, 'CANCELLED', True)}
        for name, (fields, state, final) in cases.items():
            with self.subTest(shape=name):
                record = self.accounting(**fields)[0]
                self.assertEqual(record['state'], state)
                self.assertIs(record['accounting_complete'], final)
                self.assertIs(record['cost_known'], final)
                self.assertEqual((record['restart'], record['restart_history_complete'], record['kind']), (0, True, 'scheduler'))
                self.assertEqual(record['start_time'], None if fields.get('Start') == 'Unknown' else CREATED + 1)
        self.assertEqual(self.accounting(**{**unknown, 'State': 'CANCELLED by 1', 'End': stamp(CREATED + 5)})[0]['cpu_seconds'], 0)

    def test_duplicate_rows_for_requeue_attempt_carry_restart_history(self):
        owners = Coordinator(self.root / 'owners')
        attempt = owners.admit(spec(resources={**spec().resources, 'partition': 'owners'}), LIMITS)
        created = attempt['created']
        first = sacct_line(attempt['id'], created, State='PREEMPTED', ElapsedRaw='30', End=stamp(created + 31))
        second = sacct_line(attempt['id'], created, State='COMPLETED', Restarts='1', Start=stamp(created + 100), End=stamp(created + 160), DBIndex='43')
        records = parse_accounting(first + '\n' + second + '\n', attempt)
        self.assertEqual([record['restart'] for record in records], [0, 1])
        self.assertEqual([record['restart_history_complete'] for record in records], [True, True])
        self.assertEqual([(record['cost_known'], record['accounting_complete']) for record in records], [(True, True), (True, True)])
        self.assertEqual([record['cpu_seconds'] for record in records], [30, 60])
        result = owners.reconcile(attempt['id'], records)
        self.assertEqual(result['resolution'], 'terminal')
        self.assertEqual((result['attempt']['cpu_seconds'], result['attempt']['cost_known'], result['attempt']['reserved']), (90, 1, 0))
        gap = sacct_line(attempt['id'], created, State='COMPLETED', Restarts='2', Start=stamp(created + 100), End=stamp(created + 160), DBIndex='44')
        records = parse_accounting(first + '\n' + gap, owners.get(attempt['id']))
        self.assertEqual([record['restart_history_complete'] for record in records], [False, False])
        # A restart on a non-preemptible partition is parsed and then flagged by reconcile.
        restarted = parse_accounting(sacct_line(self.attempt['id'], self.attempt['created'], State='RUNNING', Restarts='1', End='Unknown'), self.attempt)
        self.assertEqual((restarted[0]['restart'], restarted[0]['restart_history_complete'], restarted[0]['cost_known']), (1, False, False))
        self.assertEqual(self.coordinator.reconcile(self.attempt['id'], restarted)['resolution'], 'unexpected_preemption')
        self.assertEqual(self.coordinator.get(self.attempt['id'])['reserved'], 1)

    def test_gpu_tres_typed_generic_and_missing(self):
        self.attempt['spec']['resources']['gpus'] = 1
        for tres in ('cpu=1,gres/gpu:h100=1', 'cpu=1,gres/gpu=1,gres/gpu:h100=1'):
            record = self.accounting(AllocTRES=tres)[0]
            self.assertTrue(record['accounting_complete'])
            self.assertEqual(record['gpu_seconds'], 60)
        with self.assertRaises(SafetyError):
            self.accounting(AllocTRES='cpu=1,gres/gpu=2,gres/gpu:h100=1')
        for tres in ('cpu=1,gres/gpu=x', 'cpu=1,gres/gpu:h100=x', 'cpu=1,gres/gpu=-1'):
            with self.subTest(tres=tres), self.assertRaises(SafetyError):
                self.accounting(AllocTRES=tres)
        self.assertFalse(self.accounting(AllocTRES='cpu=1')[0]['accounting_complete'])

    def test_private_config_rejects_unknown_fields_before_state_root(self):
        state = self.root / 'uncreated'
        config = self.root / 'private.json'
        base = dict(schema_version=1, state_root=str(state), principal='fixture', cluster='sherlock', limits=LIMITS, transport={})
        config.write_text(json.dumps({**base, 'max_jobs': 4, 'extra_root': '/synthetic/extra'}))
        config.chmod(0o600)
        with self.assertRaisesRegex(SafetyError, r'^unknown private config field\(s\): extra_root, max_jobs; filesystem and partition access are not configurable$'):
            private_config(config)
        code, output, err = self.run_main(['status', '--local', '--config', str(config), '--attempt', self.attempt['id']], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('shk: unknown private config field(s): extra_root, max_jobs; filesystem and partition access are not configurable', err)
        self.assertFalse(state.exists())
        config.write_text(json.dumps({**base, 'fetch_root': '/synthetic/results', 'validator_roots': [], 'remote_roots': {}, 'grant': None}))
        self.assertEqual(private_config(config)['principal'], 'fixture')
        config.write_text(json.dumps({key: value for key, value in base.items() if key != 'limits'}))
        with self.assertRaisesRegex(SafetyError, 'schema mismatch'):
            private_config(config)

    def test_status_reuses_one_query_per_attempt_within_cadence(self):
        created = self.attempt['created']
        calls = []
        transport = TransportConfig(backoff_file=self.root / 'backoff.json')
        config = {'principal': 'fixture', 'cluster': 'sherlock'}
        with patch('sherlock_kit.run_remote', side_effect=recorder(calls, sacct_line(self.attempt['id'], created, State='RUNNING', End='Unknown') + '\n')):
            first = status(self.coordinator, self.attempt['id'], config, transport)
            second = status(self.coordinator, self.attempt['id'], config, transport)
        self.assertEqual((first['resolution'], first['attempt']['job_id'], first['attempt']['state']), ('identified', '123', 'submitted'))
        self.assertEqual(second['resolution'], 'identified')
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[:9], ['env', 'TZ=UTC', 'LC_ALL=C', 'SLURM_TIME_FORMAT=standard', 'sacct', '-n', '-P', '--local', '--allocations'])
        for flag in ('--user=fixture', '--duplicates', '--endtime=now', '--name=shk-' + self.attempt['id'], '--starttime=' + stamp(created - SUBMIT_TIME_TOLERANCE_SECONDS)):
            self.assertIn(flag, argv)
        self.assertFalse(any(flag.startswith('--jobs') for flag in argv))
        self.assertEqual(self.query_keys(), [canonical(['sherlock', 'fixture', 'sherlock-plain', 'sacct', self.attempt['id']])])
        with self.assertRaisesRegex(SafetyError, 'another principal'):
            status(self.coordinator, self.attempt['id'], {'principal': 'other', 'cluster': 'sherlock'}, transport)
        self.assertEqual(status(self.coordinator, self.attempt['id'], config, transport, remote=False)['resolution'], 'local_only')

    def test_status_all_runs_one_name_query_and_reconciles_each_attempt(self):
        coordinator = Coordinator(self.root / 'all')
        ack = lambda stdout, rc: (lambda command: RemoteResult('complete' if rc == 0 else 'unknown', stdout=stdout, returncode=rc, dispatched=True))
        first = coordinator.admit(spec(task='a'), MANY)['id']
        coordinator.dispatch(first, ack('101', 0))
        second = coordinator.admit(spec(task='b'), MANY)['id']
        coordinator.dispatch(second, ack('', 255))
        coordinator.admit(spec(task='c'), MANY)
        self.assertEqual([row['state'] for row in coordinator.unresolved()], ['submitted', 'unknown'])
        created = min(row['created'] for row in coordinator.unresolved())
        rows = '\n'.join((sacct_line(first, created, JobID='101', JobIDRaw='101'),
                          sacct_line(second, created, JobID='102', JobIDRaw='102', State='RUNNING', End='Unknown'))) + '\n'
        config = self.config_file(coordinator, limits=MANY)
        calls = []
        code, output, err = self.run_main(['status', '--all', '--config', config], recorder(calls, rows))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual([item['resolution'] for item in output], ['terminal', 'identified'])
        self.assertEqual([item['attempt']['id'] for item in output], [first, second])
        self.assertEqual(output[1]['attempt']['job_id'], '102')
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertIn('--name=shk-' + first + ',shk-' + second, argv)
        self.assertFalse(any(flag.startswith('--jobs') for flag in argv))
        for flag in ('--user=fixture', '--duplicates', '--starttime=' + stamp(created - SUBMIT_TIME_TOLERANCE_SECONDS), '--endtime=now'):
            self.assertIn(flag, argv)
        self.assertEqual(self.query_keys(coordinator), [canonical(['sherlock', 'fixture', 'sherlock-plain', 'sacct-all'])])
        code, output, err = self.run_main(['reconcile', '--all', '--config', config], AssertionError('cadence violated'))
        self.assertEqual((code, [item['resolution'] for item in output]), (0, ['identified']))
        self.assertEqual(output[0]['attempt']['id'], second)
        code, output, err = self.run_main(['reconcile', '--all', '--local', '--config', config], AssertionError('network ran'))
        self.assertEqual((code, [item['resolution'] for item in output]), (0, ['local_only']))
        code, output, err = self.run_main(['status', '--all', '--attempt', first, '--config', config], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        code, output, err = self.run_main(['status', '--config', config], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        code, output, err = self.run_main(['reconcile', '--all', '--acknowledge-preemption', '--config', config], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('requires --attempt', err)
        config = self.config_file(coordinator, limits=MANY, principal='other')
        code, output, err = self.run_main(['status', '--all', '--config', config], AssertionError('network ran'))
        self.assertEqual(code, 0)
        self.assertEqual([item['resolution'] for item in output], ['inconclusive'])
        self.assertIn('another principal', output[0]['reason'])

    def test_status_all_isolates_per_attempt_errors(self):
        coordinator = Coordinator(self.root / 'isolate')
        ack = lambda stdout: (lambda command: RemoteResult('complete', stdout=stdout, returncode=0, dispatched=True))
        first = coordinator.admit(spec(task='a'), MANY)['id']
        coordinator.dispatch(first, ack('101'))
        second = coordinator.admit(spec(task='b'), MANY)['id']
        coordinator.dispatch(second, ack('102'))
        created = min(row['created'] for row in coordinator.unresolved())
        rows = '\n'.join((sacct_line(first, created, JobID='101', JobIDRaw='101'),
                          sacct_line(second, created, JobID='102', JobIDRaw='102', User='someone-else'))) + '\n'
        config = self.config_file(coordinator, limits=MANY)
        code, output, err = self.run_main(['reconcile', '--all', '--config', config], recorder([], rows))
        self.assertEqual(code, 2)
        self.assertEqual([item['resolution'] for item in output], ['terminal', 'error'])
        self.assertIn('ownership', output[1]['error'])
        self.assertIn('could not be reconciled', err)
        self.assertEqual((coordinator.get(first)['state'], coordinator.get(first)['reserved']), ('terminal', 0))
        self.assertEqual((coordinator.get(second)['state'], coordinator.get(second)['reserved']), ('submitted', 1))

    def test_status_all_without_unresolved_attempts_queries_nothing(self):
        coordinator = Coordinator(self.root / 'idle')
        config = self.config_file(coordinator)
        code, output, err = self.run_main(['status', '--all', '--config', config], AssertionError('network ran'))
        self.assertEqual((code, output, err), (0, [], ''))
        code, output, err = self.run_main(['status', '--all', '--config', config], lambda *a, **k: RemoteResult('auth_required', stderr='denied', returncode=255, dispatched=True))
        self.assertEqual((code, output), (0, []))

    def test_status_all_transport_failure_is_inconclusive_for_each_attempt(self):
        coordinator = Coordinator(self.root / 'down')
        attempt = coordinator.admit(spec(), LIMITS)['id']
        coordinator.dispatch(attempt, lambda command: RemoteResult('complete', stdout='123', returncode=0, dispatched=True))
        config = self.config_file(coordinator)
        code, output, _ = self.run_main(['status', '--all', '--config', config], lambda *a, **k: RemoteResult('auth_required', stderr='denied', returncode=255, dispatched=True))
        self.assertEqual(code, 0)
        self.assertEqual([(item['resolution'], item['transport']) for item in output], [('inconclusive', 'auth_required')])
        self.assertEqual(output[0]['attempt']['id'], attempt)

    def test_unexpected_preemption_exits_two_until_acknowledged(self):
        coordinator = Coordinator(self.root / 'preempt')
        attempt = coordinator.admit(spec(), LIMITS)['id']
        coordinator.dispatch(attempt, lambda command: RemoteResult('complete', stdout='123', returncode=0, dispatched=True))
        created = coordinator.get(attempt)['created']
        config = self.config_file(coordinator)
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--acknowledge-preemption'], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('reconcile first', err)
        preempted = sacct_line(attempt, created, State='PREEMPTED') + '\n'
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt], recorder([], preempted))
        self.assertEqual(code, 2)
        self.assertEqual((output['resolution'], output['attempt']['state'], output['attempt']['reserved']), ('unexpected_preemption', 'submitted', 1))
        self.assertEqual(err, 'shk: ' + PREEMPTION_MESSAGE + '\n')
        code, output, err = self.run_main(['reconcile', '--all', '--config', config], recorder([], preempted))
        self.assertEqual(code, 2)
        self.assertEqual([item['resolution'] for item in output], ['unexpected_preemption'])
        self.assertEqual(err, 'shk: ' + PREEMPTION_MESSAGE + '\n')
        other = self.config_file(coordinator, principal='other')
        code, output, err = self.run_main(['reconcile', '--config', other, '--attempt', attempt, '--acknowledge-preemption'], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('another principal', err)
        self.assertEqual(coordinator.get(attempt)['reserved'], 1)
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--acknowledge-preemption'], AssertionError('network ran'))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['id'], output['state'], output['reserved'], output['cost_known'], output['cpu_seconds']), (attempt, 'terminal', 0, 1, 60))
        self.assertEqual(coordinator.unresolved(), [])
        code, output, err = self.run_main(['status', '--all', '--config', config], AssertionError('network ran'))
        self.assertEqual((code, output), (0, []))

    def test_installed_for_admission_pin_cases(self):
        identity = {'schema_version': 1, 'code_revision': 'f' * 40, 'policy_sha256': 'b' * 64, 'install_mode': 'frozen', 'partitions_sha256': 'c' * 64}
        frozen = spec(policy_digest='b' * 64)
        pin = self.root / 'pin.json'
        cases = {'legacy': ({key: identity[key] for key in ('schema_version', 'code_revision', 'policy_sha256')}, None),
                 'full': (identity, None),
                 'partitions': ({**identity, 'partitions_sha256': '0' * 64}, 'revision mismatch'),
                 'revision': ({**identity, 'code_revision': '0' * 40}, 'revision mismatch'),
                 'policy': ({**identity, 'policy_sha256': '0' * 64}, 'revision mismatch'),
                 'schema': ({**identity, 'schema_version': 2}, 'revision mismatch'),
                 'malformed': ([], 'revision mismatch')}
        for name, (advertised, error) in cases.items():
            with self.subTest(pin=name), patch('sherlock_kit.policy_identity', return_value=identity), patch.dict(os.environ, {'SHERLOCK_KIT_PIN': str(pin)}):
                pin.write_text(json.dumps(advertised))
                if error is None:
                    self.assertEqual(installed_for_admission(frozen), identity)
                else:
                    with self.assertRaisesRegex(SafetyError, error):
                        installed_for_admission(frozen)
        with patch('sherlock_kit.policy_identity', return_value=identity), patch.dict(os.environ):
            os.environ.pop('SHERLOCK_KIT_PIN', None)
            self.assertEqual(installed_for_admission(frozen), identity)
            with self.assertRaisesRegex(SafetyError, 'differs from installed policy'):
                installed_for_admission(spec())
        with patch('sherlock_kit.policy_identity', return_value={**identity, 'install_mode': 'development'}), self.assertRaisesRegex(SafetyError, 'frozen'):
            installed_for_admission(frozen)

    def test_direct_rejection_is_unknown_but_pre_dispatch_proof_is_not_sent(self):
        script = self.root / 'job.sh'
        script.write_text('#!/bin/sh\nprintf hello\n')
        counter = self.root / 'count'
        sbatch = self.root / 'sbatch'
        sbatch.write_text('#!/bin/sh\ncat > "$SHK_PAYLOAD"\nprintf 1 >> "$SHK_COUNT"\nif test -n "${SBATCH_GRES-}"; then exit 13; fi\nprintf "%s\\n" "$SHK_ACK"\nexit "$SHK_RC"\n')
        sbatch.chmod(0o700)
        run_directory = self.root / 'new run'
        run_directory.mkdir()
        # The admitted spec carries the frozen partition profile and requeue decision.
        frozen = {**self.attempt['spec'], 'remote_script': str(script), 'script_digest': hashlib.sha256(script.read_bytes()).hexdigest(), 'remote_run_directory': str(run_directory)}
        env = {**os.environ, 'PATH': str(self.root) + os.pathsep + os.environ['PATH'], 'SHK_COUNT': str(counter), 'SHK_PAYLOAD': str(self.root / 'received'), 'SHK_ACK': '123', 'SHK_RC': '0', 'SBATCH_GRES': 'gpu:99'}
        command = submission_argv(self.attempt['id'], frozen)
        options = json.loads(command[-2])
        self.assertIn('--chdir=' + str(run_directory), options)
        self.assertIn('--output=' + str(run_directory) + '/slurm-%j.out', options)
        self.assertIn('--error=' + str(run_directory) + '/slurm-%j.err', options)
        self.assertIn('--no-requeue', options)
        self.assertNotIn('--requeue', options)
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
        record = {key: self.attempt['spec'][key] for key in IDENTITY}
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
            evidence = {key: self.attempt['spec'][key] for key in IDENTITY}
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

    def fetch_fixture(self):
        """An admitted fetch attempt, its producer-bound manifest and the authorized local roots."""
        validator = self.root / 'validator.py'
        validator.write_text('def validate(root): return (root / "result.json").read_text() == "42\\n"\n')
        frozen = spec(task='fetch', validator_path=str(validator), validator_digest=hashlib.sha256(validator.read_bytes()).hexdigest(), validator_function='validate')
        admitted = self.coordinator.admit(frozen, LIMITS)
        source = self.root / 'source'
        source.mkdir()
        (source / 'result.json').write_text('42\n')
        expected = {key: admitted['spec'][key] for key in IDENTITY}
        expected['attempt'] = admitted['id']
        manifest = build_manifest(source, expected)
        fetch_root = self.root / 'fetch'
        fetch_root.mkdir()
        config = self.config_file(fetch_root=str(fetch_root), validator_roots=[str(self.root)], remote_roots={'control': '/authorized', 'data': '/authorized', 'namespace_verified': True})
        arguments = ['fetch', '--config', config, '--attempt', admitted['id'], '--manifest', '/authorized/manifest.json', '--source-root', '/authorized/output', '--destination', str(fetch_root / 'verified')]
        return frozen, admitted, source, manifest, fetch_root, arguments

    def test_real_cli_recovers_promoted_bundle_without_remote(self):
        frozen, admitted, source, manifest, fetch_root, arguments = self.fetch_fixture()
        self.coordinator.pin_manifest(admitted['id'], manifest)
        destination = fetch_root / 'verified'
        from sherlock_orchestration import digest
        receipt_digest = digest({'sha256': frozen.validator_digest, 'function': 'validate'})
        def crash(point):
            if point == 'promoted':
                raise RuntimeError('receipt not committed')
        with self.assertRaises(RuntimeError):
            fetch_bundle(manifest, destination, lambda stage, m: rsync_transfer(str(source), stage, m), source_manifest=lambda: manifest, validator=lambda root: True, validator_digest=receipt_digest, fault=crash)
        with patch('sherlock_kit.run_remote', side_effect=AssertionError('network ran')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(arguments[:1] + ['--local'] + arguments[1:]), 0)
        self.assertTrue(json.loads(output.getvalue())['recovered'])
        self.assertTrue((fetch_root / '.verified.shk-receipt.json').is_file())

    def test_fetch_transfer_failures_exit_two_and_retain_transaction(self):
        _, admitted, source, manifest, fetch_root, arguments = self.fetch_fixture()
        destination = fetch_root / 'verified'
        def remote(transport, argv, mutation=False):
            self.assertEqual(argv[:3], ['python3', '-c', REMOTE_MANIFEST])
            self.assertEqual(argv[3:], ['/authorized', '/authorized/output', '/authorized/manifest.json'])
            return complete(json.dumps(manifest))
        failures = {'transfer_error': TransferError('rsync transfer failed with exit status 23: some files could not be transferred', returncode=23, stderr_tail='some files could not be transferred'),
                    'called_process_error': subprocess.CalledProcessError(12, ['rsync']),
                    'timeout_expired': subprocess.TimeoutExpired(['rsync'], 300)}
        for name, failure in failures.items():
            with self.subTest(failure=name):
                transfers = []
                def transfer(transport, remote_source, stage, m, failure=failure, transfers=transfers):
                    transfers.append((transport.data_host, remote_source, Path(stage), m))
                    raise failure
                with patch('sherlock_kit.run_remote', side_effect=remote), patch('sherlock_kit.data_transfer', side_effect=transfer), \
                        patch('sys.stdout', new_callable=io.StringIO) as out, patch('sys.stderr', new_callable=io.StringIO) as err, self.assertRaises(SystemExit) as caught:
                    main(arguments)
                self.assertEqual(caught.exception.code, 2)
                self.assertTrue(err.getvalue().startswith('shk: '), err.getvalue())
                self.assertEqual(out.getvalue(), '')
                self.assertEqual(len(transfers), 1)
                host, remote_source, stage, transferred = transfers[0]
                self.assertEqual((host, remote_source), ('sherlock-dtn', '/authorized/output'))
                self.assertEqual(stage.parent, fetch_root)
                self.assertTrue(stage.name.startswith('.verified.shk-stage-'))
                self.assertEqual(transferred['producer']['attempt'], admitted['id'])
                self.assertFalse(destination.exists())
                self.assertTrue((fetch_root / '.verified.shk-transaction.json').is_file())
                self.assertFalse((fetch_root / '.verified.shk-receipt.json').exists())
                if name == 'transfer_error':
                    self.assertIn('exit status 23', err.getvalue())
        self.assertEqual(self.coordinator.pinned_manifest(admitted['id']), manifest)
        def transfer(transport, remote_source, stage, m):
            for item in m['files']:
                shutil.copy(source / item['path'], Path(stage) / item['path'])
        with patch('sherlock_kit.run_remote', side_effect=remote), patch('sherlock_kit.data_transfer', side_effect=transfer), patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertEqual(main(arguments), 0)
        self.assertFalse(json.loads(out.getvalue())['recovered'])
        self.assertEqual((destination / 'result.json').read_text(), '42\n')
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

    def test_occupancy_parses_fixed_columns_aggregates_and_caches(self):
        payload = '\n'.join((squeue_line('alice', 'RUNNING', 'cpu=4,mem=32G,node=1,billing=4,gres/gpu=2,gres/gpu:h100=2'),
                             squeue_line('alice', 'PENDING', ''),
                             squeue_line('bob', 'RUNNING', 'cpu=8,gres/gpu:h100=1'),
                             squeue_line('bob', 'COMPLETING', 'cpu=1,gres/gpu=1'),
                             squeue_line('carol', 'SUSPENDED', 'cpu=1,gres/gpu=4'),
                             squeue_line('dave', 'PENDING', 'N/A'))) + '\n'
        config = self.config_file()
        calls = []
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'owners'], recorder(calls, payload))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(calls, [['env', 'LC_ALL=C', 'squeue', '-h', '-p', 'owners', '-O', SQUEUE_FORMAT]])
        self.assertEqual(report['partition'], 'owners')
        self.assertEqual(report['profile'], dict(partition_profile('owners')))
        self.assertEqual(report['users'], {'alice': {'running_jobs': 1, 'running_gpus': 2, 'pending_jobs': 1},
                                           'bob': {'running_jobs': 2, 'running_gpus': 2, 'pending_jobs': 0},
                                           'dave': {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 1}})
        self.assertEqual(report['totals'], {'running_jobs': 3, 'running_gpus': 4, 'pending_jobs': 2})
        self.assertEqual(report['transport'], 'complete')
        cached_code, cached, err = self.run_main(['occupancy', '--config', config, '--partition', 'owners'], AssertionError('cadence violated'))
        self.assertEqual((cached_code, cached), (0, report))
        self.assertIn(canonical(['sherlock', 'fixture', 'sherlock-plain', 'squeue', 'owners']), self.query_keys())
        denied = lambda *a, **k: RemoteResult('auth_required', stderr='Permission denied', returncode=255, dispatched=True)
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'btrippe'], denied)
        self.assertEqual((code, err), (1, ''))
        self.assertEqual(report, {'partition': 'btrippe', 'profile': dict(partition_profile('btrippe')), 'users': {},
                                  'totals': {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 0}, 'transport': 'auth_required'})
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'gpu'], AssertionError('network ran'))
        self.assertEqual((code, report), (2, None))
        self.assertIn('unknown partition', err)
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'normal'], recorder([], squeue_line('erin', 'RUNNING', 'cpu=1,gres/gpu=x') + '\n'))
        self.assertEqual((code, report), (2, None))
        self.assertIn('shk: ', err)

    def test_gpu_count_prefers_generic_over_typed_and_never_double_counts(self):
        self.assertEqual(gpu_count(''), 0)
        self.assertEqual(gpu_count('N/A'), 0)
        self.assertEqual(gpu_count('cpu=4,mem=32G,node=1'), 0)
        self.assertEqual(gpu_count('cpu=4,gres/gpu=2'), 2)
        self.assertEqual(gpu_count('cpu=4,gres/gpu:h100=1'), 1)
        self.assertEqual(gpu_count('cpu=4,gres/gpu:a100=1,gres/gpu:h100=2'), 3)
        self.assertEqual(gpu_count('cpu=4,gres/gpu=2,gres/gpu:h100=2'), 2)
        self.assertEqual(gpu_count('cpu=4,gres/gpu=3,gres/gpu:h100=1'), 3)
        for malformed in ('gres/gpu=x', 'gres/gpu:h100=', 'gres/gpu=-1', 'gres/gpu'):
            with self.subTest(tres=malformed), self.assertRaises(SafetyError):
                gpu_count(malformed)

    def test_managed_cli_backoff_priority_and_no_home_writes(self):
        source_root = Path(__file__).resolve().parents[1]
        fake = self.root / 'fake-ssh'
        fake.write_bytes((source_root / 'tests/fixtures/consumer/fake_ssh.py').read_bytes())
        fake.chmod(0o700)
        record = self.root / 'ssh-calls.jsonl'
        for name in ('managed', 'null', 'environment', 'explicit'):
            with self.subTest(priority=name):
                case = self.root / name
                case.mkdir(mode=0o700)
                home = case / 'home'
                home.mkdir(mode=0o700)
                coordinator = Coordinator(case / 'state')
                attempt = coordinator.admit(spec(task=name), LIMITS)
                transport = {'ssh_binary': str(fake)}
                env = {**os.environ, 'HOME': str(home), 'XDG_STATE_HOME': str(case / 'xdg'),
                       'FAKE_SSH_MODE': 'auth', 'FAKE_SSH_RECORD': str(record)}
                env.pop('SHERLOCK_KIT_STATE_ROOT', None)
                expected = coordinator.root / 'auth-backoff.json'
                if name == 'null':
                    transport['backoff_file'] = None
                elif name == 'environment':
                    env['SHERLOCK_KIT_STATE_ROOT'] = str(case / 'override')
                    expected = case / 'override/auth-backoff.json'
                elif name == 'explicit':
                    selected = case / 'explicit'
                    selected.mkdir(mode=0o700)
                    expected = selected / 'selected.json'
                    transport['backoff_file'] = str(expected)
                    # A lower-priority invalid override must not defeat an explicit file.
                    env['SHERLOCK_KIT_STATE_ROOT'] = 'invalid-relative-root'
                config = case / 'config.json'
                config.write_text(json.dumps(dict(schema_version=1, state_root=str(coordinator.root), principal='fixture', cluster='sherlock', limits=LIMITS, transport=transport)))
                config.chmod(0o600)
                result = subprocess.run([sys.executable, str(source_root / 'src/sherlock_kit.py'), 'status', '--config', str(config), '--attempt', attempt['id']],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)['transport'], 'auth_required')
                self.assertTrue(expected.is_file())
                self.assertEqual(expected.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(home.iterdir()), [])
                self.assertFalse((case / 'xdg').exists())
                if name in {'environment', 'explicit'}:
                    self.assertFalse((coordinator.root / 'auth-backoff.json').exists())
        self.assertEqual(len(record.read_text().splitlines()), 4)

    def test_invalid_env_override_rejects_cli_before_coordinator_creation(self):
        source_root = Path(__file__).resolve().parents[1]
        home = self.root / 'empty-home'
        home.mkdir(mode=0o700)
        state = self.root / 'uncreated-state'
        config = self.root / 'private.json'
        config.write_text(json.dumps(dict(schema_version=1, state_root=str(state), principal='fixture', cluster='sherlock', limits=LIMITS, transport={})))
        config.chmod(0o600)
        for override in ('', 'relative-root'):
            with self.subTest(override=override):
                env = {**os.environ, 'HOME': str(home), 'XDG_STATE_HOME': str(self.root / 'uncreated-xdg'), 'SHERLOCK_KIT_STATE_ROOT': override}
                result = subprocess.run([sys.executable, str(source_root / 'src/sherlock_kit.py'), 'status', '--local', '--config', str(config), '--attempt', self.attempt['id']],
                                        env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertIn('SHERLOCK_KIT_STATE_ROOT', result.stderr)
                self.assertFalse(state.exists())
                self.assertFalse((self.root / 'uncreated-xdg').exists())
                self.assertEqual(list(home.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
