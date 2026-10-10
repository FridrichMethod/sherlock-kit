"""End-to-end CLI flows: every consumer command through main() against the registry program executed locally.

``tests/fake_remote.FakeRemote`` stands in for ``sherlock_kit.run_remote``: the
principal probe and the occupancy ``squeue`` get canned replies, while every
registry program call (``submit``/``read``/``event``/``fetch-manifest``) really
executes the shipped ``python3 -c`` stub against this test's temporary
``registry_root`` with fake ``sbatch``/``sacct``/``squeue`` on PATH. The data
endpoint is a patched ``sherlock_kit.data_transfer``. Accounting timestamps derive
from the seeded records and the query cadence is aged in the local cache file, so
no test sleeps or depends on the wall clock.
"""
from contextlib import ExitStack
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
sys.path.insert(1, str(Path(__file__).resolve().parent))
import sherlock_commands
from sherlock_commands import PREEMPTION_MESSAGE, abandon_hint
from sherlock_orchestration import IDENTITY_KEYS, canonical, digest, sbatch_options
from sherlock_partitions import partition_profile, partitions_sha256
from sherlock_artifacts import attempt_record, build_manifest, read_attempt_sidecar
from sherlock_kit import RemoteResult
from sherlock_registry import FIELDS, REASONS, WIDE_FIELDS, program_sha256, sacct_argv
from fake_remote import (CLUSTER, CONTROL_HOST, CREATED, PRINCIPAL, PROBE, SCRIPT, SQUEUE_FORMAT, FakeRemote, config_file, expire_queries, frozen, hex32,
                         pending_aggregate, query_keys, run_main, sacct_line, seed_attempt, spec, squeue_line, stamp, stamped_record, submitted_event, task_line)

STAMPED_KEYS = ('created', 'created_on', 'principal_uid', 'program_sha256')
RESULT_KEYS = {'attempt', 'resolution', 'job_id', 'tasks', 'cost', 'anomalies', 'events', 'scientific_validation'}


def frozen_identity(**overrides):
    """What a frozen installed toolkit reports, aligned with the fixture spec's policy digest."""
    return {'schema_version': 1, 'code_revision': 'c' * 40, 'policy_sha256': spec().policy_digest, 'install_mode': 'frozen',
            'partitions_sha256': partitions_sha256(), **overrides}


def pin_for(code_revision):
    return json.dumps({'schema_version': 1, 'code_revision': code_revision, 'policy_sha256': spec().policy_digest})


def tasks(count=1, **overrides):
    return {'count': count, 'terminal': 0, 'running': 0, 'pending': 0, 'missing': 0, 'by_state': {}, 'incomplete_history': [], **overrides}


def cost(cpu_seconds, gpu_seconds=0, known=False):
    return {'cpu_seconds': cpu_seconds, 'gpu_seconds': gpu_seconds, 'known': known}


class FlowCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.registry = self.root / 'registry'
        self.state = self.root / 'state'
        self.state.mkdir(mode=0o700)
        self.remote = FakeRemote(self.root)
        self.script = self.root / 'job.sh'
        self.script.write_bytes(SCRIPT)
        self.run_dir = self.root / 'run'
        self.run_dir.mkdir()
        self.identity = frozen_identity()
        self.counter = 0

    def config(self, **extra):
        return config_file(self.root, self.registry, self.state, **extra)

    def run_main(self, arguments, remote=None):
        return run_main(arguments, self.remote if remote is None else remote)

    def new_id(self):
        self.counter += 1
        return hex32(0xf000 + self.counter)

    def seed(self, attempt_id=None, created=CREATED, events=(), marker=True, **overrides):
        return seed_attempt(self.registry, stamped_record(attempt_id or self.new_id(), frozen(**overrides), created), events, marker)

    def read_key(self, *suffix):
        return canonical([CLUSTER, PRINCIPAL, CONTROL_HOST, *suffix])

    def files(self, attempt_id):
        directory = self.registry / 'attempts' / attempt_id
        return sorted(path.name for path in directory.iterdir()) if directory.exists() else None

    def attempts(self):
        directory = self.registry / 'attempts'
        return sorted(path.name for path in directory.iterdir()) if directory.exists() else []

    def marker(self, record):
        path = self.registry / 'tasks' / record['key']
        return path.read_text().strip() if path.exists() else None

    def stored(self, attempt_id, name='record.json'):
        return json.loads((self.registry / 'attempts' / attempt_id / name).read_text())

    def spec_file(self, **overrides):
        """A spec whose script and run directory exist locally, so the runner's workload checks pass."""
        self.counter += 1
        body = spec(**{'remote_script': str(self.script), 'script_digest': hashlib.sha256(SCRIPT).hexdigest(), 'remote_run_directory': str(self.run_dir), **overrides})
        path = self.root / f'spec-{self.counter}.json'
        path.write_text(json.dumps(dataclasses.asdict(body)))
        return str(path)

    def submit(self, arguments, remote=None, pin=None):
        with ExitStack() as stack:
            stack.enter_context(patch('sherlock_kit.policy_identity', return_value=self.identity))
            stack.enter_context(patch.dict(os.environ))
            os.environ.pop('SHERLOCK_KIT_PIN', None)
            if pin is not None:
                os.environ['SHERLOCK_KIT_PIN'] = str(pin)
            return self.run_main(arguments, remote)

    def retry(self, config, parent, **overrides):
        """submit --apply of the same logical task naming ``parent``; returns (code, output, err)."""
        return self.submit(['submit', '--config', config, '--spec', self.spec_file(parent_attempt=parent, **overrides), '--apply'])


