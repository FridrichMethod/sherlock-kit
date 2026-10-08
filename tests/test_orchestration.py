from contextlib import closing
import dataclasses
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
from sherlock_orchestration import AttemptSpec, Coordinator, SafetyError, resources_checked, scheduler_cost, submission_argv

D = 'a' * 64
LIMITS = {'cpus': 2, 'gpus': 0, 'tasks': 2, 'cpu_seconds': 3600}


def spec(**kwargs):
    data = dict(project='consumer', campaign='new-pilot', task='test', cluster='sherlock', principal='fixture', resource_scope='shared', code_digest=D, input_digest=D, runtime_digest=D, policy_digest=D, resources={'partition': 'normal', 'cpus': 1, 'gpus': 0, 'tasks': 1, 'memory_mb': 1024, 'walltime_seconds': 600}, remote_script='/synthetic/releases/frozen/job.sh', script_digest=D)
    data.update(kwargs)
    return AttemptSpec(**data)


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
        argv = submission_argv('1' * 32, {**dataclasses.asdict(spec()), 'resources': r})
        self.assertIn('--constraint=CPU_GEN1|CPU_GEN2', argv[-2])
        for wrong in ({'account': 'group'}, {'exclude': 'node'}, {'cpus': True}, {'constraint': 'cpu; false'}, {'signal': 'B:USR1@60\n'}):
            with self.subTest(wrong=wrong), self.assertRaises(SafetyError):
                resources_checked({**spec().resources, **wrong})


if __name__ == '__main__':
    unittest.main()
