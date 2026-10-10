"""Workstation CLI against the registry program executed locally (tests/fake_remote.py)."""
from contextlib import ExitStack
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(1, str(Path(__file__).resolve().parent))
import sherlock_commands
from sherlock_commands import (OPTIONAL_CONFIG, PREEMPTION_MESSAGE, QUERY_SENTINEL, REQUIRED_CONFIG, cached_query, gpu_count, installed_for_admission,
                               load_validator, private_config, transport_options)
from sherlock_orchestration import IDENTITY_KEYS, SafetyError, canonical, digest, sbatch_options
from sherlock_partitions import partition_profile, partitions_sha256
from sherlock_artifacts import TransferError, build_manifest, read_attempt_sidecar
from sherlock_kit import RemoteResult, TransportConfig, _backoff_lock, query_cache_path, state_lock
from sherlock_registry import program_sha256
import fake_remote
from fake_remote import (CLUSTER, CONTROL_HOST, CREATED, PRINCIPAL, PROBE, SCRIPT, SQUEUE_FORMAT, FakeRemote, config_file, expire_queries, frozen, hex32,
                         pending_aggregate, query_cache, query_keys, run_main, sacct_line, seed_attempt, spec, squeue_line, stamp, stamped_record,
                         submitted_event, task_line)

SOURCE_ROOT = Path(__file__).resolve().parents[1]
EXPLICIT_ROOT_MESSAGE = 'typed commands need an explicit local state location: set transport.backoff_file or SHERLOCK_KIT_STATE_ROOT'
CACHE_MESSAGE = 'query cache malformed or unsafe; remove query-cache.json explicitly'


def frozen_identity(**overrides):
    return {'schema_version': 1, 'code_revision': 'c' * 40, 'policy_sha256': spec().policy_digest, 'install_mode': 'frozen',
            'partitions_sha256': partitions_sha256(), **overrides}


class CommandCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = self.root / 'registry'
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.remote = FakeRemote(self.root)
        self.counter = 0

    def config(self, **extra):
        return config_file(self.root, self.registry, self.state, **extra)

    def run_main(self, arguments, remote=None):
        return run_main(arguments, self.remote if remote is None else remote)

    def new_id(self):
        self.counter += 1
        return hex32(0xd000 + self.counter)

    def seed(self, attempt_id=None, created=CREATED, events=(), marker=True, **overrides):
        attempt_id = attempt_id or self.new_id()
        return seed_attempt(self.registry, stamped_record(attempt_id, frozen(**overrides), created), events, marker)

    def read_key(self, *suffix):
        return canonical([CLUSTER, PRINCIPAL, CONTROL_HOST, *suffix])

    def files(self, attempt_id):
        directory = self.registry / 'attempts' / attempt_id
        return sorted(path.name for path in directory.iterdir()) if directory.exists() else None