class StatusFlowTests(FlowCase):
    def test_status_resolves_terminal_through_one_read_and_only_reconcile_writes_the_closure(self):
        record = self.seed(events=[submitted_event('123')])
        attempt = record['attempt']
        self.remote.fake['sacct_stdout'] = sacct_line(attempt) + '\n'
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(set(output), RESULT_KEYS)
        self.assertEqual((output['attempt'], output['resolution'], output['job_id'], output['events'], output['scientific_validation']),
                         (record, 'terminal', '123', ['submitted'], 'unverified'))
        self.assertEqual((output['tasks'], output['cost'], output['anomalies']), (tasks(terminal=1, by_state={'COMPLETED': 1}), cost(60, known=True), []))
        reader, = self.remote.program_calls()
        self.assertEqual(reader[3:], ['read', str(self.registry), '--attempt', attempt])
        # One bounded accounting query on the login node: 15 fields, wide names, this principal, the admission window, selection by name only.
        sacct, = self.remote.tool_calls('sacct')
        self.assertEqual(sacct['argv'], sacct_argv(PRINCIPAL, CREATED, '--name=shk-' + attempt)[5:])
        self.assertEqual(sacct['argv'][:4], ['-n', '-P', '--local', '--allocations'])
        self.assertEqual(sacct['argv'][4:8], ['--user=' + PRINCIPAL, '--duplicates', '--starttime=' + stamp(CREATED - 300), '--endtime=now'])
        fields = sacct['argv'][8].removeprefix('--format=').split(',')
        self.assertEqual(len(fields), 15)
        self.assertEqual(tuple(field.removesuffix('%128') for field in fields), FIELDS)
        self.assertEqual({field.removesuffix('%128') for field in fields if field.endswith('%128')}, set(WIDE_FIELDS))
        self.assertEqual(sacct['argv'][9], '--name=shk-' + attempt)
        self.assertFalse(any(flag.startswith('--jobs') for flag in sacct['argv']))
        # The cadence is reserved per attempt: a second look within 60 s reuses the cached read and touches nothing on Sherlock.
        code, cached, err = self.run_main(['status', '--config', config, '--attempt', attempt], AssertionError('cadence violated'))
        self.assertEqual((code, cached), (0, output))
        self.assertEqual(query_keys(self.state), [self.read_key('read', attempt)])
        self.assertEqual(self.files(attempt), ['record.json', 'submitted.json'])
        # reconcile reuses the same cached read but sends the closure; the resolved note is written by reconcile, never by status.
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['events'], output['cost']), ('terminal', ['submitted', 'resolved'], cost(60, known=True)))
        self.assertEqual([argv[3] for argv in self.remote.program_calls()], ['read', 'event'])
        self.assertEqual([mutation for _, mutation in self.remote.calls], [False, True])
        self.assertEqual(self.files(attempt), ['record.json', 'resolved.json', 'submitted.json'])
        resolved = self.stored(attempt, 'resolved.json')
        self.assertEqual((resolved['resolution'], resolved['job_id'], resolved['tasks'], resolved['cost']), ('terminal', '123', output['tasks'], output['cost']))
        self.assertEqual(resolved['rows_sha256'], digest([dict(zip(FIELDS, sacct_line(attempt).split('|')))]))
        # The closure forgot the cached read, so status within the cadence reads afresh and sees the event; the attempt has left the open set.
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['events']), (0, 'terminal', ['resolved', 'submitted']))
        self.assertEqual(len(self.remote.program_calls('read')), 2)
        code, output, err = self.run_main(['status', '--all', '--config', config])
        self.assertEqual((code, output, err), (0, [], ''))
        self.assertEqual(len(self.remote.tool_calls('sacct')), 3)

    def test_lost_reply_resolves_by_name_and_completed_work_blocks_a_retry(self):
        record = self.seed(events=[submitted_event(None)])
        attempt = record['attempt']
        self.assertIsNone(record['spec']['parent_attempt'])
        self.remote.fake['sacct_stdout'] = sacct_line(attempt, JobID='777', JobIDRaw='777', State='RUNNING', End='Unknown') + '\n'
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['job_id'], output['tasks'], output['cost']), ('identified', '777', tasks(running=1, by_state={'RUNNING': 1}), cost(0)))
        sacct, = self.remote.tool_calls('sacct')
        self.assertEqual(sacct['argv'][-1], '--name=shk-' + attempt)
        self.assertFalse(any(flag.startswith('--jobs') for flag in sacct['argv']))
        # A running attempt is not released: the runner refuses the retry under the registry lock after its own fresh query.
        code, output, err = self.retry(config, attempt)
        self.assertEqual((code, output), (2, None))
        self.assertIn('shk: parent attempt is not released (identified)', err)
        self.assertEqual([argv for argv, _ in self.remote.calls][-2], PROBE)
        self.assertEqual(len(self.remote.program_calls('submit')), 1)
        self.assertEqual((self.attempts(), self.marker(record)), ([attempt], attempt))
        expire_queries(self.state)
        self.remote.fake['sacct_stdout'] = sacct_line(attempt, JobID='777', JobIDRaw='777') + '\n'
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['job_id'], output['cost'], output['events']), ('terminal', '777', cost(60, known=True), ['submitted', 'resolved']))
        self.assertEqual(self.stored(attempt, 'resolved.json')['job_id'], '777')
        # COMPLETED work under the same logical key is never retried; a partial redo is a new logical task.
        code, output, err = self.retry(config, attempt)
        self.assertEqual((code, output), (2, None))
        self.assertIn('parent attempt is not released (terminal)', err)
        code, output, err = self.retry(config, None, task='redo')
        self.assertEqual((code, output['resolution'], output['attempt']['spec']['task']), (0, 'submitted', 'redo'))
        self.assertEqual(len(self.attempts()), 2)

    def test_unexpected_preemption_blocks_the_retry_until_acknowledged_then_the_marker_moves(self):
        record = self.seed(events=[submitted_event('123')])
        attempt = record['attempt']
        self.assertFalse(record['spec']['partition_profile']['preemptible'])
        first = sacct_line(attempt, State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 31))
        failed = sacct_line(attempt, State='FAILED', Restarts='1', Start=stamp(CREATED + 100), End=stamp(CREATED + 160), DBIndex='43', ExitCode='1:0')
        self.remote.fake['sacct_stdout'] = first + '\n' + failed + '\n'
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (2, 'shk: ' + PREEMPTION_MESSAGE + '\n'))
        self.assertEqual((output['resolution'], output['anomalies'], output['reason']),
                         ('unexpected_preemption', ['preempted_on_non_preemptible', 'restart_on_non_preemptible'], REASONS['unexpected_preemption']))
        # Both restarts are accounted, but nothing is certified while the anomaly is open.
        self.assertEqual((output['tasks'], output['cost']), (tasks(terminal=1, by_state={'FAILED': 1}), cost(90)))
        code, output, err = self.retry(config, attempt)
        self.assertEqual((code, output), (2, None))
        self.assertIn('parent attempt is not released (unexpected_preemption)', err)
        self.assertEqual((self.attempts(), self.marker(record)), ([attempt], attempt))
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution']), (2, 'unexpected_preemption'))
        self.assertEqual(self.remote.program_calls('event'), [])
        # Acknowledgement is explicit, needs the network, and waives exactly the anomalous task at its top restart.
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--acknowledge-preemption', '--note', 'node drained'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['operation'], output['outcome'], output['note'], output['attempt']), ('ack', 'written', 'node drained', record))
        ack_file = output['event']
        waiver = self.stored(attempt, ack_file)
        self.assertEqual((waiver['waived'], waiver['job_id'], waiver['note']), ({'0': {'restart': 1, 'state': 'FAILED'}}, '123', 'node drained'))
        self.assertEqual(waiver['observation']['cost'], cost(90))
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['anomalies'], output['events'], output['cost']), ('terminal', ['acknowledged_preemption'], ['ack', 'submitted'], cost(90, known=True)))
        # A FAILED top restart releases the logical task: the retry is admitted and the marker moves to the new attempt.
        code, output, err = self.retry(config, attempt)
        self.assertEqual((code, err), (0, ''), err)
        self.assertEqual((output['resolution'], output['job_id'], output['recorded']), ('submitted', '777', True))
        child = output['attempt']['id']
        self.assertEqual(output['attempt']['spec']['parent_attempt'], attempt)
        self.assertEqual((sorted(self.attempts()), self.marker(record)), (sorted([attempt, child]), child))
        self.assertEqual((self.files(child), self.stored(child)['spec']['parent_attempt']), (['record.json', 'submitted.json'], attempt))
        self.assertEqual(len(self.remote.program_calls('submit')), 2)
        # The child's rows are not in accounting yet: a known job id keeps it inconclusive, never abandonable.
        code, output, err = self.run_main(['status', '--config', config, '--attempt', child])
        self.assertEqual((code, output['resolution'], output['job_id'], output['reason'], err), (0, 'inconclusive', '777', REASONS['inconclusive'], ''))
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, [(item['attempt']['attempt'], item['resolution']) for item in output]), (0, [(attempt, 'terminal'), (child, 'inconclusive')]))
        self.assertEqual((self.files(attempt), output[0]['events']), (sorted([ack_file, 'record.json', 'resolved.json', 'submitted.json']), ['ack', 'submitted', 'resolved']))

    def test_owners_requeue_attempt_resolves_from_restart_history(self):
        record = self.seed(resources={**spec().resources, 'partition': 'owners', 'gpus': 1}, events=[submitted_event('555')])
        attempt = record['attempt']
        self.assertEqual((record['spec']['resources']['requeue'], record['spec']['partition_profile']), (True, dict(partition_profile('owners'))))
        for flag in ('--partition=owners', '--requeue', '--open-mode=append', '-G'):
            self.assertIn(flag, record['sbatch_options'])
        self.assertNotIn('--no-requeue', record['sbatch_options'])
        gpu = {'JobID': '555', 'JobIDRaw': '555', 'AllocTRES': 'cpu=1,gres/gpu=1'}
        first = sacct_line(attempt, State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 31), **gpu)
        running = sacct_line(attempt, State='RUNNING', Restarts='1', ElapsedRaw='20', Start=stamp(CREATED + 100), End='Unknown', DBIndex='43', **gpu)
        self.remote.fake['sacct_stdout'] = first + '\n' + running + '\n'
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['job_id'], output['anomalies']), ('identified', '555', []))
        # The finished restart is a lower bound; the running one is not yet measured and nothing is certified.
        self.assertEqual((output['tasks'], output['cost']), (tasks(running=1, by_state={'RUNNING': 1}), cost(30, 30)))
        expire_queries(self.state)
        done = sacct_line(attempt, State='COMPLETED', Restarts='1', Start=stamp(CREATED + 100), End=stamp(CREATED + 160), DBIndex='43', **gpu)
        self.remote.fake['sacct_stdout'] = first + '\n' + done + '\n'
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['tasks'], output['cost'], output['events']),
                         ('terminal', tasks(terminal=1, by_state={'COMPLETED': 1}), cost(90, 90, True), ['submitted', 'resolved']))
        self.assertEqual(self.stored(attempt, 'resolved.json')['cost'], cost(90, 90, True))
        # A restart on a preemptible partition is expected; a gap in the restart history and a non-terminal result after the closure are flagged.
        expire_queries(self.state)
        self.remote.fake['sacct_stdout'] = done + '\n'
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['anomalies'], output['tasks']['incomplete_history']), (0, 'identified', ['reopened_after_resolved', 'restart_gap'], [0]))


