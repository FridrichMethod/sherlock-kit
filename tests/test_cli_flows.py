"""In-process CLI flows: every consumer command through main() against a faked transport.

The control endpoint, the scheduler and the data endpoint are replaced by patched
``sherlock_kit.run_remote`` / ``sherlock_kit.data_transfer``. Every scheduler
timestamp derives from the attempt actually admitted and the query cadence is aged
in the coordinator's own state, so no test sleeps or depends on the wall clock.
"""
from contextlib import closing
import dataclasses
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sherlock_commands import REMOTE_MANIFEST, main, sacct_argv
from sherlock_orchestration import Coordinator, IDENTITY_KEYS, SUBMIT_TIME_TOLERANCE_SECONDS, SafetyError, canonical, digest
from sherlock_partitions import partition_profile, partitions_sha256
from sherlock_artifacts import build_manifest
from sherlock_kit import RemoteResult
from test_orchestration import spec, LIMITS
from test_commands import stamp, sacct_line, squeue_line, recorder, complete, MANY, PREEMPTION_MESSAGE, SQUEUE_FORMAT

CONTROL_HOST = 'sherlock-plain'
GPU_LIMITS = {'cpus': 4, 'gpus': 2, 'tasks': 2, 'cpu_seconds': 36000, 'gpu_seconds': 7200}
PROBE = ['id', '-un']
REMOTE_SCOPE = ['/authorized', '/authorized/output', '/authorized/manifest.json']


def ack(stdout, returncode=0):
    """Transport acknowledgement for dispatch(): the job id on success, a lost reply on 255."""
    return lambda command: RemoteResult('complete' if returncode == 0 else 'unknown', stdout=stdout, returncode=returncode, dispatched=True)


def frozen_identity(**overrides):
    """What a frozen installed toolkit reports, aligned with the fixture spec's policy digest."""
    return {'schema_version': 1, 'code_revision': 'c' * 40, 'policy_sha256': spec().policy_digest,
            'partitions_sha256': partitions_sha256(), 'install_mode': 'frozen', **overrides}


def on_partition(partition, **overrides):
    return {**spec().resources, 'partition': partition, **overrides}


def pin_for(code_revision):
    return json.dumps({'schema_version': 1, 'code_revision': code_revision, 'policy_sha256': spec().policy_digest})


class CliFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.coordinator = Coordinator(self.root / 'state')
        self.configs = 0

    def config_file(self, coordinator=None, limits=LIMITS, **extra):
        self.configs += 1
        config = self.root / f'config-{self.configs}.json'
        payload = {**dict(schema_version=1, state_root=str((coordinator or self.coordinator).root), principal='fixture', cluster='sherlock', limits=limits, transport={}), **extra}
        config.write_text(json.dumps(payload))
        config.chmod(0o600)
        return str(config)

    def run_main(self, arguments, remote):
        """main() in-process; an exception instance as `remote` fails the test if the network is touched."""
        with patch('sherlock_kit.run_remote', side_effect=remote), patch('sys.stdout', new_callable=io.StringIO) as out, patch('sys.stderr', new_callable=io.StringIO) as err:
            try:
                code = main(arguments)
            except SystemExit as stop:
                code = stop.code
        return code, (json.loads(out.getvalue()) if out.getvalue().strip() else None), err.getvalue()

    def query_keys(self, coordinator=None):
        with closing((coordinator or self.coordinator).connect()) as db:
            return [row[0] for row in db.execute('SELECT key FROM queries')]

    def expire_queries(self, coordinator=None):
        """Age every cached query past the 60 s cadence; the next command must query again."""
        with (coordinator or self.coordinator).transaction() as db:
            db.execute('UPDATE queries SET observed = observed - 120')

    def attempt_count(self, coordinator=None):
        with closing((coordinator or self.coordinator).connect()) as db:
            return db.execute('SELECT count(*) FROM attempts').fetchone()[0]

    def submitted(self, job_id, coordinator=None, limits=LIMITS, **overrides):
        """An admitted attempt whose dispatch was acknowledged with job_id, or whose reply was lost when None."""
        coordinator = coordinator or self.coordinator
        attempt = coordinator.admit(spec(**overrides), limits)['id']
        return coordinator.dispatch(attempt, ack(job_id or '', 0 if job_id else 255))

    def spec_file(self, name, **overrides):
        path = self.root / f'{name}-spec.json'
        path.write_text(json.dumps(dataclasses.asdict(spec(**overrides))))
        return str(path)

    def run_directory(self):
        run = self.root / 'run'
        run.mkdir(exist_ok=True)
        return str(run)

    def test_status_resolves_terminal_through_one_bounded_query(self):
        row = self.submitted('123')
        attempt, created = row['id'], row['created']
        config = self.config_file()
        calls = []
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt], recorder(calls, sacct_line(attempt, created) + '\n'))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['resolution'], output['scientific_validation']), ('terminal', 'unverified'))
        self.assertEqual((output['attempt']['state'], output['attempt']['reserved'], output['attempt']['job_id']), ('terminal', 0, '123'))
        self.assertEqual((output['attempt']['cpu_seconds'], output['attempt']['gpu_seconds'], output['attempt']['cost_known']), (60, 0, 1))
        self.assertEqual(calls, [sacct_argv('fixture', created, '--jobs=123')])
        argv = calls[0]
        self.assertEqual(argv[:9], ['env', 'TZ=UTC', 'LC_ALL=C', 'SLURM_TIME_FORMAT=standard', 'sacct', '-n', '-P', '--local', '--allocations'])
        self.assertEqual(argv[9:13], ['--user=fixture', '--duplicates', '--starttime=' + stamp(created - SUBMIT_TIME_TOLERANCE_SECONDS), '--endtime=now'])
        self.assertTrue(argv[13].startswith('--format=JobID,JobIDRaw,User%128,JobName%128,State,'))
        self.assertEqual((argv[-1], len(argv)), ('--jobs=123', 15))
        # The cadence is reserved per attempt: a second look within 60 s reuses the cached rows.
        code, cached, err = self.run_main(['status', '--config', config, '--attempt', attempt], AssertionError('cadence violated'))
        self.assertEqual((code, err, cached['resolution']), (0, '', 'terminal'))
        self.assertEqual(self.query_keys(), [canonical(['sherlock', 'fixture', CONTROL_HOST, 'sacct', attempt])])
        # A released attempt has left the batch set.
        code, output, err = self.run_main(['status', '--all', '--config', config], AssertionError('network ran'))
        self.assertEqual((code, output, err), (0, [], ''))

    def test_lost_acknowledgement_adopts_the_job_id_by_name_then_queries_by_id(self):
        row = self.submitted(None)
        attempt, created = row['id'], row['created']
        self.assertEqual((row['state'], row['job_id'], row['reserved']), ('unknown', None, 1))
        config = self.config_file()
        calls = []
        running = sacct_line(attempt, created, JobID='777', JobIDRaw='777', State='RUNNING', End='Unknown') + '\n'
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt], recorder(calls, running))
        self.assertEqual((code, err, output['resolution']), (0, '', 'identified'))
        self.assertEqual((output['attempt']['state'], output['attempt']['job_id'], output['attempt']['reserved'], output['attempt']['cost_known']), ('submitted', '777', 1, 0))
        self.assertEqual(calls[0][-1], '--name=shk-' + attempt)
        self.assertFalse(any(flag.startswith('--jobs') for flag in calls[0]))
        # Once identified, the next query selects the job id under the same per-attempt cache key.
        self.expire_queries()
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt], recorder(calls, sacct_line(attempt, created, JobID='777', JobIDRaw='777') + '\n'))
        self.assertEqual((code, err, output['resolution']), (0, '', 'terminal'))
        self.assertEqual((output['attempt']['state'], output['attempt']['reserved'], output['attempt']['cpu_seconds'], output['attempt']['cost_known']), ('terminal', 0, 60, 1))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][-1], '--jobs=777')
        self.assertEqual(calls[1][:-1], calls[0][:-1])
        self.assertEqual(self.query_keys(), [canonical(['sherlock', 'fixture', CONTROL_HOST, 'sacct', attempt])])

    def test_unexpected_preemption_blocks_retry_until_acknowledged(self):
        row = self.submitted('123')
        attempt, created = row['id'], row['created']
        config = self.config_file()
        preempted = sacct_line(attempt, created, State='PREEMPTED') + '\n'
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt], recorder([], preempted))
        self.assertEqual((code, err), (2, 'shk: ' + PREEMPTION_MESSAGE + '\n'))
        self.assertEqual((output['resolution'], output['attempt']['state'], output['attempt']['reserved']), ('unexpected_preemption', 'submitted', 1))
        # The measured cost is recorded as a lower bound but never certified while the anomaly is open.
        self.assertEqual((output['attempt']['cpu_seconds'], output['attempt']['cost_known']), (60, 0))
        with self.assertRaisesRegex(SafetyError, 'unresolved'):
            self.coordinator.admit(spec(parent_attempt=attempt), LIMITS)
        self.assertEqual([item['id'] for item in self.coordinator.unresolved()], [attempt])
        # Acknowledgement is explicit and offline; it prints the released row.
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt, '--acknowledge-preemption'], AssertionError('network ran'))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual((output['id'], output['state'], output['reserved'], output['cost_known'], output['cpu_seconds']), (attempt, 'terminal', 0, 1, 60))
        self.assertEqual(self.coordinator.unresolved(), [])
        self.assertEqual(self.coordinator.admit(spec(parent_attempt=attempt), LIMITS)['spec']['parent_attempt'], attempt)
        # The same scheduler rows seen again are stale for a released attempt, not a new anomaly.
        self.expire_queries()
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt], recorder([], preempted))
        self.assertEqual((code, err, output['resolution']), (0, '', 'stale_observation_ignored'))
        with closing(self.coordinator.connect()) as db:
            kinds = sorted(json.loads(body[0]).get('kind') for body in db.execute('SELECT body FROM evidence WHERE attempt=?', (attempt,)))
        self.assertEqual(kinds, ['operator_ack', 'scheduler', 'transport_ack'])

    def test_owners_requeue_attempt_resolves_from_restart_history(self):
        owners = Coordinator(self.root / 'owners')
        row = self.submitted('555', owners, GPU_LIMITS, resources=on_partition('owners', gpus=1))
        attempt, created = row['id'], row['created']
        self.assertIs(row['spec']['resources']['requeue'], True)
        config = self.config_file(owners, limits=GPU_LIMITS)
        gpu = {'JobID': '555', 'JobIDRaw': '555', 'AllocTRES': 'cpu=1,gres/gpu=1'}
        first = sacct_line(attempt, created, State='PREEMPTED', ElapsedRaw='30', End=stamp(created + 31), **gpu)
        running = sacct_line(attempt, created, State='RUNNING', Restarts='1', ElapsedRaw='20', Start=stamp(created + 100), End='Unknown', DBIndex='43', **gpu)
        code, output, err = self.run_main(['status', '--config', config, '--attempt', attempt], recorder([], first + '\n' + running + '\n'))
        self.assertEqual((code, err, output['resolution']), (0, '', 'identified'))
        self.assertEqual((output['attempt']['state'], output['attempt']['reserved'], output['attempt']['cost_known']), ('submitted', 1, 0))
        # The finished restart is a stored lower bound; the running one is not yet measured.
        self.assertEqual((output['attempt']['cpu_seconds'], output['attempt']['gpu_seconds']), (30, 30))
        self.expire_queries(owners)
        done = sacct_line(attempt, created, State='COMPLETED', Restarts='1', Start=stamp(created + 100), End=stamp(created + 160), DBIndex='43', **gpu)
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', attempt], recorder([], first + '\n' + done + '\n'))
        self.assertEqual((code, err, output['resolution']), (0, '', 'terminal'))
        self.assertEqual((output['attempt']['state'], output['attempt']['reserved'], output['attempt']['cost_known']), ('terminal', 0, 1))
        self.assertEqual((output['attempt']['cpu_seconds'], output['attempt']['gpu_seconds']), (90, 90))
        self.assertEqual(owners.unresolved(), [])

    def test_submit_preview_is_offline_and_refuses_ineligible_specs(self):
        config = self.config_file()
        code, output, err = self.run_main(['submit', '--config', config, '--spec', self.spec_file('normal')], AssertionError('network ran'))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(list(output), ['operation', 'resources', 'spec_digest'])
        self.assertEqual((output['operation'], output['resources']), ('preview', {**spec().resources, 'requeue': False}))
        self.assertRegex(output['spec_digest'], '^[0-9a-f]{64}$')
        owners = self.spec_file('owners', resources=on_partition('owners', gpus=1))
        code, output, err = self.run_main(['submit', '--config', config, '--spec', owners], AssertionError('network ran'))
        self.assertEqual((code, output['resources']['requeue'], output['resources']['gpus']), (0, True, 1))
        refused = {'unknown partition': on_partition('gpu'), 'GPU': on_partition('normal', gpus=1), 'requeue': on_partition('btrippe', requeue=True)}
        for message, resources in refused.items():
            path = self.spec_file(message.replace(' ', '-'), resources=resources)
            for arguments in (['submit', '--config', config, '--spec', path], ['submit', '--config', config, '--spec', path, '--apply']):
                with self.subTest(message=message, apply='--apply' in arguments):
                    code, output, err = self.run_main(arguments, AssertionError('network ran'))
                    self.assertEqual((code, output), (2, None))
                    self.assertTrue(err.startswith('shk: '), err)
                    self.assertIn(message, err)
        self.assertEqual(self.attempt_count(), 0)

    def test_submit_apply_gates_run_in_order_before_any_remote_contact(self):
        config = self.config_file()
        apply = ['submit', '--config', config, '--spec', self.spec_file('ready', remote_run_directory=self.run_directory()), '--apply']
        pin = self.root / 'pin.json'
        with patch.dict(os.environ, {'SHERLOCK_KIT_PIN': str(pin)}):
            pin.write_text(pin_for('0' * 40))
            # 1. The frozen spec itself, before the installed identity is consulted.
            with patch('sherlock_kit.policy_identity', side_effect=AssertionError('identity consulted')):
                code, output, err = self.run_main(['submit', '--config', config, '--spec', self.spec_file('bare'), '--apply'], AssertionError('network ran'))
                self.assertEqual((code, output), (2, None))
                self.assertIn('remote_run_directory', err)
            # 2. The installed toolkit must be frozen and carry the spec's policy, whatever the pin says.
            gates = {'production admission requires a frozen installed toolkit': frozen_identity(install_mode='development'),
                     'new attempt policy differs from installed policy': frozen_identity(policy_sha256='b' * 64),
                     'advertised/installed revision mismatch': frozen_identity()}
            for message, identity in gates.items():
                with self.subTest(gate=message), patch('sherlock_kit.policy_identity', return_value=identity):
                    code, output, err = self.run_main(apply, AssertionError('network ran'))
                    self.assertEqual((code, output), (2, None))
                    self.assertIn(message, err)
            # 3. With a matching pin, exactly one principal probe; admission needs the authenticated principal.
            pin.write_text(pin_for('c' * 40))
            probes = {'other_principal': complete('someone-else\n'),
                      'auth_required': RemoteResult('auth_required', stderr='Permission denied', returncode=255, dispatched=True)}
            for name, probe in probes.items():
                calls = []
                def remote(transport, argv, mutation=False, probe=probe):
                    calls.append((list(argv), mutation))
                    return probe
                with self.subTest(probe=name), patch('sherlock_kit.policy_identity', return_value=frozen_identity()):
                    code, output, err = self.run_main(apply, remote)
                    self.assertEqual((code, output), (2, None))
                    self.assertIn('authenticated principal not established before admission', err)
                    self.assertEqual(calls, [(PROBE, False)])
        self.assertEqual(self.attempt_count(), 0)

    def test_submit_apply_dispatches_the_frozen_profile_and_requeue_decision(self):
        run = self.run_directory()
        apply = ['submit', '--config', self.config_file(), '--spec', self.spec_file('ready', remote_run_directory=run), '--apply']
        calls = []
        def remote(transport, argv, mutation=False):
            calls.append((list(argv), mutation))
            return complete('fixture\n' if argv == PROBE else '4242\n')
        with patch.dict(os.environ), patch('sherlock_kit.policy_identity', return_value=frozen_identity()):
            os.environ.pop('SHERLOCK_KIT_PIN', None)
            code, output, err = self.run_main(apply, remote)
            self.assertEqual((code, err), (0, ''))
            self.assertEqual((output['state'], output['job_id'], output['reserved']), ('submitted', '4242', 1))
            frozen = output['spec']
            self.assertEqual(frozen['partition_profile'], dict(partition_profile('normal')))
            self.assertEqual((frozen['resources']['requeue'], frozen['toolkit_revision'], frozen['partitions_sha256'], frozen['grant']), (False, 'c' * 40, partitions_sha256(), None))
            self.assertEqual([(argv[:2], mutation) for argv, mutation in calls], [(PROBE, False), (['python3', '-c'], True)])
            command = calls[1][0]
            self.assertEqual(command[3:6], [output['id'], spec().remote_script, spec().script_digest])
            self.assertEqual(command[7], run)
            options = json.loads(command[6])
            self.assertEqual(options[:3], ['sbatch', '--parsable', '--job-name=shk-' + output['id']])
            for flag in ('--partition=normal', '--no-requeue', '--chdir=' + run):
                self.assertIn(flag, options)
            self.assertFalse(any(flag in options for flag in ('--requeue', '--open-mode=append', '-G')))
            self.assertEqual(self.coordinator.get(output['id'])['state'], 'submitted')
            # The open attempt blocks a second submission of the same logical task; the probe still precedes admission.
            calls.clear()
            code, output, err = self.run_main(apply, remote)
            self.assertEqual((code, output), (2, None))
            self.assertIn('unresolved/active attempt', err)
            self.assertEqual((calls, self.attempt_count()), ([(PROBE, False)], 1))
            # An owners spec freezes the preemptible profile and asks Slurm to requeue with appended output.
            owners = Coordinator(self.root / 'owners')
            gpu = self.spec_file('owners', remote_run_directory=run, resources=on_partition('owners', gpus=1))
            code, output, err = self.run_main(['submit', '--config', self.config_file(owners, limits=GPU_LIMITS), '--spec', gpu, '--apply'], remote)
            self.assertEqual((code, err), (0, ''))
            self.assertEqual((output['state'], output['job_id'], output['spec']['resources']['requeue']), ('submitted', '4242', True))
            self.assertEqual(output['spec']['partition_profile'], dict(partition_profile('owners')))
            options = json.loads(calls[-1][0][6])
            for flag in ('--partition=owners', '--requeue', '--open-mode=append'):
                self.assertIn(flag, options)
            self.assertEqual(options[options.index('-G') + 1], '1')
            self.assertNotIn('--no-requeue', options)

    def fetch_fixture(self, task):
        """An admitted attempt with a frozen validator, its producer bundle and manifest, and the fetch arguments."""
        validator = self.root / 'validator.py'
        validator.write_text('def validate(root): return (root / "result.json").read_text() == "42\\n"\n')
        frozen = spec(task=task, validator_path=str(validator), validator_digest=hashlib.sha256(validator.read_bytes()).hexdigest(), validator_function='validate')
        admitted = self.coordinator.admit(frozen, LIMITS)
        source = self.root / 'producer'
        source.mkdir(exist_ok=True)
        (source / 'result.json').write_text('42\n')
        producer = {key: admitted['spec'][key] for key in IDENTITY_KEYS}
        producer['attempt'] = admitted['id']
        fetch_root = self.root / 'fetch'
        fetch_root.mkdir(exist_ok=True)
        config = self.config_file(fetch_root=str(fetch_root), validator_roots=[str(self.root)],
                                  remote_roots={'control': '/authorized', 'data': '/authorized', 'namespace_verified': True})
        arguments = ['fetch', '--config', config, '--attempt', admitted['id'], '--manifest', REMOTE_SCOPE[2], '--source-root', REMOTE_SCOPE[1], '--destination', str(fetch_root / task)]
        return SimpleNamespace(attempt=admitted['id'], manifest=build_manifest(source, producer), source=source, fetch_root=fetch_root,
                               destination=fetch_root / task, arguments=arguments,
                               receipt_digest=digest({'sha256': frozen.validator_digest, 'function': 'validate'}))

    def control_endpoint(self, served, events):
        """The control endpoint serving whatever manifest `served['manifest']` currently holds."""
        def remote(transport, argv, mutation=False):
            self.assertEqual((argv[:3], argv[3:], mutation), (['python3', '-c', REMOTE_MANIFEST], REMOTE_SCOPE, False))
            events.append('manifest')
            return complete(json.dumps(served['manifest']))
        return remote

    def data_endpoint(self, fixture, events, after=None):
        """A data transfer that copies the manifest's files into the stage, then runs `after`."""
        def transfer(transport, remote_source, stage, manifest):
            events.append(('transfer', transport.data_host, remote_source, Path(stage), manifest))
            for item in manifest['files']:
                shutil.copy(fixture.source / item['path'], Path(stage) / item['path'])
            if after is not None:
                after()
        return transfer

    def test_remote_fetch_pins_rereads_transfers_once_and_promotes(self):
        fixture = self.fetch_fixture('verified')
        events = []
        with patch('sherlock_kit.data_transfer', side_effect=self.data_endpoint(fixture, events)):
            code, output, err = self.run_main(fixture.arguments, self.control_endpoint({'manifest': fixture.manifest}, events))
        self.assertEqual((code, err), (0, ''))
        receipt = {'schema_version': 1, 'manifest_sha256': digest(fixture.manifest), 'producer': fixture.manifest['producer'], 'validator_sha256': fixture.receipt_digest}
        self.assertEqual(output, {'destination': str(fixture.destination), 'receipt': receipt, 'recovered': False})
        self.assertEqual(json.loads((fixture.fetch_root / '.verified.shk-receipt.json').read_text()), receipt)
        self.assertEqual(json.loads((fixture.fetch_root / '.verified.shk-transaction.json').read_text()), receipt)
        self.assertEqual((fixture.destination / 'result.json').read_text(), '42\n')
        self.assertEqual(self.coordinator.pinned_manifest(fixture.attempt), fixture.manifest)
        # One transfer: the data host is addressed inside the toolkit, the source stays host-less, the stage is private.
        transfers = [event for event in events if event != 'manifest']
        self.assertEqual(len(transfers), 1)
        _, host, remote_source, stage, transferred = transfers[0]
        self.assertEqual((host, remote_source, transferred), ('sherlock-dtn', REMOTE_SCOPE[1], fixture.manifest))
        self.assertEqual(stage, fixture.fetch_root / ('.verified.shk-stage-' + digest(fixture.manifest)))
        self.assertFalse(stage.exists())
        # The manifest is read before the transfer and re-read for stability afterwards.
        position = events.index(transfers[0])
        self.assertEqual(events[0], 'manifest')
        self.assertIn('manifest', events[position + 1:])
        # Recovery afterwards needs neither endpoint.
        with patch('sherlock_kit.data_transfer', side_effect=AssertionError('transfer ran')):
            code, output, err = self.run_main(fixture.arguments[:1] + ['--local'] + fixture.arguments[1:], AssertionError('network ran'))
        self.assertEqual((code, err, output['recovered']), (0, '', True))

    def test_fetch_never_promotes_when_the_manifest_changes_mid_transfer(self):
        fixture = self.fetch_fixture('moving')
        changed = {**fixture.manifest, 'files': [{**fixture.manifest['files'][0], 'sha256': 'f' * 64}]}
        served, events = {'manifest': fixture.manifest}, []
        def replace_manifest():
            served['manifest'] = changed
        with patch('sherlock_kit.data_transfer', side_effect=self.data_endpoint(fixture, events, after=replace_manifest)):
            code, output, err = self.run_main(fixture.arguments, self.control_endpoint(served, events))
        self.assertEqual((code, output, err), (2, None, 'shk: source manifest changed during transfer\n'))
        self.assertEqual(sum(1 for event in events if event != 'manifest'), 1)
        self.assertFalse(fixture.destination.exists())
        self.assertFalse((fixture.fetch_root / '.moving.shk-receipt.json').exists())
        self.assertTrue((fixture.fetch_root / '.moving.shk-transaction.json').is_file())
        # The pinned manifest is the one read first; the transferred bytes stay in the private stage for a resume.
        self.assertEqual(self.coordinator.pinned_manifest(fixture.attempt), fixture.manifest)
        stage = fixture.fetch_root / ('.moving.shk-stage-' + digest(fixture.manifest))
        self.assertEqual((stage / 'result.json').read_text(), '42\n')
        self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
        # Local recovery cannot promote either: nothing was promoted.
        with patch('sherlock_kit.data_transfer', side_effect=AssertionError('transfer ran')):
            code, output, err = self.run_main(fixture.arguments[:1] + ['--local'] + fixture.arguments[1:], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('already promoted destination', err)

    def test_fetch_pins_nothing_when_the_control_manifest_is_unavailable(self):
        fixture = self.fetch_fixture('absent')
        unavailable = lambda transport, argv, mutation=False: RemoteResult('unavailable', stderr='Connection timed out', returncode=255, dispatched=True)
        with patch('sherlock_kit.data_transfer', side_effect=AssertionError('transfer ran')):
            code, output, err = self.run_main(fixture.arguments, unavailable)
        self.assertEqual((code, output, err), (2, None, 'shk: control manifest unavailable; no unverified transfer\n'))
        with self.assertRaisesRegex(SafetyError, 'no durable manifest'):
            self.coordinator.pinned_manifest(fixture.attempt)
        self.assertEqual(list(fixture.fetch_root.iterdir()), [])
        with patch('sherlock_kit.data_transfer', side_effect=AssertionError('transfer ran')):
            code, output, err = self.run_main(fixture.arguments[:1] + ['--local'] + fixture.arguments[1:], AssertionError('network ran'))
        self.assertEqual((code, output), (2, None))
        self.assertIn('no durable manifest', err)

    def test_occupancy_reports_the_borrowed_partition_from_one_cached_squeue(self):
        payload = '\n'.join((squeue_line('alice', 'RUNNING', 'cpu=8,mem=64G,node=1,billing=8,gres/gpu=2,gres/gpu:a100=2'),
                             squeue_line('alice', 'PENDING', ''),
                             squeue_line('bob', 'COMPLETING', 'cpu=4,gres/gpu:a100=1'),
                             squeue_line('carol', 'CANCELLED', 'cpu=1,gres/gpu=1'))) + '\n'
        config = self.config_file()
        calls = []
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'btrippe'], recorder(calls, payload))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(calls, [['env', 'LC_ALL=C', 'squeue', '-h', '-p', 'btrippe', '-O', SQUEUE_FORMAT]])
        self.assertEqual(list(report), ['partition', 'profile', 'totals', 'transport', 'users'])
        self.assertEqual((report['partition'], report['transport'], report['profile']), ('btrippe', 'complete', dict(partition_profile('btrippe'))))
        self.assertTrue(report['profile']['borrowed'] and report['profile']['gpus_allowed'])
        self.assertIn('shk occupancy', report['profile']['courtesy'])
        self.assertEqual(report['users'], {'alice': {'running_jobs': 1, 'running_gpus': 2, 'pending_jobs': 1},
                                           'bob': {'running_jobs': 1, 'running_gpus': 1, 'pending_jobs': 0}})
        self.assertEqual(report['totals'], {'running_jobs': 2, 'running_gpus': 3, 'pending_jobs': 1})
        # Within the cadence the same partition is answered from the cache; another partition is its own query.
        code, cached, err = self.run_main(['occupancy', '--config', config, '--partition', 'btrippe'], AssertionError('cadence violated'))
        self.assertEqual((code, cached), (0, report))
        code, idle, err = self.run_main(['occupancy', '--config', config, '--partition', 'owners'], recorder(calls, ''))
        self.assertEqual((code, idle['users'], idle['totals']), (0, {}, {'running_jobs': 0, 'running_gpus': 0, 'pending_jobs': 0}))
        self.assertEqual((len(calls), calls[1][5]), (2, 'owners'))
        self.assertEqual(sorted(self.query_keys()), sorted(canonical(['sherlock', 'fixture', CONTROL_HOST, 'squeue', name]) for name in ('btrippe', 'owners')))
        # Occupancy gates nothing: an unreachable scheduler still prints the profile and exits 1.
        down = lambda transport, argv, mutation=False: RemoteResult('unavailable', stderr='Connection timed out', returncode=255, dispatched=True)
        code, report, err = self.run_main(['occupancy', '--config', config, '--partition', 'normal'], down)
        self.assertEqual((code, err, report['transport'], report['users'], report['profile']), (1, '', 'unavailable', {}, dict(partition_profile('normal'))))

    def test_reconcile_all_resolves_a_mixed_set_through_one_query(self):
        coordinator = Coordinator(self.root / 'batch')
        done = self.submitted('101', coordinator, MANY, task='done')
        lost = self.submitted(None, coordinator, MANY, task='lost')
        silent = self.submitted('103', coordinator, MANY, task='silent')
        coordinator.admit(spec(task='unsent'), MANY)  # never dispatched: not part of the batch
        anomaly = self.submitted('104', coordinator, MANY, task='anomaly')
        restarted = self.submitted('105', coordinator, MANY, task='restarted', resources=on_partition('owners'))
        batch = [done, lost, silent, anomaly, restarted]
        self.assertEqual([row['id'] for row in coordinator.unresolved()], [row['id'] for row in batch])
        rows = '\n'.join((sacct_line(done['id'], done['created'], JobID='101', JobIDRaw='101'),
                          sacct_line(lost['id'], lost['created'], JobID='102', JobIDRaw='102', State='RUNNING', End='Unknown', DBIndex='43'),
                          sacct_line(anomaly['id'], anomaly['created'], JobID='104', JobIDRaw='104', State='PREEMPTED', DBIndex='44'),
                          sacct_line(restarted['id'], restarted['created'], JobID='105', JobIDRaw='105', State='PREEMPTED', ElapsedRaw='30', End=stamp(restarted['created'] + 31), DBIndex='45'),
                          sacct_line(restarted['id'], restarted['created'], JobID='105', JobIDRaw='105', State='COMPLETED', Restarts='1', Start=stamp(restarted['created'] + 100), End=stamp(restarted['created'] + 160), DBIndex='46'))) + '\n'
        config = self.config_file(coordinator, limits=MANY)
        calls = []
        code, output, err = self.run_main(['reconcile', '--all', '--config', config], recorder(calls, rows))
        self.assertEqual((code, err), (2, 'shk: ' + PREEMPTION_MESSAGE + '\n'))
        self.assertEqual([item['attempt']['id'] for item in output], [row['id'] for row in batch])
        self.assertEqual([item['resolution'] for item in output], ['terminal', 'identified', 'inconclusive', 'unexpected_preemption', 'terminal'])
        self.assertEqual((output[0]['attempt']['reserved'], output[0]['attempt']['cpu_seconds']), (0, 60))
        self.assertEqual((output[1]['attempt']['job_id'], output[1]['attempt']['state']), ('102', 'submitted'))
        self.assertEqual(output[2]['reason'], 'absence/lag/retention never proves non-submission')
        self.assertEqual((output[3]['attempt']['state'], output[3]['attempt']['reserved']), ('submitted', 1))
        self.assertEqual((output[4]['attempt']['cpu_seconds'], output[4]['attempt']['cost_known'], output[4]['attempt']['reserved']), (90, 1, 0))
        self.assertEqual(len(calls), 1)
        argv = calls[0]
        self.assertEqual(argv[-1], '--name=' + ','.join('shk-' + row['id'] for row in batch))
        self.assertFalse(any(flag.startswith('--jobs') for flag in argv))
        self.assertIn('--starttime=' + stamp(min(row['created'] for row in batch) - SUBMIT_TIME_TOLERANCE_SECONDS), argv)
        self.assertEqual(self.query_keys(coordinator), [canonical(['sherlock', 'fixture', CONTROL_HOST, 'sacct-all'])])
        # Once the anomaly is acknowledged, the next batch holds only the still-open attempts, selected by name.
        code, output, err = self.run_main(['reconcile', '--config', config, '--attempt', anomaly['id'], '--acknowledge-preemption'], AssertionError('network ran'))
        self.assertEqual((code, err, output['state'], output['reserved']), (0, '', 'terminal', 0))
        self.expire_queries(coordinator)
        code, output, err = self.run_main(['status', '--all', '--config', config], recorder(calls, rows))
        self.assertEqual((code, err), (0, ''))
        self.assertEqual([(item['attempt']['id'], item['resolution']) for item in output], [(lost['id'], 'identified'), (silent['id'], 'inconclusive')])
        self.assertEqual(calls[1][-1], '--name=shk-' + lost['id'] + ',shk-' + silent['id'])


if __name__ == '__main__':
    unittest.main()