class ConfigTests(CommandCase):
    def test_config_key_sets_and_registry_root_validation(self):
        self.assertEqual(REQUIRED_CONFIG, {'schema_version', 'registry_root', 'principal', 'cluster', 'transport'})
        self.assertEqual(OPTIONAL_CONFIG, {'fetch_root', 'validator_roots', 'remote_roots', 'grant'})
        config = self.root / 'private.json'
        base = dict(schema_version=1, registry_root='/synthetic/registry', principal=PRINCIPAL, cluster=CLUSTER, transport={'backoff_file': str(self.state / 'b.json')})
        config.write_text(json.dumps({**base, 'state_root': '/synthetic/state', 'limits': {'cpus': 1}}))
        config.chmod(0o600)
        message = r'^unknown private config field\(s\): limits, state_root; filesystem and partition access are not configurable$'
        with self.assertRaisesRegex(SafetyError, message):
            private_config(config)
        code, output, err = self.run_main(['status', '--config', str(config), '--attempt', hex32(1)], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('shk: unknown private config field(s): limits, state_root; filesystem and partition access are not configurable', err)
        self.assertEqual(list(self.state.iterdir()), [])
        self.assertFalse(self.registry.exists())
        config.write_text(json.dumps({**base, 'fetch_root': '/synthetic/results', 'validator_roots': [], 'remote_roots': {}, 'grant': None}))
        self.assertEqual(private_config(config)['registry_root'], '/synthetic/registry')
        config.write_text(json.dumps({key: value for key, value in base.items() if key != 'registry_root'}))
        with self.assertRaisesRegex(SafetyError, 'schema mismatch'):
            private_config(config)
        for wrong in ('relative/registry', '', 7, None, '/registry\n', '/regis\x00try'):
            with self.subTest(registry_root=wrong):
                config.write_text(json.dumps({**base, 'registry_root': wrong}))
                with self.assertRaisesRegex(SafetyError, 'registry_root must be an absolute POSIX path without control characters'):
                    private_config(config)
        for wrong in ('/a/../b', '/a/./b', '/a//b', '/a/b/', '/a/b/..', '//a', '//a/b'):
            with self.subTest(registry_root=wrong):
                config.write_text(json.dumps({**base, 'registry_root': wrong}))
                with self.assertRaisesRegex(SafetyError, r'registry_root must be canonical: no \., \.\. or repeated/trailing slashes'):
                    private_config(config)
        config.chmod(0o640)
        with self.assertRaisesRegex(SafetyError, '0600'):
            private_config(config)

    def test_typed_commands_need_an_explicit_local_state_root(self):
        with patch.dict(os.environ):
            os.environ.pop('SHERLOCK_KIT_STATE_ROOT', None)
            for transport in ({}, {'backoff_file': None}):
                with self.subTest(transport=transport):
                    with self.assertRaisesRegex(SafetyError, '^' + EXPLICIT_ROOT_MESSAGE.replace('.', r'\.') + '$'):
                        transport_options({'transport': transport})
                    config = self.config(transport=transport)
                    code, output, err = self.run_main(['status', '--config', config, '--attempt', hex32(1)], AssertionError('network ran'))
                    self.assertEqual((code, output), (2, None))
                    self.assertEqual(err, 'shk: ' + EXPLICIT_ROOT_MESSAGE + '\n')
            self.assertEqual(transport_options({'transport': {'backoff_file': '/x/b.json', 'control_host': 'alias'}}), {'backoff_file': '/x/b.json', 'control_host': 'alias'})
            os.environ['SHERLOCK_KIT_STATE_ROOT'] = str(self.state)
            self.assertEqual(transport_options({'transport': {}}), {})
        self.assertEqual(list(self.state.iterdir()), [])


class SubmitTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.script = self.root / 'job.sh'
        self.script.write_bytes(SCRIPT)
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.identity = frozen_identity()

    def spec_file(self, **overrides):
        self.counter += 1
        body = spec(**{'remote_script': str(self.script), 'script_digest': hashlib.sha256(SCRIPT).hexdigest(), 'remote_run_directory': str(self.run_dir), **overrides})
        path = self.root / f'spec-{self.counter}.json'
        path.write_text(json.dumps(dataclasses.asdict(body)))
        return str(path), body

    def submit(self, arguments, remote=None):
        with ExitStack() as stack:
            stack.enter_context(patch('sherlock_kit.policy_identity', return_value=self.identity))
            stack.enter_context(patch.dict(os.environ))
            os.environ.pop('SHERLOCK_KIT_PIN', None)
            return self.run_main(arguments, remote)

    def test_preview_is_offline_and_carries_array_resources(self):
        path, body = self.spec_file(resources={**spec().resources, 'array': {'count': 4, 'throttle': 2}})
        code, output, err = self.submit(['submit', '--config', self.config(), '--spec', path], AssertionError('network ran'))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(output['operation'], 'preview')
        self.assertEqual(output['spec_digest'], digest(body.__dict__))
        self.assertEqual(output['resources']['array'], {'count': 4, 'throttle': 2})
        self.assertIs(output['resources']['requeue'], False)
        self.assertFalse(self.registry.exists())
        self.assertEqual(list(self.state.iterdir()), [])
        path, _ = self.spec_file(principal='other')
        code, output, err = self.submit(['submit', '--config', self.config(), '--spec', path], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('differs from private controller config', err)

    def test_apply_submits_through_one_runner_call_with_fresh_ids(self):
        path, body = self.spec_file(resources={**spec().resources, 'array': {'count': 4, 'throttle': 2}})
        config = self.config()
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual((code, err), (0, ''), err)
        attempt = output['attempt']['id']
        self.assertRegex(attempt, '^[0-9a-f]{32}$')
        self.assertEqual((output['operation'], output['resolution'], output['job_id'], output['recorded'], output['transport']), ('submit', 'submitted', '777', True, 'complete'))
        self.assertEqual(output['attempt']['registry_root'], str(self.registry))
        frozen_spec = output['attempt']['spec']
        self.assertEqual((frozen_spec['toolkit_revision'], frozen_spec['partition_profile'], frozen_spec['grant']), ('c' * 40, dict(partition_profile('normal')), None))
        self.assertEqual(frozen_spec['resources']['array'], {'count': 4, 'throttle': 2})
        self.assertEqual([argv for argv, _ in self.remote.calls][0], PROBE)
        self.assertEqual([mutation for _, mutation in self.remote.calls], [False, True])
        runner, = self.remote.program_calls('submit')
        self.assertEqual((runner[3], runner[4], runner[6]), ('submit', str(self.registry), program_sha256()))
        record = json.loads(runner[5])
        self.assertEqual(canonical(record), runner[5])
        self.assertEqual(set(record), {'schema_version', 'kind', 'attempt', 'key', 'job_name', 'spec', 'sbatch_options'})
        self.assertEqual((record['kind'], record['attempt'], record['job_name'], record['spec']), ('record', attempt, 'shk-' + attempt, frozen_spec))
        self.assertEqual(record['sbatch_options'], sbatch_options(attempt, frozen_spec))
        self.assertIn('--array=0-3%2', record['sbatch_options'])
        self.assertIn('--output=' + str(self.run_dir) + '/slurm-%A_%a.out', record['sbatch_options'])
        self.assertEqual(self.files(attempt), ['record.json', 'submitted.json'])
        stored = json.loads((self.registry / 'attempts' / attempt / 'record.json').read_text())
        self.assertEqual((stored['spec'], stored['program_sha256']), (frozen_spec, program_sha256()))
        self.assertEqual(json.loads((self.registry / 'attempts' / attempt / 'submitted.json').read_text())['job_id'], '777')
        self.assertEqual((self.registry / 'tasks' / record['key']).read_text().strip(), attempt)
        sbatch, = self.remote.tool_calls('sbatch')
        self.assertEqual(sbatch['argv'], record['sbatch_options'][1:])
        self.assertEqual(sbatch['stdin'].encode('latin-1'), SCRIPT)
        self.assertEqual(list(self.state.iterdir()), [])
        # A second logical task gets a fresh id and its own single runner call.
        other, _ = self.spec_file(task='second')
        code, second, err = self.submit(['submit', '--config', config, '--spec', other, '--apply'])
        self.assertEqual((code, second['resolution']), (0, 'submitted'))
        self.assertNotEqual(second['attempt']['id'], attempt)
        self.assertEqual(len(self.remote.program_calls('submit')), 2)
        # The same logical task again is a refusal translated for the operator; its id is discarded.
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual((code, output), (2, None))
        self.assertIn('shk: logical task already has attempt ' + attempt, err)
        self.assertEqual(sorted(path.name for path in (self.registry / 'attempts').iterdir()), sorted([attempt, second['attempt']['id']]))

    def test_apply_outcomes_exit_codes_and_recorded(self):
        config = self.config()
        path, _ = self.spec_file(task='unknown-rc')
        self.remote.fake.update(sbatch_stdout='', sbatch_stderr='sbatch: error: Batch job submission failed\n', sbatch_rc='1')
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual(code, 2)
        self.assertEqual((output['resolution'], output['job_id'], output['recorded'], output['reason']), ('unknown', None, True, '1'))
        self.assertEqual(output['hint'], 'shk status --attempt ' + output['attempt']['id'])
        self.assertIn('shk status --attempt', err)
        self.assertEqual(self.files(output['attempt']['id']), ['record.json', 'submitted.json'])
        self.remote.fake = {'sbatch_stdout': '777\n'}
        self.remote.remove_tool('sbatch')
        path, _ = self.spec_file(task='no-sbatch')
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual((code, err), (1, ''))
        self.assertEqual((output['resolution'], output['recorded'], output['reason'], output['job_id']), ('not_sent', True, 'sbatch_unavailable', None))
        self.assertEqual(self.files(output['attempt']['id']), ['not_sent.json', 'record.json'])
        path, _ = self.spec_file(task='transport-not-sent')
        self.remote.fail('submit', RemoteResult('not_sent', stderr='Shared authentication state unavailable'))
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual((code, err), (1, ''))
        self.assertEqual((output['resolution'], output['recorded'], output['transport'], output['job_id']), ('not_sent', False, 'not_sent', None))
        self.assertIsNone(self.files(output['attempt']['id']))
        # The shared cooldown answers before ssh starts (dispatched False): nothing reached Slurm, so no status hint.
        self.remote.fail('submit', RemoteResult('auth_required', stderr='Shared authentication cooldown active; authenticate manually'))
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual((code, err), (1, ''))
        self.assertEqual((output['resolution'], output['recorded'], output['transport'], output['job_id']), ('not_sent', False, 'auth_required', None))
        self.assertEqual(output['reason'], 'Shared authentication cooldown active; authenticate manually')
        self.assertNotIn('hint', output)
        self.assertIsNone(self.files(output['attempt']['id']))
        self.remote.fail('submit', RemoteResult('unknown', stderr='Connection closed', returncode=255, dispatched=True))
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual(code, 2)
        self.assertEqual((output['resolution'], output['recorded'], output['transport']), ('unknown', None, 'unknown'))
        self.assertIn('shk status --attempt ' + output['attempt']['id'], err)
        self.remote.fail('submit', RemoteResult('complete', stdout='SHK_SUBMITTED:' + hex32(1) + ':5\n', returncode=0, dispatched=True))
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual((code, output['resolution'], output['reason']), (2, 'unknown', 'malformed_reply'))
        self.remote.fail('submit', RemoteResult('complete', stdout='', returncode=0, dispatched=True))
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual((code, output['resolution'], output['reason']), (2, 'unknown', 'malformed_reply'))
        self.assertEqual([argv[3] for argv in self.remote.program_calls()], ['submit'] * 7)

    def test_apply_gates_precede_the_runner(self):
        config = self.config()
        path, _ = self.spec_file(remote_run_directory=None)
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('explicit isolated remote_run_directory', err)
        path, _ = self.spec_file(policy_digest='b' * 64)
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('differs from installed policy', err)
        path, _ = self.spec_file()
        self.remote.fail('probe', RemoteResult('complete', stdout='someone-else\n', returncode=0, dispatched=True))
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual(code, 2)
        self.assertIn('authenticated principal not established', err)
        self.remote.fail('probe', RemoteResult('auth_required', stderr='denied', returncode=255, dispatched=True))
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual(code, 2)
        self.assertIn('authenticated principal not established', err)
        self.assertEqual(self.remote.program_calls(), [])
        self.remote.failures.clear()
        with patch.object(sherlock_commands, 'REMOTE_ARGV_LIMIT', 1000):
            code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'])
        self.assertEqual((code, output), (2, None))
        self.assertIn('exceeds', err)
        self.assertEqual(self.remote.program_calls(), [])
        self.assertFalse(self.registry.exists())
        self.identity['install_mode'] = 'development'
        code, output, err = self.submit(['submit', '--config', config, '--spec', path, '--apply'], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('frozen installed toolkit', err)

    def test_refusal_tokens_are_translated_and_unknown_tokens_pass_through(self):
        for token, needle in (('duplicate_logical_task:' + hex32(3), 'logical task already has attempt ' + hex32(3)),
                              ('parent_not_released:identified', 'parent attempt is not released (identified)'),
                              ('principal_mismatch', 'login principal differs'), ('registry_busy', 'registry is busy'),
                              ('digest_mismatch', 'script bytes on Sherlock differ'), ('made_up_token', 'made_up_token')):
            with self.subTest(token=token):
                self.assertIn(needle, str(sherlock_commands.refusal_error(token)))


class StatusTests(CommandCase):
    def test_status_reuses_one_read_within_cadence_and_keys_the_cache(self):
        record = self.seed(events=[submitted_event()])
        attempt = record['attempt']
        self.remote.fake['sacct_stdout'] = sacct_line(attempt, State='RUNNING', End='Unknown') + '\n'
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['job_id'], output['attempt'], output['events'], output['scientific_validation']),
                         ('identified', '123', record, ['submitted'], 'unverified'))
        self.assertEqual((output['tasks']['running'], output['cost']['known'], output['anomalies']), (1, False, []))
        reader, = self.remote.program_calls()
        self.assertEqual(reader[3:], ['read', str(self.registry), '--attempt', attempt])
        sacct, = self.remote.tool_calls('sacct')
        self.assertEqual(sacct['argv'][-1], '--name=shk-' + attempt)
        self.assertIn('--starttime=' + stamp(CREATED - 300), sacct['argv'])
        self.assertEqual(len(sacct['argv'][-2].split(',')), 15)
        code, again, err = self.run_main(['status', '--config', config, '--attempt', attempt], AssertionError('cadence violated'))
        self.assertEqual((code, again), (0, output))
        self.assertEqual(query_keys(self.state), [self.read_key('read', attempt)])
        self.assertEqual(stat.S_IMODE((self.state / 'query-cache.json').stat().st_mode), 0o600)
        self.assertTrue((self.state / 'query-cache.json.lock').is_file())
        expire_queries(self.state)
        self.remote.fake['sacct_stdout'] = sacct_line(attempt) + '\n'
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['cost']), (0, 'terminal', {'cpu_seconds': 60, 'gpu_seconds': 0, 'known': True}))
        self.assertEqual(len(self.remote.program_calls()), 2)
        # status never writes resolved.json; reconcile does.
        self.assertEqual(self.files(attempt), ['record.json', 'submitted.json'])
        # Ownership is decided against the record on Sherlock, so the refusal needs the read; it writes nothing.
        code, output, err = self.run_main(['status', '--config', self.config(principal='other'), '--attempt', attempt])
        self.assertEqual(code, 2)
        self.assertIn('another principal', err)
        code, output, err = self.run_main(['status', '--config', config, '--attempt', hex32(0x999)])
        self.assertEqual((code, output), (2, None))
        self.assertIn('unknown attempt', err)

    def test_reconcile_all_runs_one_read_and_one_resolved_batch(self):
        done = self.seed(created=CREATED, events=[submitted_event('101')], task='a')
        running = self.seed(created=CREATED + 5, events=[submitted_event('102')], task='b')
        self.remote.fake['sacct_stdout'] = '\n'.join((sacct_line(done['attempt'], JobID='101', JobIDRaw='101'),
                                                      sacct_line(running['attempt'], CREATED + 5, JobID='102', JobIDRaw='102', State='RUNNING', End='Unknown', DBIndex='43'))) + '\n'
        config = self.config()
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual([(item['attempt']['attempt'], item['resolution']) for item in output], [(done['attempt'], 'terminal'), (running['attempt'], 'identified')])
        self.assertEqual(output[0]['events'], ['submitted', 'resolved'])
        self.assertEqual(output[1]['events'], ['submitted'])
        reader, event = self.remote.program_calls()
        self.assertEqual(reader[3:], ['read', str(self.registry), '--open'])
        self.assertEqual(event[3:], ['event', str(self.registry), 'resolved', '{}', done['attempt']])
        self.assertEqual([mutation for _, mutation in self.remote.calls], [False, True])
        sacct = self.remote.tool_calls('sacct')
        self.assertEqual(len(sacct), 2)
        self.assertEqual(sacct[0]['argv'][-1], '--name=shk-' + done['attempt'] + ',shk-' + running['attempt'])
        self.assertIn('--starttime=' + stamp(CREATED - 300), sacct[0]['argv'])
        self.assertEqual(self.files(done['attempt']), ['record.json', 'resolved.json', 'submitted.json'])
        resolved = json.loads((self.registry / 'attempts' / done['attempt'] / 'resolved.json').read_text())
        self.assertEqual((resolved['resolution'], resolved['job_id'], resolved['cost']['known']), ('terminal', '101', True))
        # The written closure forgets the cached listing, so the next --all within the cadence reads afresh and the closed attempt is gone.
        self.assertEqual(query_keys(self.state), [])
        code, output, err = self.run_main(['status', '--all', '--config', config])
        self.assertEqual((code, [item['attempt']['attempt'] for item in output]), (0, [running['attempt']]))
        self.assertEqual(query_keys(self.state), [self.read_key('read-all')])
        code, cached, err = self.run_main(['status', '--all', '--config', config], AssertionError('cadence violated'))
        self.assertEqual((code, cached), (0, output))
        self.assertEqual((len(self.remote.program_calls('read')), len(self.remote.program_calls('event'))), (2, 1))

    def test_reconcile_all_isolates_errors_and_resolved_refusals_are_not_fatal(self):
        done = self.seed(events=[submitted_event('101')], task='a')
        foreign = self.seed(created=CREATED + 1, events=[submitted_event('102')], task='b')
        self.remote.fake['sacct_stdout'] = '\n'.join((sacct_line(done['attempt'], JobID='101', JobIDRaw='101'),
                                                      sacct_line(foreign['attempt'], CREATED + 1, JobID='102', JobIDRaw='102', User='someone-else', DBIndex='43'))) + '\n'
        config = self.config()
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual(code, 2)
        self.assertEqual([item['resolution'] for item in output], ['terminal', 'error'])
        self.assertEqual((output[1]['error'], output[1]['reason']), ('ownership_conflict', 'ownership_conflict'))
        self.assertIn('1 attempt(s) could not be reconciled', err)
        self.assertEqual(self.files(done['attempt']), ['record.json', 'resolved.json', 'submitted.json'])
        self.assertEqual(self.files(foreign['attempt']), ['record.json', 'submitted.json'])
        # A written closure forgets the cached read: the next run reads afresh, no longer lists the closed attempt and sends no event.
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual(code, 2)
        self.assertEqual([item['resolution'] for item in output], ['error'])
        self.assertEqual((len(self.remote.program_calls('read')), len(self.remote.program_calls('event'))), (2, 1))
        # A refusal is non-fatal and keeps the cached read, so the next run within the cadence repeats the event call.
        late = self.seed(created=CREATED + 2, events=[submitted_event('103')], task='c')
        self.remote.fake['sacct_stdout'] += sacct_line(late['attempt'], CREATED + 2, JobID='103', JobIDRaw='103', DBIndex='44') + '\n'
        expire_queries(self.state)
        self.remote.fail('event', RemoteResult('complete', stdout=f'SHK_EVENT_REFUSED:{late["attempt"]}:already_present\n', returncode=0, dispatched=True))
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, [item['resolution'] for item in output]), (2, ['error', 'terminal']))
        self.assertEqual((output[1]['resolved_event'], output[1]['events']), ('refused:already_present', ['submitted']))
        self.remote.fail('event', RemoteResult('unknown', stderr='lost', returncode=255, dispatched=True))
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, output[1]['resolved_event'], output[1]['resolution']), (2, 'transport:unknown', 'terminal'))
        self.assertEqual((len(self.remote.program_calls('read')), len(self.remote.program_calls('event'))), (3, 3))

    def test_reconcile_one_writes_resolved_and_forgets_the_cached_read(self):
        record = self.seed(events=[submitted_event('123')])
        attempt = record['attempt']
        self.remote.fake['sacct_stdout'] = sacct_line(attempt) + '\n'
        config = self.config()
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['events'], output['cost']['known']), ('terminal', ['submitted', 'resolved'], True))
        read, event = self.remote.program_calls()
        self.assertEqual((read[3], event[3:8]), ('read', ['event', str(self.registry), 'resolved', '{}', attempt]))
        self.assertEqual(self.files(attempt), ['record.json', 'resolved.json', 'submitted.json'])
        # Within the cadence, status reads afresh (the closure changed the attempt) and sees the event on Sherlock.
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['events']), (0, 'terminal', ['resolved', 'submitted']))
        self.assertEqual(len(self.remote.program_calls('read')), 2)
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt], AssertionError('cadence violated'))
        self.assertEqual((code, output['events']), (0, ['resolved', 'submitted']))
        self.assertEqual(len(self.remote.program_calls('event')), 1)

    def test_preemption_keeps_the_error_and_abandonable_notes(self):
        preempted = self.seed(events=[submitted_event('101')], task='a')
        foreign = self.seed(created=CREATED + 1, events=[submitted_event('102')], task='b')
        lost = self.seed(created=CREATED + 2, events=[submitted_event(None)], task='c')
        self.remote.fake['sacct_stdout'] = '\n'.join((sacct_line(preempted['attempt'], JobID='101', JobIDRaw='101', State='RUNNING', Restarts='1', End='Unknown'),
                                                      sacct_line(foreign['attempt'], CREATED + 1, JobID='102', JobIDRaw='102', User='someone-else', DBIndex='43'))) + '\n'
        code, output, err = self.run_main(['status', '--all', '--config', self.config()])
        self.assertEqual((code, [item['resolution'] for item in output]), (2, ['unexpected_preemption', 'error', 'abandonable']))
        self.assertEqual(err, 'shk: 1 attempt(s) could not be reconciled; see the "error" entries\n'
                              f'shk: {sherlock_commands.abandon_hint(1)}\nshk: {PREEMPTION_MESSAGE}\n')
        self.assertEqual(self.remote.program_calls('event'), [])

    def test_status_all_with_nothing_open_reads_once_and_runs_no_sacct(self):
        self.seed(events=[('not_sent.json', {'at': 1, 'reason': 'claim_failed', 'detail': ''})], marker=False)
        config = self.config()
        code, output, err = self.run_main(['status', '--all', '--config', config])
        self.assertEqual((code, output, err), (0, [], ''))
        self.assertEqual(len(self.remote.program_calls('read')), 1)
        self.assertEqual(self.remote.tool_calls('sacct'), [])
        shutil.rmtree(self.registry)
        expire_queries(self.state)
        code, output, err = self.run_main(['status', '--all', '--config', config])
        self.assertEqual((code, output, err), (0, [], ''))

    def test_reader_transport_failure_is_inconclusive_and_the_sentinel_holds(self):
        record = self.seed(events=[submitted_event()])
        config = self.config()
        denied = RemoteResult('auth_required', stderr='denied', returncode=255, dispatched=True)
        self.remote.fail('read', denied)
        code, output, err = self.run_main(['status', '--config', config, '--attempt', record['attempt']])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(output, {'attempt': {'attempt': record['attempt']}, 'resolution': 'inconclusive', 'transport': 'auth_required', 'scientific_validation': 'unverified'})
        # As before, a transport failure is a cached result for the cadence: no second call within 60 s.
        code, output, err = self.run_main(['status', '--config', config, '--attempt', record['attempt']], AssertionError('cadence violated'))
        self.assertEqual((code, output['resolution'], output['transport']), (0, 'inconclusive', 'auth_required'))
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(output, [{'attempt': None, 'resolution': 'inconclusive', 'transport': 'auth_required', 'scientific_validation': 'unverified'}])
        self.assertEqual(self.remote.program_calls('event'), [])
        # A sacct failure inside the reader is reported the same way, never as an attempt state.
        self.remote.failures.clear()
        expire_queries(self.state)
        self.remote.fake.update(sacct_rc='1', sacct_stderr='sacct: error: slurmdbd down\n')
        code, output, err = self.run_main(['status', '--config', config, '--attempt', record['attempt']])
        self.assertEqual((code, output['resolution'], output['transport'], output['attempt']), (0, 'inconclusive', 'sacct:failed', record))
        self.remote.fail('read', RemoteResult('complete', stdout='not json', returncode=0, dispatched=True))
        expire_queries(self.state)
        code, output, err = self.run_main(['status', '--config', config, '--attempt', record['attempt']])
        self.assertEqual((code, output), (2, None))
        self.assertIn('malformed reader output', err)

    def test_closing_events_short_circuit_rows(self):
        record = self.seed(events=[submitted_event(None), ('abandoned.json', {'at': 1, 'age_seconds': 1000, 'checks': {}, 'note': ''})])
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', record['attempt']])
        self.assertEqual((code, output['resolution'], output['events'], err), (0, 'abandoned', ['abandoned', 'submitted'], ''))
        self.remote.fake['sacct_stdout'] = sacct_line(record['attempt']) + '\n'
        expire_queries(self.state)
        code, output, err = self.run_main(['status', '--config', config, '--attempt', record['attempt']])
        self.assertEqual((code, output['resolution'], output['error']), (2, 'error', 'rows_for_abandoned_attempt'))
        code, output, err = self.run_main(['status', '--all', '--config', config])
        self.assertEqual((code, output), (0, []))
        # A closing event needs no rows: a sacct outage leaves the closed attempt closed, annotated with the outage.
        expire_queries(self.state)
        self.remote.fake.update(sacct_rc='1', sacct_stderr='sacct: error: slurmdbd down\n')
        code, output, err = self.run_main(['status', '--config', config, '--attempt', record['attempt']])
        self.assertEqual((code, output['resolution'], output['transport'], output['events'], err), (0, 'abandoned', 'sacct:failed', ['abandoned', 'submitted'], ''))
        open_attempt = self.seed(events=[submitted_event('124')], task='open')
        code, output, err = self.run_main(['status', '--config', config, '--attempt', open_attempt['attempt']])
        self.assertEqual((code, output['resolution'], output['transport']), (0, 'inconclusive', 'sacct:failed'))

    def test_unreadable_record_is_an_isolated_error_and_refuses_events(self):
        record = self.seed(events=[submitted_event('123')])
        attempt = record['attempt']
        (self.registry / 'attempts' / attempt / 'record.json').write_bytes(b'not json\n')
        self.remote.fake['sacct_stdout'] = sacct_line(attempt, State='RUNNING', Restarts='1', End='Unknown') + '\n'
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['attempt'], output['events']), (2, 'error', {'attempt': attempt}, ['submitted']))
        self.assertTrue(output['error'].startswith('registry_entry_errors:record.json:'), output['error'])
        self.assertIn('1 attempt(s) could not be reconciled', err)
        for flag in ('--acknowledge-preemption', '--abandon'):
            with self.subTest(flag=flag):
                code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, flag], AssertionError('cadence violated'))
                self.assertEqual((code, output), (2, None))
                self.assertIn('record', err)
                self.assertNotIn('NoneType', err)
        self.assertEqual(self.remote.program_calls('event'), [])
        self.assertEqual(self.files(attempt), ['record.json', 'submitted.json'])

    def test_array_status_counts_pending_aggregate_then_terminal_cost(self):
        record = self.seed(events=[submitted_event('500')], resources={**spec().resources, 'array': {'count': 4, 'throttle': 2}})
        attempt = record['attempt']
        self.remote.fake['sacct_stdout'] = '\n'.join((task_line(attempt, 0, 50, State='RUNNING', End='Unknown'), task_line(attempt, 1, 51, State='RUNNING', End='Unknown'),
                                                      pending_aggregate(attempt, '2-3%2'))) + '\n'
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['job_id']), (0, 'identified', '500'))
        self.assertEqual(output['tasks'], {'count': 4, 'terminal': 0, 'running': 2, 'pending': 2, 'missing': 0, 'by_state': {'PENDING': 2, 'RUNNING': 2}, 'incomplete_history': []})
        expire_queries(self.state)
        self.remote.fake['sacct_stdout'] = '\n'.join(task_line(attempt, index, 50 + index) for index in range(4)) + '\n'
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['tasks']['terminal'], output['cost']), (0, 'terminal', 4, {'cpu_seconds': 240, 'gpu_seconds': 0, 'known': True}))