class SubmitFlowTests(FlowCase):
    def test_apply_gates_run_in_order_before_the_runner_and_the_registry_records_the_attempt(self):
        config = self.config()
        pin = self.root / 'pin.json'
        pin.write_text(pin_for('0' * 40))
        # 1. The spec itself, before the installed identity is consulted.
        with patch('sherlock_kit.policy_identity', side_effect=AssertionError('identity consulted')):
            code, output, err = self.run_main(['submit', '--config', config, '--spec', self.spec_file(remote_run_directory=None), '--apply'], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('remote_run_directory', err)
        # 2. The installed toolkit must be frozen, carry the spec's policy and match the advertised pin.
        apply = ['submit', '--config', config, '--spec', self.spec_file(), '--apply']
        gates = {'production admission requires a frozen installed toolkit': frozen_identity(install_mode='development'),
                 'new attempt policy differs from installed policy': frozen_identity(policy_sha256='b' * 64),
                 'advertised/installed revision mismatch': frozen_identity()}
        for message, identity in gates.items():
            with self.subTest(gate=message):
                self.identity = identity
                code, output, err = self.submit(apply, AssertionError('network ran'), pin=pin)
                self.assertEqual((code, output), (2, None))
                self.assertIn(message, err)
        # 3. With a matching pin, exactly one principal probe precedes anything frozen or claimed.
        self.identity = frozen_identity()
        pin.write_text(pin_for('c' * 40))
        probes = {'other_principal': RemoteResult('complete', stdout='someone-else\n', returncode=0, dispatched=True),
                  'auth_required': RemoteResult('auth_required', stderr='Permission denied', returncode=255, dispatched=True)}
        for name, probe in probes.items():
            with self.subTest(probe=name):
                self.remote.calls.clear()
                self.remote.fail('probe', probe)
                code, output, err = self.submit(apply, pin=pin)
                self.assertEqual((code, output), (2, None))
                self.assertIn('authenticated principal not established before admission', err)
                self.assertEqual(self.remote.calls, [(PROBE, False)])
        self.remote.failures.clear()
        # 4. The frozen record is sized before it is shipped.
        self.remote.calls.clear()
        with patch.object(sherlock_commands, 'REMOTE_ARGV_LIMIT', 1000):
            code, output, err = self.submit(apply, pin=pin)
        self.assertEqual((code, output), (2, None))
        self.assertIn('exceeds 1000 characters', err)
        self.assertEqual(self.remote.calls, [(PROBE, False)])
        self.assertFalse(self.registry.exists())
        # 5. Exactly two remote calls: the probe, then the runner; the registry on Sherlock holds the whole attempt.
        self.remote.calls.clear()
        before = time.time()
        with patch.dict(os.environ, {'SBATCH_ACCOUNT': 'leak', 'SBATCH_PARTITION': 'leak'}):
            code, output, err = self.submit(apply, pin=pin)
        self.assertEqual((code, err), (0, ''), err)
        attempt = output['attempt']['id']
        self.assertEqual((output['operation'], output['resolution'], output['job_id'], output['recorded'], output['reason'], output['transport']),
                         ('submit', 'submitted', '777', True, None, 'complete'))
        self.assertEqual(output['attempt']['registry_root'], str(self.registry))
        self.assertEqual([(argv[:2], mutation) for argv, mutation in self.remote.calls], [(PROBE, False), (['python3', '-c'], True)])
        runner = self.remote.calls[1][0]
        self.assertEqual((runner[3], runner[4], runner[6], len(runner)), ('submit', str(self.registry), program_sha256(), 7))
        sent = json.loads(runner[5])
        self.assertEqual(canonical(sent), runner[5])
        frozen_spec = output['attempt']['spec']
        self.assertEqual((frozen_spec['toolkit_revision'], frozen_spec['partition_profile'], frozen_spec['partitions_sha256'], frozen_spec['grant']),
                         ('c' * 40, dict(partition_profile('normal')), partitions_sha256(), None))
        self.assertIs(frozen_spec['resources']['requeue'], False)
        self.assertEqual(sent, {'schema_version': 1, 'kind': 'record', 'attempt': attempt, 'key': digest([frozen_spec['project'], frozen_spec['campaign'], frozen_spec['task']]),
                                'job_name': 'shk-' + attempt, 'spec': frozen_spec, 'sbatch_options': sbatch_options(attempt, frozen_spec)})
        options = sent['sbatch_options']
        self.assertEqual(options[:3], ['sbatch', '--parsable', '--job-name=shk-' + attempt])
        for flag in ('--partition=normal', '--no-requeue', '--chdir=' + str(self.run_dir), '--output=' + str(self.run_dir) + '/slurm-%j.out'):
            self.assertIn(flag, options)
        self.assertFalse(any(flag in options for flag in ('--requeue', '--open-mode=append', '-G')) or any(flag.startswith('--array') for flag in options))
        # Registry directory contents: private tree, lock, create-once files, marker, stamped record; no temporaries.
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o700)
        for name in ('attempts', 'tasks'):
            self.assertEqual(stat.S_IMODE((self.registry / name).stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.registry / '.lock').lstat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in self.registry.iterdir()), ['.lock', 'attempts', 'tasks'])
        self.assertEqual((self.attempts(), self.files(attempt)), ([attempt], ['record.json', 'submitted.json']))
        self.assertEqual(sorted(path.name for path in (self.registry / 'tasks').iterdir()), [sent['key']])
        self.assertEqual(self.marker(sent), attempt)
        stored = self.stored(attempt)
        self.assertEqual({key: value for key, value in stored.items() if key not in STAMPED_KEYS}, sent)
        self.assertTrue(before - 1 <= stored['created'] <= time.time() + 1)
        self.assertEqual((stored['created_on'], stored['principal_uid'], stored['program_sha256']), (socket.gethostname(), os.getuid(), program_sha256()))
        submitted = self.stored(attempt, 'submitted.json')
        self.assertEqual((submitted['job_id'], submitted['cluster'], submitted['sbatch']), ('777', None, {'returncode': 0, 'stdout': '777\n', 'stderr': ''}))
        self.assertGreaterEqual(submitted['submitted_at'], stored['created'])
        for path in self.registry.rglob('*'):
            self.assertFalse(path.name.endswith('.tmp'), path)
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), 0o700 if path.is_dir() else 0o600, path)
        sbatch, = self.remote.tool_calls('sbatch')
        self.assertEqual((sbatch['argv'], sbatch['stdin'].encode('latin-1'), sbatch['sbatch_env']), (options[1:], SCRIPT, []))
        self.assertEqual(list(self.state.iterdir()), [])
        # The same logical task again is refused by the runner before any write; the fresh id is discarded.
        code, output, err = self.submit(apply, pin=pin)
        self.assertEqual((code, output), (2, None))
        self.assertIn('shk: logical task already has attempt ' + attempt + '; name it as parent_attempt to retry', err)
        self.assertEqual((self.attempts(), len(self.remote.program_calls('submit')), len(self.remote.tool_calls('sbatch'))), ([attempt], 2, 1))

    def test_array_submit_options_then_status_counts_a_pending_aggregate_and_sums_terminal_cost(self):
        config = self.config()
        code, preview, err = self.submit(['submit', '--config', config, '--spec', self.spec_file(resources={**spec().resources, 'array': {'count': 3}})], AssertionError('network ran'))
        self.assertEqual((code, preview['operation'], preview['resources']['array']), (0, 'preview', {'count': 3}))
        self.assertIn('--array=0-2', sbatch_options(hex32(1), frozen(resources=preview['resources'])))
        code, output, err = self.submit(['submit', '--config', config, '--spec', self.spec_file(resources={**spec().resources, 'array': {'count': 4, 'throttle': 2}}), '--apply'])
        self.assertEqual((code, err), (0, ''), err)
        attempt, options = output['attempt']['id'], self.stored(output['attempt']['id'])['sbatch_options']
        self.assertEqual(output['attempt']['spec']['resources']['array'], {'count': 4, 'throttle': 2})
        self.assertEqual(options[options.index('--partition=normal') + 1], '--array=0-3%2')
        self.assertEqual([flag for flag in options if flag.startswith(('--output=', '--error='))],
                         ['--output=' + str(self.run_dir) + '/slurm-%A_%a.out', '--error=' + str(self.run_dir) + '/slurm-%A_%a.err'])
        self.assertEqual(options, sbatch_options(attempt, output['attempt']['spec']))
        sbatch, = self.remote.tool_calls('sbatch')
        self.assertEqual(sbatch['argv'], options[1:])
        created = self.stored(attempt)['created']
        # Two tasks started, two still pending as one aggregate row: counts per task, no cost certified.
        self.remote.fake['sacct_stdout'] = '\n'.join((task_line(attempt, 0, 50, created, job='777', State='RUNNING', End='Unknown'),
                                                      task_line(attempt, 1, 51, created, job='777', State='RUNNING', End='Unknown'),
                                                      pending_aggregate(attempt, '2-3%2', created, job='777'))) + '\n'
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['job_id'], output['anomalies']), ('identified', '777', []))
        self.assertEqual((output['tasks'], output['cost']), (tasks(4, running=2, pending=2, by_state={'PENDING': 2, 'RUNNING': 2}), cost(0)))
        sacct = self.remote.tool_calls('sacct')[-1]
        self.assertEqual(sacct['argv'][-1], '--name=shk-' + attempt)
        # Every task finished: the cost sums over tasks; any COMPLETED task blocks a retry under the same logical key.
        expire_queries(self.state)
        elapsed = {0: '60', 1: '120', 2: '30', 3: '90'}
        self.remote.fake['sacct_stdout'] = '\n'.join(task_line(attempt, index, 50 + index, created, job='777', ElapsedRaw=elapsed[index], End=stamp(created + 1 + int(elapsed[index])),
                                                               **({'State': 'FAILED', 'ExitCode': '1:0'} if index == 2 else {})) for index in range(4)) + '\n'
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['tasks'], output['cost'], output['events']),
                         ('terminal', tasks(4, terminal=4, by_state={'COMPLETED': 3, 'FAILED': 1}), cost(300, known=True), ['submitted', 'resolved']))
        self.assertEqual(self.stored(attempt, 'resolved.json')['tasks']['count'], 4)
        code, output, err = self.retry(config, attempt, resources={**spec().resources, 'array': {'count': 4, 'throttle': 2}})
        self.assertEqual((code, output), (2, None))
        self.assertIn('parent attempt is not released (terminal)', err)
        self.assertEqual(self.attempts(), [attempt])


