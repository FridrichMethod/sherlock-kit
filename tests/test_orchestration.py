from contextlib import closing
import dataclasses
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sherlock_orchestration import (AttemptSpec, BASE_TERMINAL, Coordinator, SUBMIT_TIME_TOLERANCE_SECONDS, SafetyError, TERMINAL, canonical,
                                    resources_checked, scheduler_cost, submission_argv, terminal_states)
from sherlock_partitions import partition_profile, partitions_sha256

D = 'a' * 64
LIMITS = {'cpus': 2, 'gpus': 0, 'tasks': 2, 'cpu_seconds': 3600}


def spec(**kwargs):
    data = dict(project='consumer', campaign='new-pilot', task='test', cluster='sherlock', principal='fixture', resource_scope='shared', code_digest=D, input_digest=D, runtime_digest=D, policy_digest=D, resources={'partition': 'normal', 'cpus': 1, 'gpus': 0, 'tasks': 1, 'memory_mb': 1024, 'walltime_seconds': 600}, remote_script='/synthetic/releases/frozen/job.sh', script_digest=D)
    data.update(kwargs)
    return AttemptSpec(**data)


def frozen_spec(attempt_spec, **profile_overrides):
    """Frozen attempt body as admit() stores it: checked resources plus the packaged profile."""
    resources = resources_checked(attempt_spec.resources)
    profile = {**dict(partition_profile(resources['partition'])), **profile_overrides}
    return {**dataclasses.asdict(attempt_spec), 'resources': resources, 'partition_profile': profile}


def concurrent_admit(root, start, result, task='test'):
    coordinator = Coordinator(Path(root))
    start.wait()
    try:
        record = coordinator.admit(spec(task=task), LIMITS)
        result.put(('accepted', record['id']))
    except SafetyError:
        result.put(('blocked', None))