class EventTests(CommandCase):
    def test_abandonable_then_abandon_flow(self):
        record = self.seed(events=[submitted_event(None)])
        attempt = record['attempt']
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['job_id']), (0, 'abandonable', None))
        self.assertIn('--abandon', err)
        self.assertTrue(err.startswith('shk: '))
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--abandon', '--note', 'lost reply'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['operation'], output['outcome'], output['event'], output['attempt'], output['note']), ('abandon', 'written', 'abandoned.json', record, 'lost reply'))
        read, event = self.remote.program_calls()
        self.assertEqual(read[3], 'read')
        self.assertEqual((event[3:6], json.loads(event[6]), event[7:]), (['event', str(self.registry), 'abandon'], {'note': 'lost reply'}, [attempt]))
        self.assertEqual([mutation for _, mutation in self.remote.calls], [False, True])
        abandoned = json.loads((self.registry / 'attempts' / attempt / 'abandoned.json').read_text())
        self.assertEqual((abandoned['note'], abandoned['checks']['squeue_rows']), ('lost reply', 0))
        expire_queries(self.state)
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['events'], err), (0, 'abandoned', ['abandoned', 'submitted'], ''))
        code, output, err = self.run_main(['status', '--all', '--config', config])
        self.assertEqual((code, output), (0, []))

    def test_abandon_refusals_local_then_remote(self):
        config = self.config()
        cases = {'fresh': self.seed(created=fake_remote.now() - 10, events=[submitted_event(None)], task='fresh'),
                 'known_job': self.seed(events=[submitted_event('123')], task='known'),
                 'rows': self.seed(events=[submitted_event(None)], task='rows')}
        for name, record in cases.items():
            with self.subTest(case=name):
                self.remote.fake['sacct_stdout'] = sacct_line(record['attempt'], State='RUNNING', End='Unknown') + '\n' if name == 'rows' else ''
                code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', record['attempt'], '--abandon'])
                self.assertEqual((code, output), (2, None))
                self.assertIn('not abandonable', err)
                self.assertEqual(self.remote.program_calls('event'), [])
                self.assertEqual(self.files(record['attempt']), ['record.json', 'submitted.json'])
        old = self.seed(events=[submitted_event(None)], task='old')
        self.remote.fake['sacct_stdout'] = ''
        self.remote.fake['squeue_stdout'] = '4242\n'
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', old['attempt'], '--abandon'])
        self.assertEqual((code, output), (2, None))
        self.assertIn('squeue_shows_job', err)
        self.assertEqual(len(self.remote.program_calls('event')), 1)
        self.assertEqual(self.files(old['attempt']), ['record.json', 'submitted.json'])
        self.remote.fail('event', RemoteResult('unknown', stderr='lost', returncode=255, dispatched=True))
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', old['attempt'], '--abandon'])
        self.assertEqual(code, 2)
        self.assertEqual((output['outcome'], output['transport'], output['hint']), ('unknown', 'unknown', 'shk status --attempt ' + old['attempt']))
        self.assertIn('shk status --attempt', err)
        self.remote.fail('event', RemoteResult('not_sent', stderr='cooldown'))
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', old['attempt'], '--abandon'])
        self.assertEqual((code, output['outcome'], output['transport']), (1, 'not_sent', 'not_sent'))
        # The shared cooldown answers before ssh starts: nothing was sent, so no status hint and exit 1.
        self.remote.fail('event', RemoteResult('auth_required', stderr='Shared authentication cooldown active; authenticate manually'))
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', old['attempt'], '--abandon'])
        self.assertEqual((code, output['outcome'], output['transport'], output['event']), (1, 'not_sent', 'auth_required', None))
        self.assertNotIn('hint', output)
        self.assertEqual(err, 'shk: Shared authentication cooldown active; authenticate manually\n')
        code, output, err = self.run_main(['reconcile', '--all', '--config', config, '--abandon'], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('requires --attempt', err)
        events = len(self.remote.program_calls('event'))
        code, output, err = self.run_main(['reconcile', '--config', self.config(principal='other'), '--attempt', old['attempt'], '--abandon'])
        self.assertEqual(code, 2)
        self.assertIn('another principal', err)
        self.assertEqual(len(self.remote.program_calls('event')), events)

    def test_event_mutations_need_a_fresh_registry_read(self):
        """Ownership and the cheap pre-checks come from the read; without one, nothing is written."""
        old = self.seed(events=[submitted_event(None)])
        attempt = old['attempt']
        denied = RemoteResult('auth_required', stderr='denied', returncode=255, dispatched=True)
        for flag in ('--abandon', '--acknowledge-preemption'):
            for principal in (PRINCIPAL, 'other'):
                with self.subTest(flag=flag, principal=principal):
                    config = self.config(principal=principal)
                    if (self.state / 'query-cache.json').exists():
                        expire_queries(self.state)
                    self.remote.fail('read', denied)
                    code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, flag])
                    self.assertEqual((code, output), (2, None))
                    self.assertIn(f'{flag} needs a fresh registry read', err)
                    self.assertIn('auth_required', err)
                    # The transport failure is the cached result for the cadence: a retry within 60 s is refused without any network use.
                    self.remote.failures.clear()
                    code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, flag], AssertionError('cadence violated'))
                    self.assertEqual((code, output), (2, None))
                    self.assertIn('the last read is auth_required', err)
                    self.assertEqual(self.remote.program_calls('event'), [])
                    self.assertEqual(self.files(attempt), ['record.json', 'submitted.json'])
        # A failed status read (query raised) leaves the cadence sentinel, which blocks the mutation for the same cadence.
        config = self.config()
        expire_queries(self.state)
        self.remote.fail('read', OSError('ssh binary vanished'))
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output), (2, None))
        self.assertIn('ssh binary vanished', err)
        self.remote.failures.clear()
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--abandon'], AssertionError('cadence violated'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('--abandon needs a fresh registry read', err)
        self.assertIn(QUERY_SENTINEL['status'], err)
        self.assertEqual(self.remote.program_calls('event'), [])
        # Once the read succeeds, the same command writes; the written closure forgets the cached read.
        expire_queries(self.state)
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--abandon'])
        self.assertEqual((code, output['outcome'], err), (0, 'written', ''))
        self.assertEqual(len(self.remote.program_calls('event')), 1)
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--abandon'])
        self.assertEqual((code, output), (2, None))
        self.assertIn('not abandonable (resolution abandoned)', err)
        self.assertEqual(len(self.remote.program_calls('event')), 1)

    def test_unexpected_preemption_exits_two_until_acknowledged(self):
        record = self.seed(events=[submitted_event('123')])
        attempt = record['attempt']
        restarted = sacct_line(attempt, State='RUNNING', Restarts='1', End='Unknown') + '\n'
        self.remote.fake['sacct_stdout'] = restarted
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['anomalies']), (2, 'unexpected_preemption', ['restart_gap', 'restart_on_non_preemptible']))
        self.assertEqual(err, 'shk: ' + PREEMPTION_MESSAGE + '\n')
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, [item['resolution'] for item in output], err), (2, ['unexpected_preemption'], 'shk: ' + PREEMPTION_MESSAGE + '\n'))
        self.assertEqual(self.remote.program_calls('event'), [])
        code, output, err = self.run_main(['reconcile', '--config', self.config(principal='other'), '--attempt', attempt, '--acknowledge-preemption'])
        self.assertEqual(code, 2)
        self.assertIn('another principal', err)
        self.assertEqual(self.remote.program_calls('event'), [])
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--acknowledge-preemption', '--note', 'node drained'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['operation'], output['outcome'], output['note']), ('ack', 'written', 'node drained'))
        self.assertRegex(output['event'], r'^ack-[0-9]+\.json$')
        event, = self.remote.program_calls('event')
        self.assertEqual((event[5], json.loads(event[6]), event[7:]), ('ack', {'note': 'node drained'}, [attempt]))
        waiver = json.loads((self.registry / 'attempts' / attempt / output['event']).read_text())
        self.assertEqual(waiver['waived'], {'0': {'restart': 1, 'state': 'RUNNING'}})
        expire_queries(self.state)
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['anomalies'], err), (0, 'identified', ['acknowledged_preemption', 'restart_gap'], ''))
        self.assertEqual(output['events'], ['ack', 'submitted'])
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--acknowledge-preemption'], AssertionError('cadence violated'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('not in unexpected_preemption', err)

    def test_acknowledgement_pre_checks_and_argument_rules(self):
        config = self.config()
        owners = self.seed(events=[submitted_event('123')], task='owners', resources={**spec().resources, 'partition': 'owners'})
        self.remote.fake['sacct_stdout'] = sacct_line(owners['attempt'], State='RUNNING', Restarts='1', End='Unknown') + '\n'
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', owners['attempt'], '--acknowledge-preemption'])
        self.assertEqual((code, output), (2, None))
        self.assertIn('preemptible', err)
        self.assertEqual(self.remote.program_calls('event'), [])
        code, output, err = self.run_main(['reconcile', '--all', '--acknowledge-preemption', '--config', config], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('requires --attempt', err)
        for arguments in (['status', '--config', config, '--attempt', hex32(1), '--local'], ['reconcile', '--config', config, '--all', '--local'],
                          ['status', '--config', config, '--attempt', hex32(1), '--abandon'], ['status', '--config', config, '--attempt', hex32(1), '--acknowledge-preemption'],
                          ['reconcile', '--config', config, '--attempt', hex32(1), '--abandon', '--acknowledge-preemption'],
                          ['status', '--config', config, '--all', '--attempt', hex32(1)], ['status', '--config', config], ['reconcile', '--config', config, '--note', 'x', '--attempt', hex32(1)]):
            with self.subTest(arguments=arguments[1:]):
                code, output, err = self.run_main(arguments, AssertionError('network ran'))
                self.assertEqual((code, output), (2, None))


class QueryCacheTests(CommandCase):
    def test_query_cache_path_and_state_lock_derive_from_the_backoff_file(self):
        self.assertEqual(query_cache_path(TransportConfig(backoff_file=str(self.state / 'auth-backoff.json'))), self.state / 'query-cache.json')
        with patch.dict(os.environ, {'SHERLOCK_KIT_STATE_ROOT': str(self.state)}):
            self.assertEqual(query_cache_path(TransportConfig()), self.state / 'query-cache.json')
        link = self.root / 'link'
        link.symlink_to(self.state, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            query_cache_path(TransportConfig(backoff_file=str(link / 'auth-backoff.json')))
        self.assertIs(_backoff_lock, state_lock)
        with state_lock(self.state / 'query-cache.json'):
            lock = self.state / 'query-cache.json.lock'
            self.assertTrue(lock.is_file())
            self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)
        self.assertFalse((self.state / 'auth-backoff.json.lock').exists())

    def test_cached_query_sentinel_semantics_pruning_and_durability(self):
        path = self.state / 'query-cache.json'
        calls = []

        def query():
            calls.append(1)
            return {'status': 'complete', 'rows': [1]}

        self.assertEqual(cached_query(path, 'k', query, now=1000.0), {'status': 'complete', 'rows': [1]})
        self.assertEqual(cached_query(path, 'k', query, now=1059.0), {'status': 'complete', 'rows': [1]})
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(path.read_text()), {'schema_version': 1, 'entries': {'k': {'observed': 1000.0, 'body': {'status': 'complete', 'rows': [1]}}}})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue((self.state / 'query-cache.json.lock').is_file())
        self.assertFalse((self.state / 'auth-backoff.json.lock').exists())
        cached_query(path, 'k', query, now=1060.0)
        self.assertEqual(len(calls), 2)

        def boom():
            raise OSError('down')

        with self.assertRaises(OSError):
            cached_query(path, 'f', boom, now=2000.0)
        self.assertEqual(cached_query(path, 'f', boom, now=2030.0), QUERY_SENTINEL)
        self.assertEqual(QUERY_SENTINEL, {'status': 'query_pending_or_failed'})
        self.assertEqual(query_cache(self.state)['f'], {'observed': 2000.0, 'body': QUERY_SENTINEL})
        # Expired entries are pruned when the file is next written; the live key survives.
        self.assertEqual(cached_query(path, 'p', lambda: 'fresh', now=5000.0), 'fresh')
        self.assertEqual(query_keys(self.state), ['p'])

        def racing():
            document = json.loads(path.read_text())
            document['entries']['g'] = {'observed': 6001.0, 'body': 'other'}
            path.write_text(json.dumps(document))
            return 'mine'

        self.assertEqual(cached_query(path, 'g', racing, now=6000.0), 'mine')
        self.assertEqual(query_cache(self.state)['g'], {'observed': 6001.0, 'body': 'other'})
        with patch.object(sherlock_commands, 'QUERY_BODY_LIMIT', 16):
            with self.assertRaisesRegex(SafetyError, 'exceeds'):
                cached_query(path, 'big', lambda: {'rows': list(range(100))}, now=7000.0)
        self.assertEqual(query_cache(self.state)['big']['body'], QUERY_SENTINEL)

    def test_forget_queries_drops_only_the_named_keys_and_never_raises_after_a_write(self):
        path = self.state / 'query-cache.json'
        moment = fake_remote.now()
        cached_query(path, 'a', lambda: 1, now=moment)
        cached_query(path, 'b', lambda: 2, now=moment)
        self.assertEqual(sherlock_commands.forget_queries(path, ['a', 'never-cached']), ())
        self.assertEqual(query_keys(self.state), ['b'])
        self.assertEqual(sherlock_commands.forget_queries(path, ['never-cached']), ())
        self.assertEqual(query_keys(self.state), ['b'])
        self.assertEqual(sherlock_commands.forget_queries(self.state / 'absent.json', ['b']), ())
        # A mutation already written on Sherlock must be reported as written even when the local cache is unusable.
        path.write_text('not json')
        path.chmod(0o600)
        notes = sherlock_commands.forget_queries(path, ['b'])
        self.assertEqual(len(notes), 1)
        self.assertIn('query cache not refreshed after the write', notes[0])
        self.assertEqual(path.read_text(), 'not json')

    def test_cached_query_fails_closed_on_malformed_symlink_or_public_file(self):
        path = self.state / 'query-cache.json'
        for text in ('[]', '{"schema_version": 2, "entries": {}}', '{"schema_version": 1}', '{"schema_version": 1, "entries": []}',
                     '{"schema_version": 1, "entries": {"k": {"observed": "x", "body": 1}}}', '{"schema_version": 1, "entries": {"k": {"observed": 1}}}', 'not json'):
            with self.subTest(text=text):
                path.write_text(text)
                path.chmod(0o600)
                with self.assertRaisesRegex(SafetyError, '^' + CACHE_MESSAGE.replace('.', r'\.') + '$'):
                    cached_query(path, 'k', lambda: 1, now=1.0)
                self.assertEqual(path.read_text(), text)
        path.write_text('{"schema_version": 1, "entries": {}}')
        path.chmod(0o644)
        with self.assertRaisesRegex(SafetyError, CACHE_MESSAGE.replace('.', r'\.')):
            cached_query(path, 'k', lambda: 1, now=1.0)
        path.unlink()
        other = self.root / 'elsewhere.json'
        other.write_text('{"schema_version": 1, "entries": {}}')
        path.symlink_to(other)
        with self.assertRaisesRegex(SafetyError, CACHE_MESSAGE.replace('.', r'\.')):
            cached_query(path, 'k', lambda: 1, now=1.0)
        self.assertEqual(other.read_text(), '{"schema_version": 1, "entries": {}}')
        path.unlink()
        path.write_bytes(b'{"schema_version": 1, "entries": {}}' + b' ' * 1024)
        path.chmod(0o600)
        with patch.object(sherlock_commands, 'QUERY_FILE_LIMIT', 1024), self.assertRaisesRegex(SafetyError, CACHE_MESSAGE.replace('.', r'\.')):
            cached_query(path, 'k', lambda: 1, now=1.0)