class EventFlowTests(FlowCase):
    def test_abandon_flow_end_to_end_with_local_and_remote_refusals(self):
        old = self.seed(events=[submitted_event(None)], task='old')
        attempt = old['attempt']
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, 'shk: ' + abandon_hint(1) + '\n'))
        self.assertEqual((output['resolution'], output['job_id'], output['tasks'], output['reason']), ('abandonable', None, tasks(missing=1), REASONS['abandonable']))
        # Local refusals from the last read: fresh, known job id, rows present; argument rules; no event is sent for any of them.
        refusals = {'fresh': (self.seed(created=time.time() - 10, events=[submitted_event(None)], task='fresh'), '', 'not abandonable (resolution inconclusive)'),
                    'known_job': (self.seed(events=[submitted_event('123')], task='known'), '', 'not abandonable (resolution inconclusive)'),
                    'rows': (self.seed(events=[submitted_event(None)], task='rows'), None, 'not abandonable (resolution identified)')}
        for name, (record, rows, needle) in refusals.items():
            with self.subTest(refusal=name):
                self.remote.fake['sacct_stdout'] = sacct_line(record['attempt'], State='RUNNING', End='Unknown') + '\n' if rows is None else rows
                code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', record['attempt'], '--abandon'])
                self.assertEqual((code, output), (2, None))
                self.assertIn(needle, err)
                self.assertEqual(self.files(record['attempt']), ['record.json', 'submitted.json'])
        self.remote.fake['sacct_stdout'] = ''
        for arguments in (['status', '--config', config, '--attempt', attempt, '--abandon'], ['reconcile', '--all', '--config', config, '--abandon'],
                          ['reconcile', '--config', config, '--attempt', attempt, '--note', 'x'], ['reconcile', '--config', config, '--attempt', attempt, '--abandon', '--acknowledge-preemption']):
            with self.subTest(arguments=arguments):
                code, output, err = self.run_main(arguments, AssertionError('network ran'))
                self.assertEqual((code, output), (2, None))
        self.assertEqual(self.remote.program_calls('event'), [])
        # The event writer re-checks under the registry lock: a job still visible to squeue refuses the closure.
        self.remote.fake['squeue_stdout'] = '4242\n'
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--abandon'])
        self.assertEqual((code, output), (2, None))
        self.assertIn('shk: registry refused --abandon: squeue_shows_job', err)
        self.assertEqual((len(self.remote.program_calls('event')), self.files(attempt)), (1, ['record.json', 'submitted.json']))
        squeue = self.remote.tool_calls('squeue')[-1]
        self.assertEqual(squeue['argv'], ['-h', '--name=shk-' + attempt, '-o', '%i'])
        self.remote.fake['squeue_stdout'] = ''
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--abandon', '--note', 'reply lost on 2026-10-07'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['operation'], output['outcome'], output['event'], output['attempt'], output['note'], output['transport']),
                         ('abandon', 'written', 'abandoned.json', old, 'reply lost on 2026-10-07', 'complete'))
        event = self.remote.program_calls('event')[-1]
        self.assertEqual((event[3:6], json.loads(event[6]), event[7:]), (['event', str(self.registry), 'abandon'], {'note': 'reply lost on 2026-10-07'}, [attempt]))
        abandoned = self.stored(attempt, 'abandoned.json')
        self.assertEqual((abandoned['note'], abandoned['checks']['sacct_rows'], abandoned['checks']['squeue_rows'], abandoned['checks']['squeue_argv'][-3:]),
                         ('reply lost on 2026-10-07', 0, 0, ['--name=shk-' + attempt, '-o', '%i']))
        self.assertGreaterEqual(abandoned['age_seconds'], 900)
        # The closure decides without rows and leaves the open set; it is idempotent through the local pre-check.
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], output['events'], output['reason'], err), (0, 'abandoned', ['abandoned', 'submitted'], REASONS['abandoned'], ''))
        code, output, err = self.run_main(['status', '--all', '--config', config])
        self.assertEqual((code, [item['attempt']['spec']['task'] for item in output]), (0, ['known', 'rows', 'fresh']))
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--abandon'], AssertionError('cadence violated'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('not abandonable (resolution abandoned)', err)
        # An abandoned attempt releases its logical task: the retry is admitted and the marker moves.
        code, output, err = self.retry(config, attempt, task='old')
        self.assertEqual((code, output['resolution'], output['attempt']['spec']['parent_attempt']), (0, 'submitted', attempt))
        self.assertEqual(self.marker(old), output['attempt']['id'])
        self.assertEqual(len(self.remote.program_calls('event')), 2)

    def test_job_id_conflict_is_an_isolated_error_that_blocks_events_and_retries(self):
        record = self.seed(events=[submitted_event('123')])
        attempt = record['attempt']
        self.remote.fake['sacct_stdout'] = sacct_line(attempt, JobID='999', JobIDRaw='999') + '\n'
        config = self.config()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual(code, 2)
        self.assertEqual((output['resolution'], output['error'], output['reason'], output['job_id'], output['attempt']), ('error', 'job_id_conflict', 'job_id_conflict', None, record))
        self.assertEqual((output['tasks'], output['cost']), (tasks(missing=1), cost(0)))
        self.assertEqual(err, 'shk: 1 attempt(s) could not be reconciled; see the "error" entries\n')
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output['resolution'], self.remote.program_calls('event'), self.files(attempt)), (2, 'error', [], ['record.json', 'submitted.json']))
        for flag in ('--abandon', '--acknowledge-preemption'):
            with self.subTest(flag=flag):
                code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, flag])
                self.assertEqual((code, output), (2, None))
                self.assertIn('attempt record cannot be used (job_id_conflict)', err)
        self.assertEqual(self.remote.program_calls('event'), [])
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, [item['resolution'] for item in output], output[0]['error']), (2, ['error'], 'job_id_conflict'))
        # The runner isolates the same conflict: a retry naming the conflicted parent is refused, nothing is claimed.
        code, output, err = self.retry(config, attempt)
        self.assertEqual((code, output), (2, None))
        self.assertIn('parent attempt record conflicts (job_id_conflict)', err)
        self.assertEqual((self.attempts(), self.marker(record)), ([attempt], attempt))

    def test_reader_failures_are_inconclusive_and_send_no_events(self):
        record = self.seed(events=[submitted_event('123')])
        attempt = record['attempt']
        config = self.config()
        self.remote.fail('read', RemoteResult('unavailable', stderr='Connection timed out', returncode=255, dispatched=True))
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(output, {'attempt': {'attempt': attempt}, 'resolution': 'inconclusive', 'transport': 'unavailable', 'scientific_validation': 'unverified'})
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, output, err), (0, [{'attempt': None, 'resolution': 'inconclusive', 'transport': 'unavailable', 'scientific_validation': 'unverified'}], ''))
        # Both failures hold the cadence: a retry within 60 s is answered locally.
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt], AssertionError('cadence violated'))
        self.assertEqual((code, output['transport']), (0, 'unavailable'))
        self.assertEqual(sorted(query_keys(self.state)), sorted([self.read_key('read', attempt), self.read_key('read-all')]))
        self.remote.failures.clear()
        # sacct failing or babbling on the login node is reported through the reader, never turned into an attempt state.
        for fake, transport in (({'sacct_rc': '1', 'sacct_stderr': 'sacct: error: slurmdbd down\n'}, 'sacct:failed'), ({'sacct_stdout': 'garbage\n'}, 'sacct:malformed')):
            with self.subTest(transport=transport):
                self.remote.fake = {'sbatch_stdout': '777\n', **fake}
                expire_queries(self.state)
                code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt])
                self.assertEqual((code, output['resolution'], output['transport'], output['attempt'], err), (0, 'inconclusive', transport, record, ''))
                code, output, err = self.run_main(['reconcile', '--all', '--config', config])
                self.assertEqual((code, [item['transport'] for item in output]), (0, [transport]))
        self.assertEqual((self.remote.program_calls('event'), self.files(attempt)), ([], ['record.json', 'submitted.json']))
        self.remote.fail('read', RemoteResult('complete', stdout='{"schema_version": 2}', returncode=0, dispatched=True))
        expire_queries(self.state)
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt])
        self.assertEqual((code, output), (2, None))
        self.assertIn('malformed reader output', err)