def die_after_claim(root, attempt):
    coordinator = Coordinator(Path(root))
    def runner(argv):
        os._exit(17)
    coordinator.dispatch(attempt, runner)


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'private'
        self.coordinator = Coordinator(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def admit(self, **kwargs):
        return self.coordinator.admit(spec(**kwargs), LIMITS)

    def evidence(self, attempt, **kwargs):
        record = self.coordinator.get(attempt)
        evidence = {key: record['spec'][key] for key in ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest')}
        evidence.update(attempt=attempt, submitted_at=record['created'], job_id='123', state='COMPLETED', cost_known=True, accounting_complete=True, cpu_seconds=25, gpu_seconds=0)
        evidence.update(kwargs)
        return evidence

    def test_two_worktree_controllers_deduplicate_task(self):
        start = multiprocessing.Event()
        result = multiprocessing.Queue()
        workers = [multiprocessing.Process(target=concurrent_admit, args=(str(self.root), start, result)) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.set()
        answers = [result.get(timeout=10) for _ in workers]
        for worker in workers:
            worker.join(10)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual(sorted(x[0] for x in answers), ['accepted', 'blocked'])

    def test_shared_scope_across_projects(self):
        self.admit(project='one', task='one')
        self.admit(project='two', task='two')
        with self.assertRaisesRegex(SafetyError, 'concurrency'):
            self.admit(project='three', task='three')

    def test_process_death_after_durable_dispatch_claim_retains_reservation(self):
        attempt = self.admit()['id']
        process = multiprocessing.Process(target=die_after_claim, args=(str(self.root), attempt))
        process.start()
        process.join(10)
        self.assertEqual(process.exitcode, 17)
        self.coordinator.recover()
        row = self.coordinator.get(attempt)
        self.assertEqual((row['state'], row['reserved']), ('unknown', 1))
        with self.assertRaises(SafetyError):
            self.coordinator.dispatch(attempt, lambda _: self.fail('duplicate dispatch'))
        with self.assertRaises(SafetyError):
            self.admit()

    def test_crash_before_claim_preserves_one_dispatchable_intent(self):
        attempt = self.admit()['id']
        self.coordinator.recover()
        result = self.coordinator.dispatch(attempt, lambda _: SimpleNamespace(dispatched=True, returncode=0, stdout='123;sherlock\n'))
        self.assertEqual(result['state'], 'submitted')
        with self.assertRaises(SafetyError):
            self.coordinator.dispatch(attempt, lambda _: None)

    def test_disconnect_timeout_and_malformed_output_are_unknown(self):
        for idx, (code, output) in enumerate(((255, ''), (0, '123;cluster\nextra'), (0, '0'), (0, '123;garbage;again'), (1, 'Slurm rejected'))):
            with self.subTest(output=output, code=code):
                temp = Path(self.temp.name) / str(idx)
                coordinator = Coordinator(temp)
                attempt = coordinator.admit(spec(), LIMITS)['id']
                row = coordinator.dispatch(attempt, lambda _: SimpleNamespace(dispatched=True, returncode=code, stdout=output))
                self.assertEqual((row['state'], row['reserved']), ('unknown', 1))
        attempt = self.admit()['id']
        with self.assertRaises(TimeoutError):
            self.coordinator.dispatch(attempt, lambda _: (_ for _ in ()).throw(TimeoutError()))
        self.assertEqual(self.coordinator.get(attempt)['state'], 'unknown')

    def test_proven_non_dispatch_and_remote_rejection_release(self):
        attempt = self.admit()['id']
        row = self.coordinator.dispatch(attempt, lambda _: SimpleNamespace(dispatched=False, returncode=255, stdout=''))
        self.assertEqual((row['state'], row['reserved']), ('not_sent', 0))
        second = self.admit(parent_attempt=attempt)['id']
        row = self.coordinator.dispatch(second, lambda _: SimpleNamespace(dispatched=True, returncode=0, stdout=f'SHK_NOT_SENT:{second}:digest_mismatch'))
        self.assertEqual((row['state'], row['reserved']), ('not_sent', 0))

    def test_zero_accounting_matches_never_authorize_retry(self):
        attempt = self.admit()['id']
        self.coordinator.dispatch(attempt, lambda _: SimpleNamespace(dispatched=True, returncode=255, stdout=''))
        for _ in range(3):
            self.assertEqual(self.coordinator.reconcile(attempt, [])['resolution'], 'inconclusive')
        with self.assertRaises(SafetyError):
            self.admit(parent_attempt=attempt)

    def test_identity_job_reuse_conflicts_and_budget_idempotence(self):
        attempt = self.admit()['id']
        self.coordinator.dispatch(attempt, lambda _: SimpleNamespace(dispatched=True, returncode=0, stdout='123'))
        for wrong in ({'principal': 'foreign'}, {'policy_digest': 'b' * 64}, {'submitted_at': 0}, {'job_id': '124'}):
            with self.subTest(wrong=wrong), self.assertRaises(SafetyError):
                self.coordinator.reconcile(attempt, [self.evidence(attempt, **wrong)])
        for _ in range(2):
            result = self.coordinator.reconcile(attempt, [self.evidence(attempt)])
            self.assertEqual(result['attempt']['cpu_seconds'], 25)
            self.assertEqual(result['attempt']['reserved'], 0)
        with self.assertRaises(SafetyError):
            self.admit(parent_attempt=attempt)

    def test_policy_update_preserves_old_recovery_and_blocks_new_mismatch(self):
        attempt = self.admit()['id']
        with self.assertRaisesRegex(SafetyError, 'mismatch'):
            self.coordinator.admit(spec(task='new'), LIMITS, advertised_policy='b' * 64)
        self.coordinator.reconcile(attempt, [self.evidence(attempt)])
        self.assertEqual(self.coordinator.get(attempt)['spec']['policy_digest'], D)

    def test_grant_expiry_and_shared_caps(self):
        grant = {'grantee': 'fixture', 'evidence_reference': 'private-evidence', 'valid_from': 1, 'valid_until': 100, 'scope': 'shared', 'partitions': ['btrippe'], 'limits': LIMITS}
        borrowed = spec(resources={**spec().resources, 'partition': 'btrippe'})
        with self.assertRaises(SafetyError):
            self.coordinator.admit(borrowed, LIMITS, now=50)
        attempt = self.coordinator.admit(borrowed, LIMITS, grant=grant, now=50)['id']
        with self.assertRaises(SafetyError):
            self.coordinator.admit(dataclasses.replace(borrowed, task='two'), LIMITS, grant=grant, now=101)
        evidence = self.evidence(attempt, submitted_at=50)
        self.coordinator.reconcile(attempt, [evidence])

    def test_running_cost_is_not_final_and_stale_state_cannot_regress(self):
        attempt = self.admit()['id']
        running = self.evidence(attempt, state='RUNNING', cpu_seconds=10, accounting_complete=False)
        self.coordinator.reconcile(attempt, [running])
        done = self.evidence(attempt, cost_known=False, accounting_complete=False)
        self.coordinator.reconcile(attempt, [done])
        self.assertEqual(self.coordinator.get(attempt)['cost_known'], 0)
        result = self.coordinator.reconcile(attempt, [running])
        self.assertEqual(result['resolution'], 'stale_observation_ignored')
        self.assertEqual(self.coordinator.get(attempt)['reserved'], 0)
        with self.assertRaises(SafetyError):
            self.coordinator.reconcile(attempt, [self.evidence(attempt), self.evidence(attempt, state='FAILED')])

    def test_recover_races_with_ack_and_ack_is_retained(self):
        attempt = self.admit()['id']
        def runner(argv):
            self.coordinator.recover()
            return SimpleNamespace(dispatched=True, returncode=0, stdout='123', stderr='')
        self.assertEqual(self.coordinator.dispatch(attempt, runner)['state'], 'submitted')
        with closing(self.coordinator.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM evidence WHERE attempt=?', (attempt,)).fetchone()[0], 1)

    def test_scope_limit_mismatch_and_different_authority_fail(self):
        self.admit()
        with self.assertRaises(SafetyError):
            self.coordinator.admit(spec(task='other'), {**LIMITS, 'cpus': 10})
        with self.assertRaisesRegex(SafetyError, 'authority|authoritative'):
            Coordinator(self.root, authority='different-workstation')

    def test_malformed_state_is_never_reset(self):
        bad = Path(self.temp.name) / 'bad'
        bad.mkdir(mode=0o700)
        (bad / 'coordinator.sqlite3').write_bytes(b'not a SQLite database')
        with self.assertRaises(SafetyError):
            Coordinator(bad)
        self.assertEqual((bad / 'coordinator.sqlite3').read_bytes(), b'not a SQLite database')

    def test_shared_query_cache_reserves_cadence_even_on_error(self):
        calls = []
        self.coordinator.cached_query('same-scope', lambda: calls.append(1) or {'jobs': []}, now=10)
        self.coordinator.cached_query('same-scope', lambda: calls.append(1), now=20)
        self.assertEqual(calls, [1])
        self.coordinator.cached_query('same-scope', lambda: calls.append(1) or {}, now=70)
        self.assertEqual(calls, [1, 1])
        with self.assertRaises(TimeoutError):
            self.coordinator.cached_query('failed', lambda: (_ for _ in ()).throw(TimeoutError()), now=100)
        self.assertEqual(self.coordinator.cached_query('failed', lambda: self.fail(), now=101)['status'], 'query_pending_or_failed')

    def test_arrays_steps_duplicates_and_restarts_cost(self):
        rows = [{'job_id': '123', 'array_parent': True, 'cpu_seconds': 100, 'gpu_seconds': 0}, {'job_id': '123_0', 'cpu_seconds': 10, 'gpu_seconds': 0, 'start_time': 1}, {'job_id': '123_0.batch', 'cpu_seconds': 10, 'gpu_seconds': 0}, {'job_id': '123_0', 'cpu_seconds': 10, 'gpu_seconds': 0, 'start_time': 1}, {'job_id': '123_0', 'cpu_seconds': 15, 'gpu_seconds': 0, 'start_time': 2, 'restart': 1, 'restart_history_complete': True}]
        self.assertEqual(scheduler_cost(rows)['cpu_seconds'], 25)
        self.assertFalse(scheduler_cost([{'job_id': '124', 'cpu_seconds': None, 'gpu_seconds': None}])['known'])

    def test_site_resources_and_constraint_signal_grammar(self):
        r = resources_checked({**spec().resources, 'constraint': 'CPU_GEN1|CPU_GEN2', 'signal': 'B:USR1@60'})
        argv = submission_argv('1' * 32, {**dataclasses.asdict(spec()), 'resources': r, 'partition_profile': dict(partition_profile('normal'))})
        self.assertIn('--constraint=CPU_GEN1|CPU_GEN2', argv[-2])
        for wrong in ({'account': 'group'}, {'exclude': 'node'}, {'cpus': True}, {'constraint': 'cpu; false'}, {'signal': 'B:USR1@60\n'}):
            with self.subTest(wrong=wrong), self.assertRaises(SafetyError):
                resources_checked({**spec().resources, **wrong})



class PartitionProfileAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.coordinator = Coordinator(Path(self.temp.name) / 'private')

    def resources(self, partition='normal', **overrides):
        return {**spec().resources, 'partition': partition, **overrides}

    def test_constants_and_terminal_sets(self):
        self.assertEqual(SUBMIT_TIME_TOLERANCE_SECONDS, 300)
        self.assertIs(TERMINAL, BASE_TERMINAL)
        self.assertEqual(BASE_TERMINAL, {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMEOUT', 'NODE_FAIL', 'OUT_OF_MEMORY', 'BOOT_FAIL', 'DEADLINE'})
        owners, normal = partition_profile('owners'), partition_profile('normal')
        self.assertEqual(terminal_states({'requeue': False}, owners), BASE_TERMINAL | {'PREEMPTED'})
        self.assertEqual(terminal_states({'requeue': True}, owners), BASE_TERMINAL)
        self.assertEqual(terminal_states({'requeue': False}, normal), BASE_TERMINAL)
        self.assertEqual(terminal_states({}, normal), BASE_TERMINAL)

    def test_unknown_partition_refused_offline(self):
        with self.assertRaisesRegex(SafetyError, 'unknown partition'):
            resources_checked(self.resources('gpu'))
        with self.assertRaisesRegex(SafetyError, 'unknown partition'):
            self.coordinator.admit(spec(resources=self.resources('gpu')), LIMITS)
        with closing(self.coordinator.connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_gpus_follow_the_profile(self):
        with self.assertRaisesRegex(SafetyError, 'GPU'):
            resources_checked(self.resources('normal', gpus=1))
        for partition in ('owners', 'btrippe'):
            with self.subTest(partition=partition):
                self.assertEqual(resources_checked(self.resources(partition, gpus=1))['gpus'], 1)

    def test_requeue_follows_the_profile(self):
        for partition in ('normal', 'btrippe'):
            with self.subTest(partition=partition):
                with self.assertRaisesRegex(SafetyError, 'requeue'):
                    resources_checked(self.resources(partition, requeue=True))
                self.assertIs(resources_checked(self.resources(partition))['requeue'], False)
        self.assertIs(resources_checked(self.resources('owners'))['requeue'], True)
        self.assertIs(resources_checked(self.resources('owners', requeue=False))['requeue'], False)
        with self.assertRaisesRegex(SafetyError, 'boolean'):
            resources_checked(self.resources('owners', requeue='yes'))

    def options(self, frozen):
        return json.loads(submission_argv('1' * 32, frozen)[-2])

    def test_submission_argv_emits_profile_bound_requeue_flags(self):
        normal = self.options(frozen_spec(spec()))
        self.assertIn('--no-requeue', normal)
        self.assertNotIn('--requeue', normal)
        owners = self.options(frozen_spec(spec(resources=self.resources('owners', gpus=1))))
        self.assertIn('--requeue', owners)
        self.assertIn('--open-mode=append', owners)
        self.assertNotIn('--no-requeue', owners)
        self.assertEqual(owners[owners.index('-G') + 1], '1')
        self.assertIn('--no-requeue', self.options(frozen_spec(spec(resources=self.resources('owners', requeue=False)))))
        self.assertIn('--no-requeue', self.options(frozen_spec(spec(resources=self.resources('btrippe')))))

    def test_submission_argv_reads_only_the_frozen_profile(self):
        frozen = frozen_spec(spec(), gpus_allowed=True)
        frozen['resources']['gpus'] = 1
        self.assertIn('-G', self.options(frozen))
        with self.assertRaisesRegex(SafetyError, 'profile'):
            self.options({**frozen, 'partition_profile': {'preemptible': False}})

    def test_legacy_frozen_spec_is_refused_before_any_claim(self):
        legacy = {**dataclasses.asdict(spec()), 'resources': dict(spec().resources)}
        with self.assertRaisesRegex(SafetyError, 'partition_profile'):
            submission_argv('1' * 32, legacy)
        attempt = self.coordinator.admit(spec(), LIMITS)['id']
        with self.coordinator.transaction() as db:
            db.execute('UPDATE attempts SET spec=? WHERE id=?', (canonical(legacy), attempt))
        with self.assertRaisesRegex(SafetyError, 'partition_profile'):
            self.coordinator.dispatch(attempt, lambda _: self.fail('legacy spec reached transport'))
        row = self.coordinator.get(attempt)
        self.assertEqual((row['state'], row['reserved']), ('not_sent', 1))

    def test_frozen_spec_records_profile_hash_and_grant_only_when_borrowed(self):
        grant = {'grantee': 'fixture', 'evidence_reference': 'private-evidence', 'valid_from': 1, 'valid_until': 100, 'scope': 'shared', 'partitions': ['btrippe'], 'limits': LIMITS}
        record = self.coordinator.admit(spec(), LIMITS, grant=grant, now=50)
        self.assertIsNone(record['spec']['grant'])
        self.assertEqual(record['spec']['partition_profile'], dict(partition_profile('normal')))
        self.assertEqual(record['spec']['partitions_sha256'], partitions_sha256())
        self.assertIs(record['spec']['resources']['requeue'], False)
        borrowed = spec(task='borrowed', resources=self.resources('btrippe'))
        with self.assertRaisesRegex(SafetyError, 'grant'):
            self.coordinator.admit(borrowed, LIMITS, now=50)
        with self.assertRaisesRegex(SafetyError, 'partition/scope'):
            self.coordinator.admit(borrowed, LIMITS, grant={**grant, 'partitions': ['owners']}, now=50)
        with self.assertRaisesRegex(SafetyError, 'limits'):
            self.coordinator.admit(borrowed, LIMITS, grant={**grant, 'limits': {**LIMITS, 'cpus': 1}}, now=50)
        admitted = self.coordinator.admit(borrowed, LIMITS, grant=grant, now=50)
        self.assertEqual(admitted['spec']['grant'], grant)
        self.assertTrue(admitted['spec']['partition_profile']['borrowed'])

    def test_budget_rejections_and_task_arithmetic(self):
        limits = {'cpus': 100, 'gpus': 2, 'tasks': 5, 'cpu_seconds': 4800, 'gpu_seconds': 600}
        multi = self.resources(cpus=2, tasks=2)
        first = self.coordinator.admit(spec(task='m1', resources=multi), limits)['id']
        second = self.coordinator.admit(spec(task='m2', resources=multi), limits)['id']
        with self.assertRaisesRegex(SafetyError, 'tasks concurrency'):
            self.coordinator.admit(spec(task='t', resources=self.resources(tasks=2)), limits)
        with self.assertRaisesRegex(SafetyError, 'cpu_seconds budget'):
            self.coordinator.admit(spec(task='b'), limits)
        self.coordinator.reconcile(first, [SubmissionTests.evidence(self, first, cpu_seconds=2400)])
        with self.assertRaisesRegex(SafetyError, 'cpu_seconds budget'):
            self.coordinator.admit(spec(task='b'), limits)
        incomplete = SubmissionTests.evidence(self, second, state='FAILED', cost_known=False, accounting_complete=False, cpu_seconds=1)
        self.coordinator.reconcile(second, [incomplete])
        with self.assertRaisesRegex(SafetyError, 'cpu_seconds budget'):
            self.coordinator.admit(spec(task='b'), limits)
        self.coordinator.reconcile(first, [SubmissionTests.evidence(self, first, cpu_seconds=2400)])
        gpu = spec(task='g1', resource_scope='gpu', resources=self.resources('owners', gpus=1))
        self.coordinator.admit(gpu, limits)
        with self.assertRaisesRegex(SafetyError, 'gpu_seconds budget'):
            self.coordinator.admit(dataclasses.replace(gpu, task='g2'), limits)


class RestartReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.coordinator = Coordinator(Path(self.temp.name) / 'private')

    def admit(self, partition='normal', limits=LIMITS, **overrides):
        return self.coordinator.admit(spec(resources={**spec().resources, 'partition': partition, **overrides}), limits)['id']

    def evidence(self, attempt, **kwargs):
        return SubmissionTests.evidence(self, attempt, **kwargs)

    def dispatched(self, attempt):
        return self.coordinator.dispatch(attempt, lambda _: SimpleNamespace(dispatched=True, returncode=0, stdout='123'))

    def test_submit_time_tolerance(self):
        attempt = self.admit()
        created = self.coordinator.get(attempt)['created']
        with self.assertRaisesRegex(SafetyError, 'stale'):
            self.coordinator.reconcile(attempt, [self.evidence(attempt, submitted_at=created - SUBMIT_TIME_TOLERANCE_SECONDS - 1)])
        result = self.coordinator.reconcile(attempt, [self.evidence(attempt, submitted_at=created - SUBMIT_TIME_TOLERANCE_SECONDS + 1)])
        self.assertEqual(result['resolution'], 'terminal')

    def test_terminal_states_per_profile(self):
        cases = (('normal', {}, 'BOOT_FAIL'), ('normal', {}, 'DEADLINE'), ('owners', {'requeue': False}, 'PREEMPTED'), ('owners', {}, 'NODE_FAIL'))
        for idx, (partition, extra, state) in enumerate(cases):
            with self.subTest(partition=partition, state=state):
                coordinator = Coordinator(Path(self.temp.name) / str(idx))
                attempt = coordinator.admit(spec(resources={**spec().resources, 'partition': partition, **extra}), LIMITS)['id']
                record = coordinator.get(attempt)
                evidence = {key: record['spec'][key] for key in ('cluster', 'principal', 'code_digest', 'input_digest', 'runtime_digest', 'policy_digest')}
                evidence.update(attempt=attempt, submitted_at=record['created'], job_id='123', state=state, cost_known=True, accounting_complete=True, cpu_seconds=25, gpu_seconds=0)
                result = coordinator.reconcile(attempt, [evidence])
                self.assertEqual(result['resolution'], 'terminal')
                row = result['attempt']
                self.assertEqual((row['state'], row['reserved'], row['cpu_seconds'], row['cost_known']), ('terminal', 0, 25, 1))

    def test_preemption_on_normal_is_an_anomaly_until_acknowledged(self):
        attempt = self.admit()
        self.dispatched(attempt)
        preempted = self.evidence(attempt, state='PREEMPTED')
        result = self.coordinator.reconcile(attempt, [preempted])
        self.assertEqual(result['resolution'], 'unexpected_preemption')
        self.assertEqual((result['attempt']['state'], result['attempt']['reserved'], result['attempt']['cost_known']), ('submitted', 1, 0))
        self.assertEqual(result['attempt']['cpu_seconds'], 25)
        self.assertEqual([row['id'] for row in self.coordinator.unresolved()], [attempt])
        with self.assertRaisesRegex(SafetyError, 'unresolved'):
            self.coordinator.admit(spec(parent_attempt=attempt), LIMITS)
        self.assertEqual(self.coordinator.reconcile(attempt, [preempted])['resolution'], 'unexpected_preemption')
        requeued = self.evidence(attempt, state='REQUEUED', cost_known=False, accounting_complete=False)
        self.assertEqual(self.coordinator.reconcile(attempt, [requeued])['resolution'], 'unexpected_preemption')
        self.assertEqual(self.coordinator.get(attempt)['reserved'], 1)
        released = self.coordinator.acknowledge_preemption(attempt)
        self.assertEqual((released['state'], released['reserved'], released['cpu_seconds'], released['cost_known']), ('terminal', 0, 25, 1))
        self.assertEqual(self.coordinator.unresolved(), [])
        with self.assertRaisesRegex(SafetyError, 'already'):
            self.coordinator.acknowledge_preemption(attempt)
        self.assertEqual(self.coordinator.reconcile(attempt, [preempted])['resolution'], 'stale_observation_ignored')
        self.assertEqual(self.coordinator.get(attempt)['state'], 'terminal')
        retry = self.coordinator.admit(spec(parent_attempt=attempt), LIMITS)
        self.assertEqual(retry['spec']['parent_attempt'], attempt)
        with closing(self.coordinator.connect()) as db:
            bodies = [json.loads(row[0]) for row in db.execute('SELECT body FROM evidence WHERE attempt=?', (attempt,))]
        acks = [body for body in bodies if body.get('kind') == 'operator_ack']
        self.assertEqual(len(acks), 1)
        self.assertEqual((acks[0]['attempt'], acks[0]['job_id']), (attempt, '123'))
        self.assertIn('PREEMPTED', acks[0]['states'])

    def test_restart_on_non_preemptible_partition_is_the_same_anomaly(self):
        attempt = self.admit()
        restarted = self.evidence(attempt, state='RUNNING', restart=1, cost_known=False, accounting_complete=False)
        result = self.coordinator.reconcile(attempt, [restarted])
        self.assertEqual(result['resolution'], 'unexpected_preemption')
        self.assertEqual((result['attempt']['state'], result['attempt']['reserved']), ('submitted', 1))
        released = self.coordinator.acknowledge_preemption(attempt)
        self.assertEqual((released['state'], released['reserved'], released['cost_known']), ('terminal', 0, 0))

    def test_acknowledgement_requires_recorded_preemption_on_non_preemptible(self):
        attempt = self.admit()
        with self.assertRaisesRegex(SafetyError, 'no recorded preemption'):
            self.coordinator.acknowledge_preemption(attempt)
        self.coordinator.reconcile(attempt, [self.evidence(attempt, state='RUNNING', accounting_complete=False)])
        with self.assertRaisesRegex(SafetyError, 'no recorded preemption'):
            self.coordinator.acknowledge_preemption(attempt)
        self.assertEqual(self.coordinator.get(attempt)['reserved'], 1)
        owners = self.coordinator.admit(spec(task='owners', resources={**spec().resources, 'partition': 'owners'}), LIMITS)['id']
        result = self.coordinator.reconcile(owners, [self.evidence(owners, state='PREEMPTED', cost_known=False, accounting_complete=False)])
        self.assertEqual(result['resolution'], 'identified')
        with self.assertRaisesRegex(SafetyError, 'preemptible'):
            self.coordinator.acknowledge_preemption(owners)

    def test_requeue_attempt_resolves_from_restart_history(self):
        limits = {'cpus': 4, 'gpus': 2, 'tasks': 2, 'cpu_seconds': 36000, 'gpu_seconds': 1400}
        gpu = spec(resources={**spec().resources, 'partition': 'owners', 'gpus': 1})
        attempt = self.coordinator.admit(gpu, limits)['id']
        first = self.evidence(attempt, state='PREEMPTED', restart=0, cpu_seconds=500, gpu_seconds=500, restart_history_complete=False)
        running = self.evidence(attempt, state='RUNNING', restart=1, cpu_seconds=400, gpu_seconds=400, accounting_complete=False, restart_history_complete=False)
        result = self.coordinator.reconcile(attempt, [first, running])
        self.assertEqual(result['resolution'], 'identified')
        row = result['attempt']
        self.assertEqual((row['state'], row['reserved'], row['job_id']), ('submitted', 1, '123'))
        self.assertEqual((row['cpu_seconds'], row['gpu_seconds'], row['cost_known']), (900, 900, 0))
        # The estimate alone (600 + 600) would fit under 1400; the stored lower bound is charged instead.
        with self.assertRaisesRegex(SafetyError, 'gpu_seconds budget'):
            self.coordinator.admit(dataclasses.replace(gpu, task='second'), limits)
        with self.assertRaisesRegex(SafetyError, 'may not decrease'):
            self.coordinator.reconcile(attempt, [first, {**running, 'cpu_seconds': 300, 'gpu_seconds': 300}])
        done = self.evidence(attempt, state='COMPLETED', restart=1, cpu_seconds=700, gpu_seconds=700, restart_history_complete=True)
        result = self.coordinator.reconcile(attempt, [first, done])
        self.assertEqual(result['resolution'], 'terminal')
        row = result['attempt']
        self.assertEqual((row['state'], row['reserved'], row['cpu_seconds'], row['gpu_seconds'], row['cost_known']), ('terminal', 0, 1200, 1200, 1))
        self.assertEqual(self.coordinator.reconcile(attempt, [first, running])['resolution'], 'stale_observation_ignored')
        with self.assertRaisesRegex(SafetyError, 'conflicts'):
            self.coordinator.reconcile(attempt, [first, {**done, 'state': 'FAILED'}])

    def test_incomplete_restart_history_keeps_reservation_and_cost_unknown(self):
        attempt = self.admit('owners')
        first = self.evidence(attempt, state='PREEMPTED', restart=0, cpu_seconds=10)
        flagged = self.evidence(attempt, state='COMPLETED', restart=1, cpu_seconds=20, restart_history_complete=False)
        result = self.coordinator.reconcile(attempt, [first, flagged])
        self.assertEqual(result['resolution'], 'identified')
        self.assertEqual((result['attempt']['reserved'], result['attempt']['cpu_seconds'], result['attempt']['cost_known']), (1, 30, 0))
        gap = self.evidence(attempt, state='COMPLETED', restart=2, cpu_seconds=25, restart_history_complete=True)
        result = self.coordinator.reconcile(attempt, [first, gap])
        self.assertEqual(result['resolution'], 'identified')
        self.assertEqual((result['attempt']['reserved'], result['attempt']['cpu_seconds'], result['attempt']['cost_known']), (1, 35, 0))
        middle = self.evidence(attempt, state='COMPLETED', restart=1, cpu_seconds=20, restart_history_complete=True)
        result = self.coordinator.reconcile(attempt, [first, middle, gap])
        self.assertEqual(result['resolution'], 'terminal')
        self.assertEqual((result['attempt']['reserved'], result['attempt']['cpu_seconds'], result['attempt']['cost_known']), (0, 55, 1))
        other = self.coordinator.admit(spec(task='other', resources={**spec().resources, 'partition': 'owners'}), LIMITS)['id']
        with self.assertRaisesRegex(SafetyError, 'restart'):
            self.coordinator.reconcile(other, [self.evidence(other, restart=-1)])

    def test_unresolved_lists_reserved_open_attempts_only(self):
        limits = {'cpus': 8, 'gpus': 0, 'tasks': 8, 'cpu_seconds': 36000}
        not_sent, submitted, unknown, terminal = (self.coordinator.admit(spec(task=task), limits)['id'] for task in ('a', 'b', 'c', 'd'))
        self.dispatched(submitted)
        self.coordinator.dispatch(unknown, lambda _: SimpleNamespace(dispatched=True, returncode=255, stdout=''))
        self.dispatched(terminal)
        self.coordinator.reconcile(terminal, [self.evidence(terminal)])
        rows = self.coordinator.unresolved()
        self.assertEqual([row['id'] for row in rows], [submitted, unknown])
        self.assertEqual([row['state'] for row in rows], ['submitted', 'unknown'])
        self.assertTrue(all(isinstance(row['spec'], dict) and row['reserved'] == 1 for row in rows))


if __name__ == '__main__':
    unittest.main()