class FetchTests(CommandCase):
    def fixture(self):
        """A registered attempt with a frozen validator, its bundle under an authorized scope and the fetch arguments."""
        validator = self.root / 'validator.py'
        validator.write_text('def validate(root): return (root / "result.json").read_text() == "42\\n"\n')
        record = self.seed(task='fetch', validator_path=str(validator), validator_digest=hashlib.sha256(validator.read_bytes()).hexdigest(), validator_function='validate',
                           events=[submitted_event('123')])
        scope = self.root / 'authorized'
        source = scope / 'output'
        source.mkdir(parents=True)
        (source / 'result.json').write_text('42\n')
        expected = {key: record['spec'][key] for key in IDENTITY_KEYS}
        expected['attempt'] = record['attempt']
        manifest = build_manifest(source, expected)
        (scope / 'manifest.json').write_text(json.dumps(manifest))
        fetch_root = self.root / 'fetch'
        fetch_root.mkdir()
        config = self.config(fetch_root=str(fetch_root), validator_roots=[str(self.root)], remote_roots={'control': str(scope), 'data': str(scope), 'namespace_verified': True})
        arguments = ['fetch', '--config', config, '--attempt', record['attempt'], '--manifest', str(scope / 'manifest.json'), '--source-root', str(source), '--destination', str(fetch_root / 'verified')]
        return record, source, manifest, fetch_root, arguments

    @staticmethod
    def copying(source):
        def transfer(transport, remote_source, stage, manifest):
            for item in manifest['files']:
                shutil.copy(source / item['path'], Path(stage) / item['path'])
        return transfer

    def test_remote_fetch_pins_the_sidecar_and_local_recovery_needs_no_network(self):
        record, source, manifest, fetch_root, arguments = self.fixture()
        attempt = record['attempt']
        scope = str(self.root / 'authorized')
        with patch('sherlock_kit.data_transfer', side_effect=self.copying(source)):
            code, output, err = self.run_main(arguments)
        self.assertEqual((code, err), (0, ''), err)
        self.assertEqual((output['destination'], output['recovered']), (str(fetch_root / 'verified'), False))
        self.assertEqual(output['receipt']['producer']['attempt'], attempt)
        # One control read pins the manifest; fetch_bundle re-reads it at each of its three stability checks.
        calls = self.remote.program_calls()
        self.assertEqual(len(calls), 4)
        for argv in calls:
            self.assertEqual(argv[3:], ['fetch-manifest', str(self.registry), attempt, scope, str(source), scope + '/manifest.json'])
        self.assertEqual([mutation for _, mutation in self.remote.calls], [False] * 4)
        self.assertEqual((fetch_root / 'verified' / 'result.json').read_text(), '42\n')
        sidecar = read_attempt_sidecar(fetch_root / 'verified')
        self.assertEqual((sidecar['attempt'], sidecar['manifest'], sidecar['validator_function']), (attempt, manifest, 'validate'))
        self.assertEqual(sidecar['validator_digest'], record['spec']['validator_digest'])
        self.assertEqual(sidecar['manifest_sha256'], digest(manifest))
        self.assertTrue((fetch_root / '.verified.shk-receipt.json').is_file())
        for extra in ([], ['--validator', 'validate', '--validator-sha256', record['spec']['validator_digest']]):
            with self.subTest(extra=extra):
                code, output, err = self.run_main(arguments + ['--local'] + extra, AssertionError('network ran'))
                self.assertEqual((code, err), (0, ''), err)
                self.assertEqual((output['recovered'], output['receipt']['manifest_sha256']), (True, digest(manifest)))
        refusals = {'foreign_attempt': (['--attempt', hex32(0x77)], 'another attempt'), 'validator_name': (['--validator', 'other'], 'validator function'),
                    'validator_digest': (['--validator-sha256', 'f' * 64], 'validator digest')}
        for name, (override, needle) in refusals.items():
            with self.subTest(refusal=name):
                replaced = list(arguments)
                if override[0] in replaced:
                    replaced[replaced.index(override[0]) + 1] = override[1]
                else:
                    replaced += override
                code, output, err = self.run_main(replaced + ['--local'], AssertionError('network ran'))
                self.assertEqual((code, output), (2, None))
                self.assertIn(needle, err)
        sidecar_path = fetch_root / '.verified.shk-attempt.json'
        text = sidecar_path.read_text()
        sidecar_path.write_text(text.replace(digest(manifest), 'e' * 64))
        code, output, err = self.run_main(arguments + ['--local'], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('disagrees with manifest', err)
        sidecar_path.unlink()
        code, output, err = self.run_main(arguments + ['--local'], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('no attempt sidecar for local recovery; run a remote fetch first', err)
        sidecar_path.write_text(text)
        sidecar_path.chmod(0o600)
        shutil.rmtree(fetch_root / 'verified')
        code, output, err = self.run_main(arguments + ['--local'], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('already promoted destination', err)

    def test_fetch_transfer_failures_exit_two_and_retain_transaction(self):
        record, source, manifest, fetch_root, arguments = self.fixture()
        destination = fetch_root / 'verified'
        failures = {'transfer_error': TransferError('rsync transfer failed with exit status 23: some files could not be transferred', returncode=23, stderr_tail='some files could not be transferred'),
                    'called_process_error': subprocess.CalledProcessError(12, ['rsync']), 'timeout_expired': subprocess.TimeoutExpired(['rsync'], 300)}
        for name, failure in failures.items():
            with self.subTest(failure=name):
                transfers = []

                def transfer(transport, remote_source, stage, m, failure=failure, transfers=transfers):
                    transfers.append((transport.data_host, remote_source, Path(stage), m))
                    raise failure

                with patch('sherlock_kit.data_transfer', side_effect=transfer):
                    code, output, err = self.run_main(arguments)
                self.assertEqual((code, output), (2, None))
                self.assertTrue(err.startswith('shk: '), err)
                self.assertEqual(len(transfers), 1)
                host, remote_source, stage, transferred = transfers[0]
                self.assertEqual((host, remote_source), ('sherlock-dtn', str(source)))
                self.assertEqual(stage.parent, fetch_root)
                self.assertTrue(stage.name.startswith('.verified.shk-stage-'))
                self.assertEqual(transferred['producer']['attempt'], record['attempt'])
                self.assertFalse(destination.exists())
                self.assertTrue((fetch_root / '.verified.shk-transaction.json').is_file())
                self.assertTrue((fetch_root / '.verified.shk-attempt.json').is_file())
                self.assertFalse((fetch_root / '.verified.shk-receipt.json').exists())
                if name == 'transfer_error':
                    self.assertIn('exit status 23', err)
        with patch('sherlock_kit.data_transfer', side_effect=self.copying(source)):
            code, output, err = self.run_main(arguments)
        self.assertEqual((code, output['recovered']), (0, False))
        self.assertEqual((destination / 'result.json').read_text(), '42\n')
        self.assertTrue((fetch_root / '.verified.shk-receipt.json').is_file())

    def test_fetch_rejections_precede_the_network_or_the_transfer(self):
        record, source, manifest, fetch_root, arguments = self.fixture()
        scope = self.root / 'authorized'
        outside = self.root / 'outside'
        outside.mkdir()
        cases = {'manifest_outside_scope': {'--manifest': str(outside / 'manifest.json')}, 'source_outside_scope': {'--source-root': str(outside)},
                 'relative_manifest': {'--manifest': 'manifest.json'}, 'traversal': {'--source-root': str(scope / '..' / 'authorized' / 'output')},
                 'destination_outside_fetch_root': {'--destination': str(outside / 'verified')}, 'destination_is_fetch_root': {'--destination': str(fetch_root)}}
        for name, overrides in cases.items():
            with self.subTest(case=name):
                replaced = list(arguments)
                for flag, value in overrides.items():
                    replaced[replaced.index(flag) + 1] = value
                code, output, err = self.run_main(replaced, AssertionError('network ran'))
                self.assertEqual((code, output), (2, None))
                self.assertTrue(err.startswith('shk: '))
        unverified = self.config(fetch_root=str(fetch_root), validator_roots=[str(self.root)], remote_roots={'control': str(scope), 'data': str(scope)})
        code, output, err = self.run_main(['fetch', '--config', unverified, *arguments[3:]], AssertionError('network ran'))
        self.assertEqual(code, 2)
        self.assertIn('namespace', err)
        code, output, err = self.run_main(['fetch', '--config', arguments[2], '--attempt', hex32(0x77), *arguments[5:]])
        self.assertEqual(code, 2)
        self.assertIn('control manifest unavailable', err)
        # The record's identity, not the config alone, decides who may fetch; the validator and manifest come after it.
        foreign = self.config(principal='other', fetch_root=str(fetch_root), validator_roots=[str(self.root)], remote_roots={'control': str(scope), 'data': str(scope), 'namespace_verified': True})
        code, output, err = self.run_main(['fetch', '--config', foreign, *arguments[3:]])
        self.assertEqual(code, 2)
        self.assertIn('differs from recorded attempt', err)
        self.remote.fail('fetch-manifest', RemoteResult('auth_required', stderr='denied', returncode=255, dispatched=True))
        code, output, err = self.run_main(arguments)
        self.assertEqual(code, 2)
        self.assertIn('control manifest unavailable; no unverified transfer', err)
        self.assertFalse((fetch_root / '.verified.shk-transaction.json').exists())
        self.assertFalse((fetch_root / '.verified.shk-attempt.json').exists())
        self.assertFalse((fetch_root / '.verified.shk-lock').exists())


class OccupancyTests(CommandCase):
    def test_occupancy_parses_fixed_columns_aggregates_and_caches(self):
        payload = '\n'.join((squeue_line('alice', 'RUNNING', 'cpu=4,mem=32G,node=1,billing=4,gres/gpu=2,gres/gpu:h100=2'),
                             squeue_line('alice', 'PENDING', ''),
                             squeue_line('bob', 'RUNNING', 'cpu=8,gres/gpu:h100=1'),
                             squeue_line('bob', 'COMPLETING', 'cpu=1,gres/gpu=1'),
                             squeue_line('carol', 'SUSPENDED', 'cpu=1,gres/gpu=4'),
                             squeue_line('dave', 'PENDING', 'N/A'))) + '\n'
        self.remote.squeue_stdout = payload
        config = self.config()
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'owners'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual([argv for argv, _ in self.remote.calls], [['env', 'LC_ALL=C', 'squeue', '-h', '-p', 'owners', '-O', SQUEUE_FORMAT]])
        self.assertEqual(report['partition'], 'owners')
        self.assertEqual(report['profile'], dict(partition_profile('owners')))
        self.assertEqual(report['users'], {'alice': {'running_jobs': 1, 'running_gpus': 2, 'pending_jobs': 1},
                                           'bob': {'running_jobs': 2, 'running_gpus': 2, 'pending_jobs': 0},
                                           'dave': {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 1}})
        self.assertEqual(report['totals'], {'running_jobs': 3, 'running_gpus': 4, 'pending_jobs': 2})
        self.assertEqual(report['transport'], 'complete')
        cached_code, cached, err = self.run_main(['occupancy', '--config', config, '--partition', 'owners'], AssertionError('cadence violated'))
        self.assertEqual((cached_code, cached), (0, report))
        self.assertEqual(query_keys(self.state), [self.read_key('squeue', 'owners')])
        denied = lambda *a, **k: RemoteResult('auth_required', stderr='Permission denied', returncode=255, dispatched=True)
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'btrippe'], denied)
        self.assertEqual((code, err), (1, ''))
        self.assertEqual(report, {'partition': 'btrippe', 'profile': dict(partition_profile('btrippe')), 'users': {},
                                  'totals': {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 0}, 'transport': 'auth_required'})
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'nonexistent'], AssertionError('network ran'))
        self.assertEqual((code, report), (2, None))
        self.assertIn('unknown partition', err)
        self.remote.squeue_stdout = squeue_line('erin', 'RUNNING', 'cpu=1,gres/gpu=x') + '\n'
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'normal'])
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