class FetchFlowTests(FlowCase):
    def fixture(self, task='verified'):
        """A registered attempt with a frozen validator, its bundle under an authorized scope and the fetch arguments."""
        validator = self.root / 'validator.py'
        validator.write_text('def validate(root): return (root / "result.json").read_text() == "42\\n"\n')
        validator_digest = hashlib.sha256(validator.read_bytes()).hexdigest()
        record = self.seed(task=task, validator_path=str(validator), validator_digest=validator_digest, validator_function='validate', events=[submitted_event('123')])
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
        arguments = ['fetch', '--config', config, '--attempt', record['attempt'], '--manifest', str(scope / 'manifest.json'), '--source-root', str(source), '--destination', str(fetch_root / task)]
        receipt = {'schema_version': 1, 'manifest_sha256': digest(manifest), 'producer': expected, 'validator_sha256': digest({'sha256': validator_digest, 'function': 'validate'})}
        return SimpleNamespace(record=record, attempt=record['attempt'], scope=scope, source=source, manifest=manifest, expected=expected, fetch_root=fetch_root,
                               destination=fetch_root / task, arguments=arguments, receipt=receipt, metadata=lambda kind: fetch_root / f'.{task}.shk-{kind}.json')

    def data_endpoint(self, fixture, transfers, after=None):
        """A data transfer that copies the manifest's files into the stage, recording how many control reads preceded it."""
        def transfer(transport, remote_source, stage, manifest):
            stage = Path(stage)
            transfers.append({'reads_before': len(self.remote.program_calls('fetch-manifest')), 'host': transport.data_host, 'source': remote_source,
                              'stage': stage, 'mode': stat.S_IMODE(stage.stat().st_mode), 'manifest': manifest})
            for item in manifest['files']:
                shutil.copy(fixture.source / item['path'], stage / item['path'])
            if after is not None:
                after()
        return transfer

    def local(self, fixture):
        with patch('sherlock_kit.data_transfer', side_effect=AssertionError('transfer ran')):
            return self.run_main(fixture.arguments + ['--local'], AssertionError('network ran'))

    def test_remote_fetch_pins_the_sidecar_rereads_transfers_once_promotes_then_recovers_locally(self):
        fixture, transfers = self.fixture(), []
        with patch('sherlock_kit.data_transfer', side_effect=self.data_endpoint(fixture, transfers)):
            code, output, err = self.run_main(fixture.arguments)
        self.assertEqual((code, err), (0, ''), err)
        self.assertEqual(output, {'destination': str(fixture.destination), 'receipt': fixture.receipt, 'recovered': False})
        # One control read pins the manifest; fetch_bundle re-reads it before the transaction and at its two post-transfer stability checks.
        calls = self.remote.program_calls()
        self.assertEqual(len(calls), 4)
        for argv in calls:
            self.assertEqual(argv[3:], ['fetch-manifest', str(self.registry), fixture.attempt, str(fixture.scope), str(fixture.source), str(fixture.scope / 'manifest.json')])
        self.assertEqual([mutation for _, mutation in self.remote.calls], [False] * 4)
        self.assertEqual(len(transfers), 1)
        transfer = transfers[0]
        self.assertEqual((transfer['reads_before'], transfer['host'], transfer['source'], transfer['manifest']), (2, 'sherlock-dtn', str(fixture.source), fixture.manifest))
        self.assertEqual((transfer['stage'], transfer['mode']), (fixture.fetch_root / ('.verified.shk-stage-' + digest(fixture.manifest)), 0o700))
        self.assertFalse(transfer['stage'].exists())
        self.assertEqual((fixture.destination / 'result.json').read_text(), '42\n')
        self.assertEqual(json.loads(fixture.metadata('receipt').read_text()), fixture.receipt)
        self.assertEqual(json.loads(fixture.metadata('transaction').read_text()), fixture.receipt)
        sidecar = read_attempt_sidecar(fixture.destination)
        self.assertEqual(sidecar, attempt_record(fixture.attempt, fixture.expected, fixture.record['spec'], fixture.manifest))
        self.assertEqual((sidecar['attempt'], sidecar['producer'], sidecar['manifest_sha256'], sidecar['validator_function']), (fixture.attempt, fixture.expected, digest(fixture.manifest), 'validate'))
        self.assertEqual(stat.S_IMODE(fixture.metadata('attempt').stat().st_mode), 0o600)
        self.assertEqual(sorted(path.name for path in fixture.fetch_root.iterdir()),
                         ['.verified.shk-attempt.json', '.verified.shk-lock', '.verified.shk-receipt.json', '.verified.shk-transaction.json', 'verified'])
        # Recovery afterwards needs neither endpoint and verifies the promoted bundle against the sidecar's manifest.
        code, output, err = self.local(fixture)
        self.assertEqual((code, err), (0, ''), err)
        self.assertEqual(output, {'destination': str(fixture.destination), 'receipt': fixture.receipt, 'recovered': True})
        # A repeated remote fetch of a promoted bundle reads the manifest once and transfers nothing.
        with patch('sherlock_kit.data_transfer', side_effect=AssertionError('transfer ran')):
            code, output, err = self.run_main(fixture.arguments)
        self.assertEqual((code, output['recovered'], len(self.remote.program_calls())), (0, True, 5))
        self.assertEqual(read_attempt_sidecar(fixture.destination), sidecar)

    def test_fetch_never_promotes_when_the_manifest_changes_mid_transfer(self):
        fixture, transfers = self.fixture('moving'), []
        original = (fixture.scope / 'manifest.json').read_text()
        changed = {**fixture.manifest, 'files': [{**fixture.manifest['files'][0], 'sha256': 'f' * 64}]}
        with patch('sherlock_kit.data_transfer', side_effect=self.data_endpoint(fixture, transfers, after=lambda: (fixture.scope / 'manifest.json').write_text(json.dumps(changed)))):
            code, output, err = self.run_main(fixture.arguments)
        self.assertEqual((code, output, err), (2, None, 'shk: source manifest changed during transfer\n'))
        self.assertEqual((len(transfers), len(self.remote.program_calls())), (1, 3))
        self.assertFalse(fixture.destination.exists())
        self.assertFalse(fixture.metadata('receipt').exists())
        self.assertEqual(json.loads(fixture.metadata('transaction').read_text()), fixture.receipt)
        # The sidecar pins the manifest read first; the transferred bytes stay in the private stage for a resume.
        self.assertEqual(read_attempt_sidecar(fixture.destination)['manifest'], fixture.manifest)
        stage = fixture.fetch_root / ('.moving.shk-stage-' + digest(fixture.manifest))
        self.assertEqual(((stage / 'result.json').read_text(), stat.S_IMODE(stage.stat().st_mode)), ('42\n', 0o700))
        code, output, err = self.local(fixture)
        self.assertEqual((code, output), (2, None))
        self.assertIn('local recovery requires an already promoted destination', err)
        # Once the control manifest is stable again the same transaction resumes and promotes.
        (fixture.scope / 'manifest.json').write_text(original)
        with patch('sherlock_kit.data_transfer', side_effect=self.data_endpoint(fixture, transfers)):
            code, output, err = self.run_main(fixture.arguments)
        self.assertEqual((code, err), (0, ''), err)
        self.assertEqual((output['recovered'], output['receipt'], len(transfers)), (False, fixture.receipt, 2))
        self.assertEqual(read_attempt_sidecar(fixture.destination)['manifest_sha256'], digest(fixture.manifest))
        self.assertEqual(self.local(fixture)[1]['recovered'], True)

    def test_fetch_pins_nothing_when_the_control_manifest_is_unavailable(self):
        fixture = self.fixture('absent')
        self.remote.fail('fetch-manifest', RemoteResult('unavailable', stderr='Connection timed out', returncode=255, dispatched=True))
        with patch('sherlock_kit.data_transfer', side_effect=AssertionError('transfer ran')):
            code, output, err = self.run_main(fixture.arguments)
        self.assertEqual((code, output, err), (2, None, 'shk: control manifest unavailable; no unverified transfer\n'))
        self.assertEqual(list(fixture.fetch_root.iterdir()), [])
        self.assertEqual(len(self.remote.program_calls('fetch-manifest')), 1)
        code, output, err = self.local(fixture)
        self.assertEqual((code, output), (2, None))
        self.assertIn('no attempt sidecar for local recovery; run a remote fetch first', err)
        # An attempt the registry does not know fails the same way on the login node: the program exits non-zero, nothing is transferred.
        self.remote.failures.clear()
        unknown = list(fixture.arguments)
        unknown[unknown.index('--attempt') + 1] = hex32(0x77)
        with patch('sherlock_kit.data_transfer', side_effect=AssertionError('transfer ran')):
            code, output, err = self.run_main(unknown)
        self.assertEqual((code, output, err), (2, None, 'shk: control manifest unavailable; no unverified transfer\n'))
        self.assertEqual(list(fixture.fetch_root.iterdir()), [])