class AdmissionAndValidatorTests(CommandCase):
    def test_installed_for_admission_pin_cases(self):
        identity = {'schema_version': 1, 'code_revision': 'f' * 40, 'policy_sha256': 'b' * 64, 'install_mode': 'frozen', 'partitions_sha256': 'c' * 64}
        frozen_spec = spec(policy_digest='b' * 64)
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
                    self.assertEqual(installed_for_admission(frozen_spec), identity)
                else:
                    with self.assertRaisesRegex(SafetyError, error):
                        installed_for_admission(frozen_spec)
        with patch('sherlock_kit.policy_identity', return_value=identity), patch.dict(os.environ):
            os.environ.pop('SHERLOCK_KIT_PIN', None)
            self.assertEqual(installed_for_admission(frozen_spec), identity)
            with self.assertRaisesRegex(SafetyError, 'differs from installed policy'):
                installed_for_admission(spec())
        with patch('sherlock_kit.policy_identity', return_value={**identity, 'install_mode': 'development'}), self.assertRaisesRegex(SafetyError, 'frozen'):
            installed_for_admission(frozen_spec)

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


class ManagedCliTests(CommandCase):
    def test_managed_cli_backoff_priority_and_no_home_writes(self):
        fake = self.root / 'fake-ssh'
        fake.write_bytes((SOURCE_ROOT / 'tests/fixtures/consumer/fake_ssh.py').read_bytes())
        fake.chmod(0o700)
        record = self.root / 'ssh-calls.jsonl'
        for name in ('environment', 'explicit', 'neither', 'null'):
            with self.subTest(priority=name):
                case = self.root / name
                case.mkdir(mode=0o700)
                home = case / 'home'
                home.mkdir(mode=0o700)
                transport = {'ssh_binary': str(fake)}
                env = {**os.environ, 'HOME': str(home), 'XDG_STATE_HOME': str(case / 'xdg'), 'FAKE_SSH_MODE': 'auth', 'FAKE_SSH_RECORD': str(record)}
                env.pop('SHERLOCK_KIT_STATE_ROOT', None)
                expected = None
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
                config.write_text(json.dumps(dict(schema_version=1, registry_root='/synthetic/registry', principal=PRINCIPAL, cluster=CLUSTER, transport=transport)))
                config.chmod(0o600)
                result = subprocess.run([sys.executable, str(SOURCE_ROOT / 'src/sherlock_kit.py'), 'status', '--config', str(config), '--attempt', hex32(1)],
                                        env=env, capture_output=True, text=True, timeout=30)
                if expected is None:
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertEqual(result.stderr, 'shk: ' + EXPLICIT_ROOT_MESSAGE + '\n')
                    self.assertEqual(result.stdout, '')
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout)['transport'], 'auth_required')
                    self.assertTrue(expected.is_file())
                    self.assertEqual(stat.S_IMODE(expected.stat().st_mode), 0o600)
                    self.assertTrue(expected.with_name('query-cache.json').is_file())
                self.assertEqual(list(home.iterdir()), [])
                self.assertFalse((case / 'xdg').exists())
        self.assertEqual(len(record.read_text().splitlines()), 2)

    def test_invalid_env_override_rejects_cli_before_any_network_or_write(self):
        home = self.root / 'empty-home'
        home.mkdir(mode=0o700)
        config = self.root / 'private.json'
        config.write_text(json.dumps(dict(schema_version=1, registry_root='/synthetic/registry', principal=PRINCIPAL, cluster=CLUSTER, transport={})))
        config.chmod(0o600)
        for override in ('', 'relative-root'):
            with self.subTest(override=override):
                env = {**os.environ, 'HOME': str(home), 'XDG_STATE_HOME': str(self.root / 'uncreated-xdg'), 'SHERLOCK_KIT_STATE_ROOT': override}
                result = subprocess.run([sys.executable, str(SOURCE_ROOT / 'src/sherlock_kit.py'), 'status', '--config', str(config), '--attempt', hex32(1)],
                                        env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 2)
                self.assertIn('SHERLOCK_KIT_STATE_ROOT', result.stderr)
                self.assertFalse((self.root / 'uncreated-xdg').exists())
                self.assertEqual(list(home.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