class BatchAndOccupancyFlowTests(FlowCase):
    def test_reconcile_all_resolves_a_mixed_set_through_one_read_and_one_closure_batch(self):
        done = self.seed(created=CREATED, events=[submitted_event('101')], task='done')
        lost = self.seed(created=CREATED + 1, events=[submitted_event(None)], task='lost')
        silent = self.seed(created=CREATED + 2, events=[submitted_event('103')], task='silent')
        anomaly = self.seed(created=CREATED + 3, events=[submitted_event('104')], task='anomaly')
        restarted = self.seed(created=CREATED + 4, events=[submitted_event('105')], task='restarted', resources={**spec().resources, 'partition': 'owners'})
        self.seed(created=CREATED + 5, events=[('not_sent.json', {'at': CREATED + 6, 'reason': 'sbatch_unavailable', 'detail': ''})], task='unsent', marker=False)
        self.seed(created=CREATED + 6, events=[submitted_event(None), ('abandoned.json', {'at': CREATED + 1000, 'age_seconds': 994, 'checks': {}, 'note': ''})], task='gone')
        batch = [done, lost, silent, anomaly, restarted]
        rows = '\n'.join((sacct_line(done['attempt'], CREATED, JobID='101', JobIDRaw='101'),
                          sacct_line(lost['attempt'], CREATED + 1, JobID='102', JobIDRaw='102', State='RUNNING', End='Unknown', DBIndex='43'),
                          sacct_line(anomaly['attempt'], CREATED + 3, JobID='104', JobIDRaw='104', State='PREEMPTED', DBIndex='44'),
                          sacct_line(restarted['attempt'], CREATED + 4, JobID='105', JobIDRaw='105', State='PREEMPTED', ElapsedRaw='30', End=stamp(CREATED + 35), DBIndex='45'),
                          sacct_line(restarted['attempt'], CREATED + 4, JobID='105', JobIDRaw='105', State='COMPLETED', Restarts='1', Start=stamp(CREATED + 104), End=stamp(CREATED + 164), DBIndex='46'))) + '\n'
        self.remote.fake['sacct_stdout'] = rows
        config = self.config()
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, err), (2, 'shk: ' + PREEMPTION_MESSAGE + '\n'))
        self.assertEqual([item['attempt']['attempt'] for item in output], [record['attempt'] for record in batch])
        self.assertEqual([item['resolution'] for item in output], ['terminal', 'identified', 'inconclusive', 'unexpected_preemption', 'terminal'])
        self.assertEqual([item['job_id'] for item in output], ['101', '102', '103', '104', '105'])
        self.assertEqual((output[0]['cost'], output[0]['events']), (cost(60, known=True), ['submitted', 'resolved']))
        self.assertEqual((output[1]['tasks']['running'], output[1]['events']), (1, ['submitted']))
        self.assertEqual((output[2]['reason'], output[2]['tasks']['missing']), (REASONS['inconclusive'], 1))
        self.assertEqual((output[3]['anomalies'], output[3]['cost']), (['preempted_on_non_preemptible'], cost(60)))
        self.assertEqual((output[4]['cost'], output[4]['anomalies'], output[4]['events']), (cost(90, known=True), [], ['submitted', 'resolved']))
        # One reader call selects every open attempt by name from the earliest admission window; one closure batch names the fresh terminal ones.
        reader, event = self.remote.program_calls()
        self.assertEqual(reader[3:], ['read', str(self.registry), '--open'])
        self.assertEqual(event[3:], ['event', str(self.registry), 'resolved', '{}', done['attempt'], restarted['attempt']])
        self.assertEqual([mutation for _, mutation in self.remote.calls], [False, True])
        sacct = self.remote.tool_calls('sacct')
        self.assertEqual(len(sacct), 2)
        self.assertEqual(sacct[0]['argv'][-1], '--name=' + ','.join('shk-' + record['attempt'] for record in batch))
        self.assertIn('--starttime=' + stamp(CREATED - 300), sacct[0]['argv'])
        self.assertEqual(sacct[1]['argv'][-1], '--name=shk-' + done['attempt'] + ',shk-' + restarted['attempt'])
        for record in (done, restarted):
            self.assertEqual(self.files(record['attempt']), ['record.json', 'resolved.json', 'submitted.json'])
        self.assertEqual(query_keys(self.state), [])
        # Once the anomaly is acknowledged, its PREEMPTED end is terminal; the next batch closes it and lists only the still-open attempts.
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', anomaly['attempt'], '--acknowledge-preemption'])
        self.assertEqual((code, output['outcome'], err), (0, 'written', ''))
        self.assertEqual(self.stored(anomaly['attempt'], output['event'])['waived'], {'0': {'restart': 0, 'state': 'PREEMPTED'}})
        code, output, err = self.run_main(['reconcile', '--all', '--config', config])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual([(item['attempt']['spec']['task'], item['resolution']) for item in output], [('lost', 'identified'), ('silent', 'inconclusive'), ('anomaly', 'terminal')])
        self.assertEqual((output[2]['tasks']['by_state'], output[2]['cost'], output[2]['anomalies'], output[2]['events']),
                         ({'PREEMPTED': 1}, cost(60, known=True), ['acknowledged_preemption'], ['ack', 'submitted', 'resolved']))
        code, output, err = self.run_main(['status', '--all', '--config', config])
        self.assertEqual((code, [item['attempt']['spec']['task'] for item in output], err), (0, ['lost', 'silent'], ''))
        self.assertEqual(self.remote.tool_calls('sacct')[-1]['argv'][-1], '--name=shk-' + lost['attempt'] + ',shk-' + silent['attempt'])
        self.assertEqual((len(self.remote.program_calls('read')), len(self.remote.program_calls('event'))), (4, 3))

    def test_occupancy_reports_the_borrowed_partition_from_one_cached_squeue(self):
        self.remote.squeue_stdout = '\n'.join((squeue_line('alice', 'RUNNING', 'cpu=8,mem=64G,node=1,billing=8,gres/gpu=2,gres/gpu:a100=2'),
                                               squeue_line('alice', 'PENDING', ''),
                                               squeue_line('bob', 'COMPLETING', 'cpu=4,gres/gpu:a100=1'),
                                               squeue_line('carol', 'CANCELLED', 'cpu=1,gres/gpu=1'))) + '\n'
        config = self.config()
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'btrippe'])
        self.assertEqual((code, err), (0, ''))
        self.assertEqual([argv for argv, _ in self.remote.calls], [['env', 'LC_ALL=C', 'squeue', '-h', '-p', 'btrippe', '-O', SQUEUE_FORMAT]])
        self.assertEqual(list(report), ['partition', 'profile', 'totals', 'transport', 'users'])
        self.assertEqual((report['partition'], report['transport'], report['profile']), ('btrippe', 'complete', dict(partition_profile('btrippe'))))
        self.assertTrue(report['profile']['borrowed'] and report['profile']['gpus_allowed'])
        self.assertIn('shk occupancy', report['profile']['courtesy'])
        self.assertEqual(report['users'], {'alice': {'running_jobs': 1, 'running_gpus': 2, 'pending_jobs': 1}, 'bob': {'running_jobs': 1, 'running_gpus': 1, 'pending_jobs': 0}})
        self.assertEqual(report['totals'], {'running_jobs': 2, 'running_gpus': 3, 'pending_jobs': 1})
        # Within the cadence the same partition is answered from the cache; another partition is its own query.
        code, cached, err = self.run_main(['occupancy', '--config', config, '--partition', 'btrippe'], AssertionError('cadence violated'))
        self.assertEqual((code, cached), (0, report))
        self.remote.squeue_stdout = ''
        code, idle, err = self.run_main(['occupancy', '--config', config, '--partition', 'owners'])
        self.assertEqual((code, idle['users'], idle['totals']), (0, {}, {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 0}))
        self.assertEqual((len(self.remote.calls), self.remote.calls[1][0][5]), (2, 'owners'))
        self.assertEqual(sorted(query_keys(self.state)), sorted(self.read_key('squeue', name) for name in ('btrippe', 'owners')))
        # Occupancy gates nothing: an unreachable scheduler still prints the profile and exits 1; nothing touches the registry.
        self.remote.fail('squeue', RemoteResult('unavailable', stderr='Connection timed out', returncode=255, dispatched=True))
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'normal'])
        self.assertEqual((code, err, report['transport'], report['users'], report['profile']), (1, '', 'unavailable', {}, dict(partition_profile('normal'))))
        self.assertEqual(self.remote.program_calls(), [])
        self.assertFalse(self.registry.exists())


if __name__ == '__main__':
    unittest.main()
